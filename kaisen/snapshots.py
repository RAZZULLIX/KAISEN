# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Project & config snapshots — one-click revert for anything the agents
or the user change. The "unified standard we can always revert back to".

Layout: .kaisen_snapshots/
  <project_id>/<snap_id>/…   — full project copy minus runs/best/scratch
  global/<snap_id>/…         — config.json + ui_prefs.json copies
  meta.json per snapshot: {created, reason, kind}

Note: seen_hashes.json is DELIBERATELY not part of a snapshot — reverting
a project to an earlier state does NOT reset the visited set, so the engine
never re-evaluates code it has already scored (the memory that a candidate
was tried survives the undo of everything else).
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import FRAMEWORK_ROOT

SNAPSHOT_DIR = FRAMEWORK_ROOT / ".kaisen_snapshots"
KEEP = 25
_IGNORE = {"runs", "best", "state.json", "results.csv", "seen_hashes.json", ".kaisen_scripts", "__pycache__"}


def _meta(created: float, reason: str, kind: str) -> Dict[str, Any]:
    return {"created": created, "reason": reason, "kind": kind}


def take_project_snapshot(project_dir: Path, reason: str) -> str:
    snap_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    dest = SNAPSHOT_DIR / project_dir.name / snap_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project_dir, dest, ignore=shutil.ignore_patterns(*_IGNORE))
    (dest / ".snap_meta.json").write_text(json.dumps(_meta(time.time(), reason, "project")), encoding="utf-8")
    _prune(SNAPSHOT_DIR / project_dir.name)
    return snap_id


def list_project_snapshots(project_id: str) -> List[Dict[str, Any]]:
    root = SNAPSHOT_DIR / project_id
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        meta_p = d / ".snap_meta.json"
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            meta = {"created": 0, "reason": "", "kind": "project"}
        out.append({"id": d.name, **meta})
    return out


def restore_project_snapshot(project_dir: Path, snap_id: str) -> bool:
    src = SNAPSHOT_DIR / project_dir.name / snap_id
    if not src.exists():
        return False
    for child in project_dir.iterdir():
        if child.name in _IGNORE or child.name == "project.json":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    for child in src.iterdir():
        if child.name == ".snap_meta.json":
            continue
        if child.is_dir():
            shutil.copytree(child, project_dir / child.name, dirs_exist_ok=True)
        else:
            shutil.copy2(child, project_dir / child.name)
    return True


def take_config_snapshot(reason: str) -> str:
    snap_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    dest = SNAPSHOT_DIR / "global" / snap_id
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "ui_prefs.json"):
        src = FRAMEWORK_ROOT / name
        if src.exists():
            shutil.copy2(src, dest / name)
    (dest / ".snap_meta.json").write_text(json.dumps(_meta(time.time(), reason, "config")), encoding="utf-8")
    _prune(SNAPSHOT_DIR / "global")
    return snap_id


def list_config_snapshots() -> List[Dict[str, Any]]:
    root = SNAPSHOT_DIR / "global"
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        try:
            meta = json.loads((d / ".snap_meta.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {"created": 0, "reason": "", "kind": "config"}
        out.append({"id": d.name, **meta})
    return out


def restore_config_snapshot(snap_id: str) -> bool:
    src = SNAPSHOT_DIR / "global" / snap_id
    if not src.exists():
        return False
    for name in ("config.json", "ui_prefs.json"):
        s = src / name
        if s.exists():
            shutil.copy2(s, FRAMEWORK_ROOT / name)
    return True


def _prune(root: Path) -> None:
    try:
        snaps = sorted(root.iterdir(), reverse=True)
        for d in snaps[KEEP:]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
