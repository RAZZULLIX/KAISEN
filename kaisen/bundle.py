# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Bundles — export/import of projects and sets as ONE portable file.

A bundle is a `.kaisen.zip` with one layout for both kinds:

    manifest.json          {kind: "project"|"set", version, id, name, exported_at}
    set.json               the set definition            (set bundles only)
    projects/<pid>/...     the project files             (one dir per project)

Exporting a PROJECT is a bundle with one project and no set; exporting a
SET adds the set definition and every member project.  Import auto-detects
the kind from the manifest — one endpoint, one GUI button, one KAI command.

A bundle is the portable DEFINITION of the work: `runs/` and `state.json`
are machine-local runtime data and are NEVER exported.  Two OPTIONAL
payloads may travel with each project — both default ON, and the export
options (include_best / include_baseline / baseline_measured) are
documented on build_project_bundle:

    projects/<pid>/best/…         everything best/ holds on the exporting
                                  machine, plus best/meta.json — the
                                  champion's score/metrics/provenance from
                                  its state.json `best` entry
    projects/<pid>/baseline.json  the MEASURED baseline (fitness/metrics/
                                  generation) when the engine has scored
                                  the baseline source

Old bundles without those payloads import unchanged: import tolerates both.
Tags never travel inside a project bundle (an imported project must land as
an orphan, visible in the default workspace — a dangling tag would hide it);
a set bundle re-tags its members with the set's id, remapped on import.

Import NEVER overwrites: ids that collide are renamed with `-2`, `-3`, …
and the response reports every `old → new` mapping.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from .projects import (BEST_DIR, PROMPTS_DIR, RUNS_DIR, SPEC_FILE,
                       STATE_FILE, Project, re_ident)
from .util import load_json

BUNDLE_VERSION = 1
KIND_PROJECT = "project"
KIND_SET = "set"
SUFFIX = ".kaisen.zip"

# Optional export payloads (absent from old bundles — import tolerates both).
BEST_META = "meta.json"          # champion score/provenance, inside best/
BASELINE_META = "baseline.json"  # measured baseline record, project root
BASELINE_MEASURED_OPTS = ("auto", "yes", "no")

# Top-level project entries that are runtime data, not definition.
_RUNTIME_SKIP = {RUNS_DIR, BEST_DIR, STATE_FILE}

# Unpack caps — a bundle is a definition, it must stay small.
MAX_FILES = 5000
MAX_TOTAL_BYTES = 1 << 30  # 1 GiB unpacked


class BundleError(ValueError):
    """Malformed / unsafe / unsupported bundle."""


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _raw_spec(project: Project) -> Dict[str, Any]:
    """The spec as stored on disk — no runtime-injected fields, no defaults
    baked in.  `tags` are stripped: membership is decided by the import."""
    spec = json.loads((project.path / SPEC_FILE).read_text(encoding="utf-8"))
    spec.pop("dir", None)
    spec["tags"] = []
    return spec


def collect_project_files(project: Project, include_baseline: bool = True) -> Dict[str, bytes]:
    """Every definition file of a project (harness, prompts, data, and —
    unless excluded — the baseline source) as {posix-rel-path: bytes}.
    Runtime dirs/files are always excluded."""
    files: Dict[str, bytes] = {}
    root = project.path
    baseline_name = str((project.spec.get("data") or {}).get("baseline_source") or "")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] in _RUNTIME_SKIP:
            continue
        if not include_baseline and baseline_name and rel.as_posix() == baseline_name:
            continue
        files[rel.as_posix()] = path.read_bytes()
    return files


def collect_best_files(project: Project) -> Dict[str, bytes]:
    """Everything best/ holds on the exporting machine ({posix-rel-path:
    bytes}), excluding meta.json — that sidecar is regenerated from the
    project's state at export time and never carried over stale."""
    files: Dict[str, bytes] = {}
    best_dir = project.path / BEST_DIR
    if not best_dir.is_dir():
        return files
    for path in sorted(best_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(project.path).as_posix()
        if rel == f"{BEST_DIR}/{BEST_META}":
            continue
        files[rel] = path.read_bytes()
    return files


def best_payload(project: Project) -> Optional[Dict[str, Any]]:
    """The champion's score + provenance as recorded in state.json's `best`
    entry — {fitness, metrics, generation, code_path (project-relative),
    artifact}.  None when the project has no recorded best."""
    st = load_json(project.state_file, {}) or {}
    best = st.get("best") if isinstance(st.get("best"), dict) else {}
    if best.get("fitness") is None and not best.get("metrics"):
        return None
    out: Dict[str, Any] = {}
    for key in ("fitness", "generation"):
        if best.get(key) is not None:
            out[key] = best[key]
    if best.get("metrics"):
        out["metrics"] = dict(best["metrics"])
    cp = str(best.get("code_path") or "")
    if cp:
        try:
            out["code_path"] = PurePosixPath(Path(cp).resolve().relative_to(project.path)).as_posix()
        except (ValueError, OSError):
            out["code_path"] = PurePosixPath(cp).name
    if best.get("artifact"):
        out["artifact"] = str(best["artifact"])
    return out


def _champion_bytes(project: Project, best: Dict[str, Any]) -> Optional[bytes]:
    """The champion program's bytes: state's code_path when it still exists,
    else the lone non-meta file under best/."""
    cp = str(best.get("code_path") or "")
    if cp:
        try:
            p = Path(cp)
            if p.is_file():
                return p.read_bytes()
        except OSError:
            pass
    best_dir = project.path / BEST_DIR
    if best_dir.is_dir():
        files = [p for p in best_dir.rglob("*")
                 if p.is_file() and not p.is_symlink() and p.name != BEST_META]
        if len(files) == 1:
            try:
                return files[0].read_bytes()
            except OSError:
                pass
    return None


def measured_baseline(project: Project) -> Optional[Dict[str, Any]]:
    """The recorded measurement of the baseline source, or None when the
    engine never scored it.

    The framework keeps no standalone baseline record — its score lives in
    the champion data state.json writes — so this derives it:
      1. the current champion IS the baseline (its bytes equal the baseline
         source): state.json's `best` entry is that measurement;
      2. else the latest `baseline_reeval` history entry — the forced
         re-measurement after the baseline source changed.
    Returns {source, fitness, metrics, generation}."""
    source = str((project.spec.get("data") or {}).get("baseline_source") or "")
    if not source:
        return None
    st = load_json(project.state_file, {}) or {}
    best = st.get("best") if isinstance(st.get("best"), dict) else {}
    base_file = project.path / source
    if (best.get("fitness") is not None or best.get("metrics")) and base_file.is_file():
        champ = _champion_bytes(project, best)
        if champ is not None and champ == base_file.read_bytes():
            return {"source": source, "fitness": best.get("fitness"),
                    "metrics": best.get("metrics") or {},
                    "generation": best.get("generation")}
    for entry in reversed(st.get("history") or []):
        if isinstance(entry, dict) and entry.get("outcome") == "baseline_reeval" \
                and entry.get("fitness") is not None:
            return {"source": source, "fitness": entry["fitness"],
                    "metrics": entry.get("metrics") or {},
                    "generation": entry.get("generation")}
    return None


def _check_export_options(include_best: bool, include_baseline: bool,
                          baseline_measured: str) -> None:
    if baseline_measured not in BASELINE_MEASURED_OPTS:
        raise BundleError(
            f"baseline_measured must be {'|'.join(BASELINE_MEASURED_OPTS)} "
            f"(got {baseline_measured!r})")
    if baseline_measured == "yes" and not include_baseline:
        raise BundleError("baseline_measured=yes requires include_baseline=1")


def _put(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    zf.writestr(name, data)


def _manifest(kind: str, ident: str, name: str) -> bytes:
    return json.dumps({"kind": kind, "version": BUNDLE_VERSION,
                       "id": ident, "name": name,
                       "exported_at": time.time()},
                      indent=2).encode("utf-8")


def _write_project(zf: zipfile.ZipFile, project: Project, spec: Dict[str, Any],
                   include_best: bool = True, include_baseline: bool = True,
                   baseline_measured: str = "auto") -> None:
    pid = project.id
    _put(zf, f"projects/{pid}/{SPEC_FILE}",
         json.dumps(spec, indent=2).encode("utf-8"))
    for rel, data in collect_project_files(project, include_baseline).items():
        if rel == SPEC_FILE:
            continue
        _put(zf, f"projects/{pid}/{rel}", data)
    if include_best:
        best_files = collect_best_files(project)
        meta = best_payload(project)
        if meta is not None:
            best_files[f"{BEST_DIR}/{BEST_META}"] = json.dumps(
                {**meta, "exported_at": time.time()}, indent=2).encode("utf-8")
        for rel, data in best_files.items():
            _put(zf, f"projects/{pid}/{rel}", data)
    if include_baseline:
        mb = measured_baseline(project)
        if mb is None and baseline_measured == "yes":
            raise BundleError(
                f"project '{pid}' has no measured baseline — "
                "baseline_measured=yes cannot be satisfied")
        if mb is not None and baseline_measured != "no":
            _put(zf, f"projects/{pid}/{BASELINE_META}", json.dumps(
                {**mb, "exported_at": time.time()}, indent=2).encode("utf-8"))


def build_project_bundle(project: Project, include_best: bool = True,
                         include_baseline: bool = True,
                         baseline_measured: str = "auto") -> bytes:
    """A project bundle with optional runtime payloads (default: full).

    include_best       ship everything best/ holds plus best/meta.json — the
                       champion's score/metrics/provenance (default True).
    include_baseline   ship the baseline source file and, when a measured
                       baseline exists, baseline.json (default True).  False
                       exports the bare pipeline definition only.
    baseline_measured  "auto" (default): the measured baseline travels when
                       it exists; "yes": same, but BundleError when any
                       project has none; "no": never ship the measured score.
    """
    _check_export_options(include_best, include_baseline, baseline_measured)
    spec = _raw_spec(project)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _put(zf, "manifest.json", _manifest(KIND_PROJECT, project.id, project.name))
        _write_project(zf, project, spec, include_best, include_baseline,
                       baseline_measured)
    return buf.getvalue()


def build_set_bundle(set_def: Dict[str, Any], members: List[Project],
                     include_best: bool = True, include_baseline: bool = True,
                     baseline_measured: str = "auto") -> bytes:
    """A set + all its projects.  Member specs carry tags=[set-id] so the
    membership survives the round trip (remapped if the id collides).
    The three export options apply to EVERY member identically — see
    build_project_bundle."""
    _check_export_options(include_best, include_baseline, baseline_measured)
    sid = set_def["id"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _put(zf, "manifest.json", _manifest(KIND_SET, sid, set_def.get("name", sid)))
        _put(zf, "set.json", json.dumps(
            {"id": sid, "name": set_def.get("name", sid),
             "description": set_def.get("description", "")}, indent=2).encode("utf-8"))
        for p in members:
            spec = _raw_spec(p)
            spec["tags"] = [sid]
            _write_project(zf, p, spec, include_best, include_baseline,
                           baseline_measured)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Reading / validating
# ---------------------------------------------------------------------------

def _safe_member(name: str) -> bool:
    """Zip entries must be plain relative paths: no absolute, no '..', no
    backslash tricks, no drive letters."""
    if not name or name.startswith("/") or "\\" in name:
        return False
    p = PurePosixPath(name)
    if p.is_absolute() or p.drive:
        return False
    return ".." not in p.parts


def read_bundle(data: bytes) -> Dict[str, Any]:
    """Parse + validate a bundle.  Returns
    {kind, id, name, set: {id,name,description}|None,
     projects: {pid: {"spec": dict, "files": {rel: bytes}}}}.
    Raises BundleError on anything malformed or unsafe — the caller imports
    nothing unless this returns."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise BundleError(f"not a readable zip file: {e}") from e

    with zf:
        names = zf.namelist()
        for n in names:
            if not _safe_member(n):
                raise BundleError(f"unsafe path in bundle: {n!r}")
        if len(names) > MAX_FILES:
            raise BundleError(f"bundle has too many files ({len(names)} > {MAX_FILES})")
        total = sum(i.file_size for i in zf.infolist())
        if total > MAX_TOTAL_BYTES:
            raise BundleError(f"bundle unpacks to {total} bytes (cap {MAX_TOTAL_BYTES})")

        if "manifest.json" not in names:
            raise BundleError("manifest.json missing — not a KAISEN bundle")
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception as e:
            raise BundleError(f"manifest.json unreadable: {e}") from e
        if manifest.get("version") != BUNDLE_VERSION:
            raise BundleError(f"unsupported bundle version {manifest.get('version')!r}")
        kind = manifest.get("kind")
        if kind not in (KIND_PROJECT, KIND_SET):
            raise BundleError(f"unknown bundle kind {kind!r}")
        bid = str(manifest.get("id", ""))
        if not re_ident(bid):
            raise BundleError(f"bundle id {bid!r} is not [a-z0-9_-]+")

        set_def: Optional[Dict[str, Any]] = None
        if kind == KIND_SET:
            if "set.json" not in names:
                raise BundleError("set bundle without set.json")
            try:
                set_def = json.loads(zf.read("set.json").decode("utf-8"))
            except Exception as e:
                raise BundleError(f"set.json unreadable: {e}") from e
            if str(set_def.get("id", "")) != bid:
                raise BundleError("set.json id does not match the manifest")

        projects: Dict[str, Dict[str, Any]] = {}
        for n in names:
            if not n.startswith("projects/"):
                continue
            parts = PurePosixPath(n).parts
            if len(parts) < 3:
                raise BundleError(f"loose file in bundle: {n!r}")
            pid = parts[1]
            if not re_ident(pid):
                raise BundleError(f"project dir {pid!r} is not [a-z0-9_-]+")
            entry = projects.setdefault(pid, {"spec": None, "files": {}})
            rel = "/".join(parts[2:])
            if rel == SPEC_FILE:
                try:
                    entry["spec"] = json.loads(zf.read(n).decode("utf-8"))
                except Exception as e:
                    raise BundleError(f"projects/{pid}/{SPEC_FILE} unreadable: {e}") from e
            else:
                entry["files"][rel] = zf.read(n)

        if not projects and kind == KIND_PROJECT:
            raise BundleError("project bundle without a project")
        for pid, entry in projects.items():
            if entry["spec"] is None:
                raise BundleError(f"project '{pid}' has no {SPEC_FILE}")
            if str(entry["spec"].get("id", pid)) != pid:
                raise BundleError(f"project dir '{pid}' disagrees with its spec id")
            entry["spec"].pop("dir", None)

        return {"kind": kind, "id": bid, "name": str(manifest.get("name", bid)),
                "set": set_def, "projects": projects}
