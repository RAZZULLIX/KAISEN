"""BUNDLES API: export/import of projects and sets as .kaisen.zip.

Contract (kaisen/bundle.py + server handlers): one zip layout for both
kinds; export downloads it, import auto-detects the kind from the manifest.
Import NEVER overwrites — colliding ids are renamed -2, -3, … and every
old → new mapping is reported.  Malformed/unsafe bundles are rejected with
400 and nothing is created (atomic).  runs/ and state.json never travel;
the champion's best/ payload and the measured baseline are OPTIONAL export
options (include_best / include_baseline / baseline_measured, default full)
and import tolerates bundles with and without them.  Tags never travel
inside a project bundle.
"""
import asyncio
import io
import json
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest
import requests

from kaisen.server import DashboardServer

POLL = 0.05


def _live_server(tmp_cfg, registry):
    """(server, base_url) for a live DashboardServer on an ephemeral port."""
    srv = DashboardServer(registry, tmp_cfg, engine=None,
                          host="127.0.0.1", port=8080,
                          temp_root=tmp_cfg.path.parent / "temp")
    holder = {}

    def _serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop
        runner = __import__("aiohttp").web.AppRunner(srv.app)
        loop.run_until_complete(runner.setup())
        site = __import__("aiohttp").web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        holder["port"] = site._server.sockets[0].getsockname()[1]
        holder["runner"] = runner
        loop.run_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    deadline = time.time() + 10
    while "port" not in holder and time.time() < deadline:
        time.sleep(POLL)
    assert "port" in holder, "server did not bind"
    srv.port = holder["port"]
    base = f"http://127.0.0.1:{holder['port']}"
    yield srv, base
    loop = holder["loop"]
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)


@pytest.fixture
def api(tmp_cfg, registry):
    yield from _live_server(tmp_cfg, registry)


def _spec(pid, name="X"):
    return {"id": pid, "name": name,
            "steps": {"build": {"program": "gcc", "args": []}, "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}


def _create(base, pid, files=None):
    spec = _spec(pid, pid)
    if files:
        spec["files"] = files
    r = requests.post(base + "/api/projects", json={"id": pid, "spec": spec}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()["project"]


def _create_set(base, name, description=""):
    r = requests.post(base + "/api/sets",
                      json={"name": name, "description": description}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()["set"]


def _add_members(base, sid, pids):
    r = requests.post(base + f"/api/sets/{sid}/members",
                      json={"project_ids": pids}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"], r.text


def _export(base, path):
    r = requests.get(base + path, timeout=10)
    assert r.status_code == 200, r.text
    assert r.headers["Content-Type"] == "application/zip"
    return r.content


def _read_zip(data):
    zf = zipfile.ZipFile(io.BytesIO(data))
    return zf, {n: zf.read(n) for n in zf.namelist()}


def _import(base, data, method="POST"):
    return requests.request(method, base + "/api/import", data=data,
                            headers={"Content-Type": "application/zip"}, timeout=10)


def _rows(base):
    r = requests.get(base + "/api/projects", timeout=5)
    return {p["id"]: p for p in r.json()["projects"]}


# ----------------------------------------------------------------------
# 1. Project export
# ----------------------------------------------------------------------

def test_project_export_contents(api, registry):
    _, base = api
    _create(base, "alpha", files={"harness/build.py": "print(1)\n",
                                  "original.c": "int main(){}\n"})
    # runtime junk that must NEVER travel (runs/ and state.json); best/
    # travels by default — the full export includes everything best/ holds
    pdir = registry.root / "alpha"
    (pdir / "runs" / "gen_000001").mkdir(parents=True, exist_ok=True)
    (pdir / "runs" / "gen_000001" / "program").write_bytes(b"\x7fELF")
    (pdir / "best").mkdir(exist_ok=True)
    (pdir / "best" / "program.c").write_text("champion")
    (pdir / "state.json").write_text("{}")

    zf, names = _read_zip(_export(base, "/api/projects/alpha/export"))
    manifest = json.loads(names["manifest.json"])
    assert manifest["kind"] == "project" and manifest["id"] == "alpha"
    spec = json.loads(names["projects/alpha/project.json"])
    assert spec["name"] == "alpha" and "dir" not in spec
    assert names["projects/alpha/harness/build.py"] == b"print(1)\n"
    assert names["projects/alpha/original.c"] == b"int main(){}\n"
    # state.json is {} here, so only the on-disk champion travels — no meta
    assert not any("/runs/" in n or n.endswith("state.json") for n in names)
    assert names["projects/alpha/best/program.c"] == b"champion"


def test_project_export_missing_404(api):
    _, base = api
    assert requests.get(base + "/api/projects/ghost/export", timeout=5).status_code == 404


# ----------------------------------------------------------------------
# 2. Project import: round trip, no-overwrite, PUT upload
# ----------------------------------------------------------------------

def test_project_import_roundtrip_and_dedupe(api, registry):
    _, base = api
    _create(base, "alpha", files={"harness/build.py": "print(1)\n"})
    data = _export(base, "/api/projects/alpha/export")

    r = _import(base, data)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["kind"] == "project"
    assert out["projects"] == {"alpha": "alpha-2"}     # never overwrites
    assert out["set"] is None
    rows = _rows(base)
    assert rows["alpha-2"]["tags"] == []               # lands as an orphan
    assert (registry.root / "alpha-2" / "harness" / "build.py").read_text() == "print(1)\n"

    # second import renames again — -3
    out2 = _import(base, data).json()
    assert out2["projects"] == {"alpha": "alpha-3"}


def test_import_put_matches_curl_dash_T(api):
    _, base = api
    _create(base, "beta")
    data = _export(base, "/api/projects/beta/export")
    r = _import(base, data, method="PUT")     # `curl -T` sends PUT
    assert r.status_code == 200 and r.json()["ok"]


def test_import_rejects_garbage_and_empty(api):
    _, base = api
    assert _import(base, b"not a zip").status_code == 400
    assert _import(base, b"").status_code == 400


def test_import_rejects_zip_slip(api, registry):
    _, base = api
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"kind": "project", "version": 1, "id": "evil", "name": "evil"}))
        zf.writestr("projects/evil/project.json", json.dumps(_spec("evil")))
        zf.writestr("projects/evil/../../escaped.txt", "nope")
    r = _import(base, buf.getvalue())
    assert r.status_code == 400 and "unsafe" in r.json()["error"]
    assert "evil" not in _rows(base)


def test_import_guardrail_atomic(api):
    """A blocked pipeline anywhere in the bundle rejects the WHOLE import —
    the good sibling must not be created."""
    _, base = api
    good = dict(_spec("good"), name="good")
    evil = {"id": "evil", "name": "evil",
            "steps": {"build": {"program": "rm", "args": ["-rf", "/"]},
                      "verify": [], "score": []},
            "metrics": {"ms": {"direction": "lower"}}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"kind": "project", "version": 1, "id": "evil", "name": "evil"}))
        zf.writestr("projects/good/project.json", json.dumps(good))
        zf.writestr("projects/evil/project.json", json.dumps(evil))
    r = _import(base, buf.getvalue())
    assert r.status_code == 400 and "guardrail" in r.json()["error"]
    assert "good" not in _rows(base)


# ----------------------------------------------------------------------
# 3. Set export/import
# ----------------------------------------------------------------------

def test_set_export_roundtrip(api, registry):
    _, base = api
    _create(base, "m1"); _create(base, "m2"); _create(base, "lonely")
    s = _create_set(base, "My Fleet", "fleet desc")
    _add_members(base, s["id"], ["m1", "m2"])

    zf, names = _read_zip(_export(base, f"/api/sets/{s['id']}/export"))
    manifest = json.loads(names["manifest.json"])
    assert manifest["kind"] == "set" and manifest["id"] == s["id"]
    set_def = json.loads(names["set.json"])
    assert set_def["name"] == "My Fleet" and set_def["description"] == "fleet desc"
    assert "projects/m1/project.json" in names and "projects/m2/project.json" in names
    assert "projects/lonely/project.json" not in names
    for pid in ("m1", "m2"):
        spec = json.loads(names[f"projects/{pid}/project.json"])
        assert spec["tags"] == [s["id"]]              # membership travels

    out = _import(base, _export(base, f"/api/sets/{s['id']}/export")).json()
    assert out["ok"] and out["kind"] == "set"
    assert out["set"] == {s["id"]: s["id"] + "-2"}    # set id deduped too
    assert out["projects"] == {"m1": "m1-2", "m2": "m2-2"}
    rows = _rows(base)
    new_sid = out["set"][s["id"]]
    assert rows["m1-2"]["tags"] == [new_sid]          # tags remapped to new id
    assert rows["m1"]["tags"] == [s["id"]]            # original untouched


def test_set_export_empty_and_missing(api):
    _, base = api
    s = _create_set(base, "Empty")
    zf, names = _read_zip(_export(base, f"/api/sets/{s['id']}/export"))
    assert json.loads(names["manifest.json"])["kind"] == "set"
    out = _import(base, _export(base, f"/api/sets/{s['id']}/export")).json()
    assert out["ok"] and out["projects"] == {} and out["set"]
    assert requests.get(base + "/api/sets/ghost/export", timeout=5).status_code == 404


# ----------------------------------------------------------------------
# 4. KAI surface: EXPORT / IMPORT
# ----------------------------------------------------------------------

def _kai(base, text):
    r = requests.post(base + "/kai", data=text.encode(),
                      headers={"Content-Type": "text/plain"}, timeout=30)
    assert r.status_code == 200, r.text
    return r.text


def test_kai_export_set_and_import(api):
    _, base = api
    _create(base, "k1"); _create(base, "k2")
    s = _create_set(base, "Kai Fleet")
    _add_members(base, s["id"], ["k1", "k2"])
    import tempfile, os
    dest = os.path.join(tempfile.mkdtemp(), "fleet.kaisen.zip")
    out = _kai(base, f"PACK SET {s['id']} to {dest}")     # PACK = EXPORT alias
    assert out.startswith("OK exported")
    out = _kai(base, f"UNPACK {dest}")                    # UNPACK = IMPORT alias
    assert out.startswith("OK imported set") and "k1-2" in out and "k2-2" in out
    rows = _rows(base)
    new_sid = next(iter([x["id"] for x in requests.get(base + "/api/sets", timeout=5)
                         .json()["sets"] if x["id"] != s["id"]]))
    assert rows["k1-2"]["tags"] == [new_sid]


def test_kai_export_import_project(api, tmp_path):
    _, base = api
    _create(base, "kai-proj", files={"original.c": "int main(){}\n"})
    dest = tmp_path / "kai-proj.kaisen.zip"
    out = _kai(base, f"EXPORT kai-proj to {dest}")
    assert out.startswith("OK exported") and dest.is_file()

    out = _kai(base, f"IMPORT {dest}")
    assert out.startswith("OK imported project") and "kai-proj-2" in out
    assert "kai-proj-2" in _rows(base)


def test_kai_import_missing_file(api):
    _, base = api
    out = _kai(base, "IMPORT /nonexistent/nope.kaisen.zip")
    assert out.startswith("ERR no such file")


# ----------------------------------------------------------------------
# 5. Export options: best payload + measured baseline
# ----------------------------------------------------------------------

BASELINE_SRC = "int main(){}\n"
CHAMPION_SRC = "int main(){ /* optimized sieve */ }\n"


def _create_with_baseline(base, pid):
    """Project whose spec names original.c as data.baseline_source."""
    spec = _spec(pid, pid)
    spec["data"] = {"baseline_source": "original.c"}
    spec["files"] = {"original.c": BASELINE_SRC, "harness/build.py": "print(1)\n"}
    r = requests.post(base + "/api/projects", json={"id": pid, "spec": spec}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()["project"]


def _give_champion(registry, pid, body, fitness, metrics, generation):
    """Simulate engine state: best/ holds the champion, state.json its score."""
    pdir = registry.root / pid
    (pdir / "best").mkdir(exist_ok=True)
    champ = pdir / "best" / "program.c"
    champ.write_text(body)
    state = {"best": {"fitness": fitness, "metrics": metrics,
                      "code_path": str(champ), "generation": generation}}
    (pdir / "state.json").write_text(json.dumps(state))


def _append_history(registry, pid, entry):
    pdir = registry.root / pid
    st = json.loads((pdir / "state.json").read_text())
    st.setdefault("history", []).append(entry)
    (pdir / "state.json").write_text(json.dumps(st))


def test_export_default_includes_best_payload(api, registry):
    """Default export ships best/ + meta.json (score/provenance); runs and
    state.json still never travel."""
    _, base = api
    _create_with_baseline(base, "beta")
    # champion has improved OVER the baseline -> no measured baseline exists
    _give_champion(registry, "beta", CHAMPION_SRC, 0.9, {"ms": 400}, 12)

    zf, names = _read_zip(_export(base, "/api/projects/beta/export"))
    assert names["projects/beta/best/program.c"] == CHAMPION_SRC.encode()
    meta = json.loads(names["projects/beta/best/meta.json"])
    assert meta["fitness"] == 0.9 and meta["metrics"] == {"ms": 400}
    assert meta["generation"] == 12 and meta["code_path"] == "best/program.c"
    assert "projects/beta/baseline.json" not in names      # never measured
    assert names["projects/beta/original.c"] == BASELINE_SRC.encode()
    assert not any(n.endswith("state.json") or "/runs/" in n for n in names)


def test_export_opt_out_excludes_best_and_baseline(api, registry):
    """include_best=0 -> project + baseline only; include_baseline=0 -> bare
    pipeline definition (no baseline source file either)."""
    _, base = api
    _create_with_baseline(base, "gamma")
    # champion IS the baseline: state's score is also its measured baseline
    _give_champion(registry, "gamma", BASELINE_SRC, 1.0, {"ms": 800}, 1)

    names = _read_zip(_export(base, "/api/projects/gamma/export?include_best=0"))[1]
    assert not any("/best/" in n for n in names)           # no best payload
    assert names["projects/gamma/original.c"] == BASELINE_SRC.encode()
    mb = json.loads(names["projects/gamma/baseline.json"])  # measured one still travels (auto)
    assert mb["source"] == "original.c" and mb["fitness"] == 1.0
    assert mb["metrics"] == {"ms": 800} and mb["generation"] == 1

    names = _read_zip(_export(base, "/api/projects/gamma/export?include_baseline=0"))[1]
    assert "projects/gamma/original.c" not in names        # bare definition
    assert "projects/gamma/baseline.json" not in names
    assert names["projects/gamma/best/program.c"] == BASELINE_SRC.encode()
    assert json.loads(names["projects/gamma/best/meta.json"])["fitness"] == 1.0


def test_export_measured_baseline_flag_respected(api, registry):
    """baseline_measured: auto ships the record when it exists, no never
    ships it, yes errors when it does not exist."""
    _, base = api
    _create_with_baseline(base, "delta")
    _give_champion(registry, "delta", BASELINE_SRC, 1.0, {"ms": 800}, 1)

    names = _read_zip(_export(base, "/api/projects/delta/export"))[1]
    assert json.loads(names["projects/delta/baseline.json"])["fitness"] == 1.0
    names = _read_zip(_export(base, "/api/projects/delta/export?baseline_measured=no"))[1]
    assert "projects/delta/baseline.json" not in names
    r = requests.get(base + "/api/projects/delta/export?baseline_measured=yes", timeout=10)
    assert r.status_code == 200, r.text
    _, names = _read_zip(r.content)
    assert json.loads(names["projects/delta/baseline.json"])["fitness"] == 1.0

    # A project whose champion already improved: the measured baseline is
    # only recoverable from a recorded baseline_reeval entry.
    _give_champion(registry, "delta", CHAMPION_SRC, 0.5, {"ms": 400}, 9)
    r = requests.get(base + "/api/projects/delta/export?baseline_measured=yes", timeout=10)
    assert r.status_code == 400 and "measured baseline" in r.json()["error"]

    # ... until a re-evaluation of the (changed) source is on record.
    _append_history(registry, "delta", {"generation": 15, "outcome": "baseline_reeval",
                                        "fitness": 0.6, "metrics": {"ms": 660}})
    names = _read_zip(_export(base, "/api/projects/delta/export"))[1]
    mb = json.loads(names["projects/delta/baseline.json"])
    assert mb["fitness"] == 0.6 and mb["metrics"] == {"ms": 660}
    assert mb["generation"] == 15 and mb["source"] == "original.c"


def test_export_rejects_bad_options(api):
    _, base = api
    _create(base, "badopts")
    r = requests.get(base + "/api/projects/badopts/export?include_best=banana", timeout=10)
    assert r.status_code == 400 and "include_best" in r.json()["error"]
    r = requests.get(base + "/api/projects/badopts/export?baseline_measured=sometimes", timeout=10)
    assert r.status_code == 400 and "baseline_measured" in r.json()["error"]
    # yes without a baseline is contradictory
    r = requests.get(base + "/api/projects/badopts/export?include_baseline=0&baseline_measured=yes", timeout=10)
    assert r.status_code == 400


def test_set_export_inherits_options_for_every_member(api, registry):
    _, base = api
    _create_with_baseline(base, "s1"); _create(base, "s2")
    _give_champion(registry, "s1", BASELINE_SRC, 1.0, {"ms": 800}, 1)
    s = _create_set(base, "Fleet")
    _add_members(base, s["id"], ["s1", "s2"])

    zf, names = _read_zip(_export(base, f"/api/sets/{s['id']}/export"))
    assert json.loads(names["manifest.json"])["kind"] == "set"
    # every member inherits the full default: s1 ships its best payload,
    # s2 (never ran) ships definition only — nothing crashes.
    assert names["projects/s1/best/program.c"] == BASELINE_SRC.encode()
    assert json.loads(names["projects/s1/best/meta.json"])["fitness"] == 1.0
    assert json.loads(names["projects/s1/baseline.json"])["fitness"] == 1.0
    assert "projects/s2/project.json" in names
    assert not any(n.startswith("projects/s2/best/") for n in names)

    names = _read_zip(_export(base, f"/api/sets/{s['id']}/export?include_best=0"))[1]
    # no best payload anywhere; s1's measured baseline still travels (auto —
    # it is independent of the champion data), s2 carries definition only.
    assert not any("/best/" in n for n in names)
    assert json.loads(names["projects/s1/baseline.json"])["fitness"] == 1.0
    assert "projects/s1/project.json" in names and "projects/s2/project.json" in names


# ----------------------------------------------------------------------
# 6. Import tolerates bundles with and without the new payloads
# ----------------------------------------------------------------------

def test_import_old_style_bundle(api):
    """A pre-feature bundle (manifest + spec + plain files, no best/ or
    baseline.json) still imports cleanly."""
    _, base = api
    spec = _spec("oldstyle")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"kind": "project", "version": 1, "id": "oldstyle", "name": "oldstyle"}))
        zf.writestr("projects/oldstyle/project.json", json.dumps(spec))
        zf.writestr("projects/oldstyle/original.c", BASELINE_SRC)
    r = _import(base, buf.getvalue())
    assert r.status_code == 200 and r.json()["ok"], r.text
    rows = _rows(base)
    assert "oldstyle" in rows and rows["oldstyle"]["tags"] == []


def test_import_full_export_lands_best_data(api, registry):
    """A default (full) export imports: the champion data lands under the
    RENAMEDED id (never overwriting an existing best), twice."""
    _, base = api
    _create_with_baseline(base, "eta")
    _give_champion(registry, "eta", BASELINE_SRC, 1.0, {"ms": 800}, 1)
    data = _export(base, "/api/projects/eta/export")

    out = _import(base, data).json()
    assert out["ok"] and out["projects"] == {"eta": "eta-2"}
    root = registry.root / "eta-2"
    assert (root / "best" / "program.c").read_text() == BASELINE_SRC
    assert json.loads((root / "best" / "meta.json").read_text())["fitness"] == 1.0
    assert json.loads((root / "baseline.json").read_text())["generation"] == 1
    # the original project's runtime data is untouched
    assert (registry.root / "eta" / "best" / "program.c").read_text() == BASELINE_SRC

    out2 = _import(base, data).json()        # no overwrite: -3, same payload lands again
    assert out2["projects"] == {"eta": "eta-3"}
    assert (registry.root / "eta-3" / "best" / "meta.json").is_file()
