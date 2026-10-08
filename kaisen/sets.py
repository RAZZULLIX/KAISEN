# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Sets — named, loadable bundles of projects (workspaces).  See docs/SETS.md.

A set is a TAG: projects carry `tags: [set-id, ...]` in their spec and can
belong to several sets at once.  The set DEFINITIONS (name, description)
live in sets.json; MEMBERSHIP lives with the projects, so deleting a
project can never leave an orphan reference behind.

The ACTIVE set is the dashboard's workspace: while a set is active only its
projects are visible and only their engines may run.  The file lives NEXT
TO config.json (repo root in production, temp dir in tests) — the same
convention as engine_pool.json, so tests stay hermetic.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .projects import SPEC_FILE
from .util import load_json, save_json

_IDENT = re.compile(r"[a-z0-9_-]+")


def slugify(name: str) -> str:
    """Display name -> set id ([a-z0-9_-]+), or '' when nothing usable."""
    s = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:48].strip("-")


class SetRegistry:
    """Set definitions + the active workspace.  The server process is the
    single writer; every mutation saves immediately."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # -- storage ----------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        data = load_json(self.path, None)
        if not isinstance(data, dict):
            return {"version": 1, "active": None, "sets": {}}
        data.setdefault("version", 1)
        data.setdefault("active", None)
        if not isinstance(data.get("sets"), dict):
            data["sets"] = {}
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        save_json(self.path, data)

    # -- queries ----------------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        data = self._load()
        return [
            {"id": sid,
             "name": d.get("name", sid),
             "description": d.get("description", ""),
             "created_at": d.get("created_at")}
            for sid, d in data["sets"].items()
        ]

    def get(self, set_id: str) -> Optional[Dict[str, Any]]:
        d = self._load()["sets"].get(set_id)
        return None if d is None else {"id": set_id, **d}

    def exists(self, set_id: str) -> bool:
        return set_id in self._load()["sets"]

    def active(self) -> Optional[str]:
        """The active workspace id, or None (default).  An active id whose
        set was deleted out-of-band falls back to the default workspace."""
        data = self._load()
        a = data.get("active")
        return a if a and a in data["sets"] else None

    # -- mutations ----------------------------------------------------------

    def create(self, name: str, description: str = "") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("set name is required")
        base = slugify(name)
        if not base or not _IDENT.fullmatch(base):
            raise ValueError("set name must contain letters or digits")
        data = self._load()
        sid, n = base, 2
        while sid in data["sets"]:
            sid = f"{base}-{n}"
            n += 1
        data["sets"][sid] = {"name": name,
                             "description": (description or "").strip(),
                             "created_at": time.time()}
        self._save(data)
        return {"id": sid, **data["sets"][sid]}

    def update(self, set_id: str, name: Optional[str] = None,
               description: Optional[str] = None) -> Dict[str, Any]:
        """Edit metadata only — the id is immutable, tags reference it."""
        data = self._load()
        if set_id not in data["sets"]:
            raise KeyError(f"set '{set_id}' not found")
        if name is not None and name.strip():
            data["sets"][set_id]["name"] = name.strip()
        if description is not None:
            data["sets"][set_id]["description"] = description.strip()
        self._save(data)
        return {"id": set_id, **data["sets"][set_id]}

    def delete(self, set_id: str) -> None:
        """Remove the definition.  Membership is stripped by the caller
        (strip_tag) — deleting a set NEVER deletes projects."""
        data = self._load()
        if set_id not in data["sets"]:
            raise KeyError(f"set '{set_id}' not found")
        del data["sets"][set_id]
        if data.get("active") == set_id:
            data["active"] = None
        self._save(data)

    def set_active(self, set_id: Optional[str]) -> Optional[str]:
        data = self._load()
        if set_id:
            if set_id not in data["sets"]:
                raise KeyError(f"set '{set_id}' not found")
            data["active"] = set_id
        else:
            data["active"] = None
        self._save(data)
        return data["active"]


# ---------------------------------------------------------------------------
# Membership — tags live in project.json; write + reload so a live engine
# picks the change up at its next generation (same rule as any spec edit).
# ---------------------------------------------------------------------------

def project_tags(project) -> List[str]:
    tags = project.spec.get("tags") or []
    return [t for t in tags if isinstance(t, str) and t]


def set_members(set_id: str, registry) -> List[str]:
    """Project ids tagged with this set (real registry only — temp projects
    are never taggable)."""
    return [row["id"] for row in registry.list()
            if set_id in (row.get("tags") or [])]


def _write_tags(project, tags: List[str]) -> None:
    spec = load_json(project.path / SPEC_FILE, {})
    spec["tags"] = tags
    save_json(project.path / SPEC_FILE, spec)
    project.reload()


def add_tag(project, set_id: str) -> List[str]:
    tags = project_tags(project)
    if set_id not in tags:
        tags.append(set_id)
        _write_tags(project, tags)
    return tags


def remove_tag(project, set_id: str) -> List[str]:
    tags = project_tags(project)
    kept = [t for t in tags if t != set_id]
    if len(kept) != len(tags):
        _write_tags(project, kept)
    return kept


def strip_tag(set_id: str, registry) -> List[str]:
    """Remove a set tag from every project (set deletion).  Returns the
    project ids that actually carried the tag."""
    touched = []
    for pid in set_members(set_id, registry):
        p = registry.get(pid)
        if p is not None:
            remove_tag(p, set_id)
            touched.append(pid)
    return touched
