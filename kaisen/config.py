# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Framework-wide (general) configuration.

This is the "general part" of the config split: things that are shared across
all projects — GUI server, LLM server registry, worker limits, Telegram,
and the global safety switch.  Project-specific settings live in the
project spec (projects/<id>/project.json).

Layout:
  FRAMEWORK_ROOT        repo root (KAISEN/)
  CONFIG_FILE           config.json in the repo root
  PROJECTS_DIR          projects/
  PAGES_DIR             pages/
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict

from .util import load_json, save_json

FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = FRAMEWORK_ROOT / "config.json"
PROJECTS_DIR = FRAMEWORK_ROOT / "projects"
PAGES_DIR = FRAMEWORK_ROOT / "pages"
TEMP_ROOT = FRAMEWORK_ROOT / "temp"
SECRETS_FILE = FRAMEWORK_ROOT / "secrets.json"


DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        # Loopback by default: the dashboard, /kai and /api have no
        # authentication. Opt into LAN exposure deliberately by setting
        # "host": "0.0.0.0" in config.json (see README "Security").
        "host": "127.0.0.1",
        "port": 8080,
        # Optional server password ("" = no auth). When set, every page
        # and endpoint requires it (Bearer header / Basic auth prompt).
        # Env KAISEN_API_KEY wins.
        "api_key": "",
    },
    "llm": {
        "read_timeout": 1200,
        "connect_timeout": 15,
        "nodata_timeout": 120,
        "max_retries": 3,
        "retry_backoff": 2.0,
        # Active server ids (subset of "servers") — the checkbox selection.
        "active_ids": [],
        # Servers are managed live via the GUI; the list below is the
        # persisted registry.
        "servers": [
            {
                "id": "gpt-oss-20b",
                "type": "llama",
                "url": "http://127.0.0.1:8502/completion",
                "model": "gpt-oss-20b",
                "max_concurrent": 2,
                "params": {"temperature": 0.6},
                "timeout": 1200,
                "spawn_cmd": [],
            }
        ],
    },
    "workers": {
        "default_count": 1,
        "max_count": 32,
        "queue_size": 15,
        # Optional CPU pinning for worker processes, e.g. "0,2" or "1-3".
        # Empty = no pinning.  On a busy shared box, pin each worker to its
        # own core (or use `nice`) so score timings stop swinging with load.
        # See MANUAL "Quiet benchmarking".
        "affinity": "",
        # Run worker processes at lower priority (nice +10) so scorers don't
        # starve the dashboard/LLMs while the engines race.
        "quiet": False,
    },
    # Engine startup: start PAUSED by default — nothing generates until
    # the user presses play.  Set false to start generating immediately.
    "engine": {
        "start_paused": True,
        "default_multi": 1,
    },
    "telegram": {
        "token": "",
        "chat_id": "",
        "max_len": 4000,
        "trunc_len": 3900,
        "enabled": False,
    },
    # Global safety switch.  NEVER toggled from the GUI: requires editing
    # config.json AND setting KAISEN_SAFETY_OFF=1 in the environment.
    # Guardrails stay fully on unless BOTH are set.
    "safety": {
        "global_off": False,
    },
    # Auto-fix of compiler-suggested build errors ("did you forget to
    "autofix": {
        "build_enabled": True,
        # Compile-loop caps. max_tries = deterministic autofix turns before
        # giving up (the old MAX_FIX_TRIES); llm_repair_max = LLM repair
        # attempts per generation when llm_repair is on. Both overridable
        # per run via KAI: AUTOFIX tries <n> repair <n|off>.
        "max_tries": 5,
        # LAST-RESORT layer: when the deterministic fixer is exhausted and
        # the build still fails, up to llm_repair_max LLM repair passes may
        # rewrite the candidate. The reply is danger-scanned and re-runs
        # the FULL pipeline before it can count. Hardcoded guardrails
        # still apply; this gate only switches the layer off.
        "llm_repair": True,
        "llm_repair_max": 3,
    },
    # Auto-fix defaults apply to NEW projects; existing projects carry
    # their own skills.autofix_build (true | false | custom fixer path).
    # First-run onboarding: the wizard sets done=true when finished.
    "onboarding": {
        "done": False,
    },
    "debug_logs": True,
}


def resolve_secret(env_names, current_value: str = "") -> str:
    """Environment variables take precedence over config-file values, so
    secrets never have to live in config.json (which may be published)."""
    for name in env_names:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return (current_value or "").strip()


def load_secrets() -> Dict[str, Any]:
    """Read the local secrets file (gitignored, 0600). Never shipped."""
    return load_json(SECRETS_FILE, {}) or {}


def save_secret(section: str, key: str, value: str) -> None:
    """Upsert (or delete, when empty) one secret entry. The file is written
    with owner-only permissions (0600) — standard practice for local
    secrets; environment variables still take precedence at read time."""
    secrets = load_secrets()
    sec = secrets.setdefault(section, {})
    if value:
        sec[key] = value
    else:
        sec.pop(key, None)
    save_json(SECRETS_FILE, secrets)
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:  # pragma: no cover — best effort
        pass


class FrameworkConfig:
    """Thread-safe-ish holder for the general config (single writer: the
    GUI server / main process; readers may read concurrently)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else CONFIG_FILE
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        loaded = load_json(self.path, None)
        if isinstance(loaded, dict):
            self._deep_merge(self.data, loaded)
        # Secrets convenience (env-first; never required in config.json)
        self.github_token: str = os.environ.get("KAISEN_GITHUB_TOKEN", "")

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                FrameworkConfig._deep_merge(base[k], v)
            else:
                base[k] = v

    def save(self) -> None:
        save_json(self.path, self.data)

    # ---- typed accessors ----
    @property
    def server_host(self) -> str:
        return self.data["server"]["host"]

    @property
    def server_port(self) -> int:
        return int(self.data["server"]["port"])

    @property
    def llm(self) -> Dict[str, Any]:
        return self.data["llm"]

    @property
    def workers(self) -> Dict[str, Any]:
        return self.data["workers"]

    @property
    def engine(self) -> Dict[str, Any]:
        return self.data.setdefault("engine", {"start_paused": True})

    @property
    def engine_start_paused(self) -> bool:
        return bool(self.engine.get("start_paused", True))

    @property
    def telegram(self) -> Dict[str, Any]:
        return self.data["telegram"]

    @property
    def safety(self) -> Dict[str, Any]:
        return self.data["safety"]

    @property
    def autofix(self) -> Dict[str, Any]:
        return self.data.setdefault("autofix", {"build_enabled": True})

    @property
    def autofix_build_enabled(self) -> bool:
        return bool(self.data.get("autofix", {}).get("build_enabled", True))

    # ---- secrets (env-first) ----
    @property
    def telegram_token(self) -> str:
        return resolve_secret(["KAISEN_TG_TOKEN"], self.telegram.get("token", ""))

    @property
    def telegram_chat_id(self) -> str:
        return resolve_secret(["KAISEN_TG_CHAT_ID"], self.telegram.get("chat_id", ""))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def api_key_from_env(self, server_id: str) -> str:
        return resolve_secret(
            [f"KAISEN_SERVER_{server_id.upper()}_API_KEY", "KAISEN_OPENAI_API_KEY"],
            "",
        )

    def api_key_from_secrets(self, server_id: str) -> str:
        return str((load_secrets().get("llm") or {}).get(server_id, "") or "").strip()

    def api_key_source(self, server_id: str) -> str:
        """Where the key for a server lives: env | secrets | unset."""
        if self.api_key_from_env(server_id):
            return "env"
        if self.api_key_from_secrets(server_id):
            return "secrets"
        return "unset"

    def server_api_key(self, server_id: str, fallback: str = "") -> str:
        """Env → secrets.json → in-memory fallback (never config.json)."""
        return self.api_key_from_env(server_id) or self.api_key_from_secrets(server_id) or (fallback or "").strip()

    @property
    def global_safety_off(self) -> bool:
        """Guardrails are disabled only when BOTH the config flag and the
        environment variable are set — a deliberate double action."""
        env_off = os.environ.get("KAISEN_SAFETY_OFF", "").strip().lower() in ("1", "true", "yes")
        return bool(self.safety.get("global_off")) and env_off

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


_config: FrameworkConfig | None = None


def get_config() -> FrameworkConfig:
    global _config
    if _config is None:
        _config = FrameworkConfig()
    return _config


def set_config(cfg: FrameworkConfig) -> None:
    global _config
    _config = cfg
