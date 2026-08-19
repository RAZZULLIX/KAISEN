# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Project specs and registry.

A project is a directory under projects/<id>/ containing:
  project.json   — the declarative spec (pipeline steps, metrics, guardrails,
                   prompts, skills, data)
  harness/       — the harness programs (user-authored; the "programs" the
                   pipeline runs, in the order the spec declares)
  prompts/       — modular generation/study/lesson prompt templates
  runs/          — per-generation artifacts (created at runtime)
  best/          — champion artifacts (created at runtime)
  state.json     — runtime state (created at runtime)

Pipeline command placeholders (substituted per step):
  {candidate}   path to the candidate source file
  {artifact}    path to the built artifact (after the build step)
  {project_dir} the project directory
  {workdir}     scratch dir for this generation (owned by the worker)
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PROJECTS_DIR
from .scores import validate_metric_schema
from .util import load_json, save_json

SPEC_FILE = "project.json"
HARNESS_DIR = "harness"
PROMPTS_DIR = "prompts"
RUNS_DIR = "runs"
BEST_DIR = "best"
STATE_FILE = "state.json"

# Steps that take a list of commands (order matters).
MULTI_STEPS = ("verify", "score")
SINGLE_STEPS = ("build",)

DEFAULT_SPEC: Dict[str, Any] = {
    "id": "",
    "name": "",
    "description": "",
    "language": "c",
    "artifact_name": "program",
    "steps": {
        "build": {
            "program": "harness/build.py",
            "args": ["{candidate}", "{artifact}"],
            "timeout": 120,
            "memory_limit_mb": 4096,
        },
        "verify": [],
        "score": [],
    },
    "metrics": {},
    "telemetry": {"enabled": True, "progress_token": "KAISEN_PROGRESS", "live_fields": []},
    # Startup sizing: project > config defaults > built-in 1/1.
    "engine": {"workers": None, "multi": None},
    "select": {"hysteresis": 1.0001},
    "guardrails": {"enabled": True, "allow_extra": [], "deny_extra": []},
    "prompts": {"generation_dir": "prompts/generation", "study": "", "lesson": ""},
    "skills": {"analyze": True, "dedup": True, "deepwork": {"enabled": False}},
    "data": {},
}


def validate_spec(spec: Dict[str, Any]) -> List[str]:
    """Structural validation of a project spec. Returns a list of errors
    (empty == valid)."""
    errors: List[str] = []
    if not spec.get("id"):
        errors.append("id: required")
    if not spec.get("name"):
        errors.append("name: required")
    steps = spec.get("steps") or {}
    if "build" not in steps:
        errors.append("steps.build: required")
    for stage in ("verify", "score"):
        for i, step in enumerate(steps.get(stage, []) or []):
            if not isinstance(step, dict):
                errors.append(f"steps.{stage}[{i}]: must be an object")
                continue
            inline = step.get("inline")
            has_inline = isinstance(inline, dict) and str(inline.get("code", "")).strip()
            if not step.get("program") and not has_inline:
                errors.append(f"steps.{stage}[{i}]: program or inline script required")
            if stage == "score":
                stg = step.get("stage")
                if stg is not None and stg not in ("screen", "confirm"):
                    errors.append(f"steps.score[{i}].stage: must be 'screen' or 'confirm' (screen = cheap filter, confirm = robust measurement used for selection)")
    metrics = spec.get("metrics") or {}
    # validate_metric_schema reports the empty case — don't double it.
    errors.extend(validate_metric_schema(metrics))
    eng = spec.get("engine") or {}
    autofix = eng.get("autofix")
    if autofix is not None:
        if not isinstance(autofix, dict):
            errors.append("engine.autofix: must be an object")
        else:
            tries = autofix.get("tries")
            repair = autofix.get("repair")
            if tries is not None:
                try:
                    if int(tries) < 1:
                        errors.append("engine.autofix.tries: must be an integer >= 1")
                except (TypeError, ValueError):
                    errors.append("engine.autofix.tries: must be an integer >= 1")
            if repair is not None:
                try:
                    if int(repair) < 0:
                        errors.append("engine.autofix.repair: must be an integer >= 0 (0 = LLM repair off)")
                except (TypeError, ValueError):
                    errors.append("engine.autofix.repair: must be an integer >= 0 (0 = LLM repair off)")
    retention = eng.get("retention")
    if retention is not None:
        if not isinstance(retention, dict):
            errors.append("engine.retention: must be an object")
        else:
            kl = retention.get("keep_last")
            if kl is not None and (not isinstance(kl, int) or isinstance(kl, bool) or kl < 1):
                errors.append("engine.retention.keep_last: must be an integer >= 1")
            kb = retention.get("keep_best")
            if kb is not None and not isinstance(kb, bool):
                errors.append("engine.retention.keep_best: must be boolean")
            en = retention.get("enabled")
            if en is not None and not isinstance(en, bool):
                errors.append("engine.retention.enabled: must be boolean")
    telemetry = spec.get("telemetry") or {}
    if not isinstance(telemetry.get("live_fields", []), list):
        errors.append("telemetry.live_fields: must be a list of metric keys shown live in worker cards")
    for key in telemetry.get("live_fields", []):
        if not isinstance(key, str) or not key:
            errors.append("telemetry.live_fields: keys must be non-empty strings")
        # Every metric must come from a score step's parse rules or be
        # declared as a derived/baseline metric — warn, not error, since
        # parse rules may be added later.
        pass
    try:
        hyst = float((spec.get("select") or {}).get("hysteresis", 1.0001))
        if hyst < 1.0:
            errors.append("select.hysteresis: must be >= 1 (values below 1 accept worse results as improvements)")
    except (TypeError, ValueError):
        errors.append("select.hysteresis: must be numeric")
    return errors


def _merge_defaults(spec: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_SPEC)
    merged.update(copy.deepcopy(spec))
    # nested merges
    for section in ("steps", "metrics", "telemetry", "engine", "select", "guardrails", "prompts", "skills", "data"):
        if isinstance(spec.get(section), dict):
            base = merged.get(section)
            if isinstance(base, dict):
                base.update(copy.deepcopy(spec[section]))
    return merged


class Project:
    def __init__(self, path: Path):
        self.path = path
        self.spec: Dict[str, Any] = _merge_defaults(load_json(path / SPEC_FILE, {}))
        self.spec["dir"] = str(path)

    def reload(self) -> None:
        """Re-read project.json into this Project (spec changes apply to the
        engine/workers at the next generation without a restart)."""
        self.spec = _merge_defaults(load_json(self.path / SPEC_FILE, {}))
        self.spec["dir"] = str(self.path)

    @property
    def id(self) -> str:
        return self.spec.get("id", self.path.name)

    @property
    def name(self) -> str:
        return self.spec.get("name", self.id)

    @property
    def harness_dir(self) -> Path:
        return self.path / HARNESS_DIR

    @property
    def prompts_dir(self) -> Path:
        return self.path / PROMPTS_DIR

    @property
    def runs_dir(self) -> Path:
        return self.path / RUNS_DIR

    @property
    def best_dir(self) -> Path:
        return self.path / BEST_DIR

    @property
    def state_file(self) -> Path:
        return self.path / STATE_FILE
    def resolve_program(self, program: str) -> Path:
        """Resolve a step's program path relative to the project dir.
        Bare system launchers (gcc, python3, make, …) stay as-is — they
        are the guardrail allowlist, not project files."""
        from .guardrails import ALLOWED_LAUNCHERS
        p = Path(program)
        if p.is_absolute():
            return p
        if program in ALLOWED_LAUNCHERS and "/" not in program:
            return p
        return (self.path / p).resolve()

    def ensure_dirs(self) -> None:
        for d in (self.harness_dir, self.prompts_dir, self.runs_dir, self.best_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def default_workers(self) -> int:
        """Startup sizing priority: project spec > config defaults > 1."""
        from .config import get_config
        spec_n = (self.spec.get("engine") or {}).get("workers")
        if spec_n:
            return max(1, int(spec_n))
        cfg_n = get_config().workers.get("default_count")
        if cfg_n:
            return max(1, int(cfg_n))
        return 1

    @property
    def default_multi(self) -> int:
        """Startup sizing priority: project spec > config defaults > 1."""
        from .config import get_config
        spec_n = (self.spec.get("engine") or {}).get("multi")
        if spec_n:
            return max(1, int(spec_n))
        cfg_n = get_config().engine.get("default_multi")
        if cfg_n:
            return max(1, int(cfg_n))
        return 1


class ProjectRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else PROJECTS_DIR
        self._projects: Dict[str, Project] = {}
        self.scan()

    def scan(self) -> None:
        self._projects = {}
        if not self.root.exists():
            return
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and (child / SPEC_FILE).is_file():
                try:
                    p = Project(child)
                    self._projects[p.id] = p
                except Exception:
                    continue

    def list(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.spec.get("description", ""),
                "path": str(p.path),
                "metrics": p.spec.get("metrics", {}),
            }
            for p in self._projects.values()
        ]

    def get(self, project_id: str) -> Optional[Project]:
        return self._projects.get(project_id)

    def require(self, project_id: str) -> Project:
        p = self._projects.get(project_id)
        if p is None:
            raise KeyError(f"project '{project_id}' not found")
        return p

    def create(self, project_id: str, spec: Dict[str, Any]) -> Project:
        """Create a new project from a spec dict (validated)."""
        if not re_ident(project_id):
            raise ValueError("project id must be [a-z0-9_-]+")
        path = self.root / project_id
        if path.exists():
            raise ValueError(f"project '{project_id}' already exists")
        spec = _merge_defaults(spec)
        spec["id"] = project_id
        errors = validate_spec(spec)
        if errors:
            raise ValueError("invalid project spec: " + "; ".join(errors))
        path.mkdir(parents=True, exist_ok=True)
        (path / HARNESS_DIR).mkdir(exist_ok=True)
        (path / PROMPTS_DIR).mkdir(exist_ok=True)
        save_json(path / SPEC_FILE, spec)
        p = Project(path)
        self._projects[project_id] = p
        return p

    def update_spec(self, project_id: str, spec: Dict[str, Any]) -> Project:
        p = self.require(project_id)
        # Runtime-injected fields (Project.__init__ adds "dir") must never
        # be persisted — they are derived, not spec.
        spec = {k: v for k, v in spec.items() if k != "dir"}
        merged = _merge_defaults(spec)
        merged["id"] = project_id
        errors = validate_spec(merged)
        if errors:
            raise ValueError("invalid project spec: " + "; ".join(errors))
        save_json(p.path / SPEC_FILE, merged)
        p.spec = merged
        p.spec["dir"] = str(p.path)
        return p

    def delete(self, project_id: str) -> None:
        p = self._projects.pop(project_id, None)
        if p is None:
            return
        # Full wipe: the GUI already made the user type the project name
        # to confirm — no need for a partial "safety" delete here.
        shutil.rmtree(p.path, ignore_errors=True)


def re_ident(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9_-]+", s or ""))


# ---------------------------------------------------------------------------
# Command template substitution
# ---------------------------------------------------------------------------


def expand_command(
    step: Dict[str, Any],
    project: Project,
    candidate: str | Path,
    artifact: Optional[str | Path],
    workdir: str | Path,
) -> List[str]:
    """Build the concrete argv for a pipeline step, substituting
    placeholders, then resolving the program path."""
    prog = str(step["program"])
    args = [str(a) for a in step.get("args", [])]
    subs = {
        "candidate": str(candidate),
        "artifact": str(artifact) if artifact else "",
        "project_dir": str(project.path),
        "workdir": str(workdir),
    }
    cmd = [prog]
    for a in args:
        for k, v in subs.items():
            a = a.replace("{" + k + "}", v)
        cmd.append(a)
    # Resolve the program path against the project dir (guardrails rely on
    # it living inside the project).
    resolved = project.resolve_program(cmd[0])
    cmd[0] = str(resolved)
    return cmd
