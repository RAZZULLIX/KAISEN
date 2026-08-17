# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""GitHub channel: publish the champion artifact + a project-defined README
to a repo.  Generic (project config supplies repo/branch/paths/readme
template)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from .config import get_config

API = "https://api.github.com"


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def is_configured(spec: Dict[str, Any]) -> bool:
    token = get_config().github_token
    return bool(token and spec.get("repo") and spec.get("branch") and spec.get("artifact_path"))


def get_file_sha(session: requests.Session, token: str, repo: str, branch: str, path: str) -> Optional[str]:
    r = session.get(f"{API}/repos/{repo}/contents/{path}", params={"ref": branch}, headers=_headers(token), timeout=30)
    if r.status_code != 200:
        return None
    data = r.json()
    return data.get("sha")


def update_file(
    session: requests.Session, token: str, repo: str, branch: str, path: str,
    content: bytes, message: str, sha: Optional[str],
) -> bool:
    payload: Dict[str, Any] = {
        "message": message,
        "branch": branch,
        "content": __import__("base64").b64encode(content).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    r = session.put(f"{API}/repos/{repo}/contents/{path}", json=payload, headers=_headers(token), timeout=60)
    return r.status_code in (200, 201)


def upload_best(
    spec: Dict[str, Any],
    artifact_bytes: bytes,
    readme_text: str,
    generation: int,
    token: Optional[str] = None,
) -> bool:
    """Publish the champion artifact and README to the configured repo."""
    token = token or get_config().github_token
    if not is_configured(spec):
        return False
    session = requests.Session()
    try:
        repo = spec["repo"]
        branch = spec["branch"]
        art_path = spec["artifact_path"]
        readme_path = spec.get("readme_path", "README.md")
        msg = f"New best at iteration {generation}"
        sha = get_file_sha(session, token, repo, branch, art_path)
        ok1 = update_file(session, token, repo, branch, art_path, artifact_bytes, msg, sha)
        sha2 = get_file_sha(session, token, repo, branch, readme_path)
        ok2 = update_file(session, token, repo, branch, readme_path, readme_text.encode("utf-8"), msg, sha2)
        return bool(ok1 and ok2)
    except Exception:
        return False
    finally:
        session.close()
