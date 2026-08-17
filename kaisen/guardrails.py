# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Guardrail system — the safety-critical layer of KAISEN.

Policy (strongest default, hardcoded):
  1. Every command the framework would execute (pipeline steps, harness
     programs, agent-suggested pipelines) passes through `check_command`.
  2. A HARD-CODED denylist of destructive patterns is always scanned
     against the full command line.  These patterns can NEVER be allowed
     by project config — only by the global safety-off path.
  3. Executables must be on the allowed launcher list, or absolute paths
     inside the project's own directory.  Shells (`sh`, `bash`, ...) are
     denied as launchers unless the project explicitly opts in.
  4. Per-project opt-out: project spec `guardrails.enabled=false` disables
     the policy for that project's commands only (never the denylist —
     see note below).
  5. Global safety-off: requires config.json safety.global_off=true AND
     env KAISEN_SAFETY_OFF=1.  Both must be set deliberately; the GUI can
     never toggle it.

Note: the HARD denylist is enforced even when a project has opted out —
it is the "never wipe someone's drive" floor.  Only the global switch
disables everything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config import FrameworkConfig, get_config

# ---------------------------------------------------------------------------
# HARD-CODED DESTRUCTIVE PATTERNS — never removable via project config.
# ---------------------------------------------------------------------------
HARD_DENY_SUBSTRINGS: List[str] = [
    # raw block devices
    "/dev/sd", "/dev/hd", "/dev/nvme", "/dev/vd", "/dev/xvd", "/dev/mmcblk",
    "/dev/mapper/", "/dev/disk/", "/dev/loop", "/dev/zero", "/dev/mem",
    # destructive utilities (any occurrence)
    "mkfs", "fdisk", "diskpart", "wipefs", "mkswap", "mkbootdisk",
    # dd writing to devices
    "of=/dev/",
    # fork bomb
    ":(){",
    # delete/format system-wide
    "--no-preserve-root", "format c:", "format /q", "rd /s /q", "rmdir /s",
    "rm -rf /", "rm -fr /", "rm -rf /*", "del /s /q", "del /f /s /q",
    # nuke from root with find
    "find / -delete", "find / -exec rm", "find / -exec rm", "xargs rm",
    # power / boot / firmware
    "shutdown", "reboot", "poweroff", "halt", "init 0", "init 6",
    "grub-install", "efibootmgr", "mkinitrd", "update-grub",
    # partition / fs mutation
    "parted", "gparted", "sfdisk", "cfdisk", "badblocks",
    # shell pipe-to-shell (arbitrary remote code)
    "curl | sh", "curl|sh", "wget | sh", "wget|sh", "curl -s", "wget -qO- | sh",
]

HARD_DENY_REGEX: List[str] = [
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-f[a-zA-Z]*r[a-zA-Z]*)\b",       # rm -rf
    r"\bdd\s+.*\bof=/?[^ ]+",                                                  # dd of=<file>
    r"\bchmod\s+-R\s+[0-7]{3,4}\s+/",                                          # chmod -R ... /
    r"\bchown\s+-R\s+[^ ]+\s+/",                                               # chown -R ... /
    r"\bmv\s+[^ ]+\s+/dev/null",                                               # mv ... /dev/null
    r"\b>+\s*/etc/(passwd|shadow|sudoers|fstab|hosts)",                        # clobber system files
    r"\btruncate\s+-s\s+\d+\s*/dev/",                                          # truncate devices
    r"\bsystemctl\s+(stop|disable|mask|enable)\s+",                            # break boot services
    r"\bgit\s+clean\s+-[a-z]*f[a-z]*d[a-z]*x?",                                # git clean -fdx
    r"\bgit\s+reset\s+--hard\s+[^ ]*\s*$",                                     # git reset --hard (no args = wipe)
    r"\bpython3?\s+.*\bos\.system\b",                                          # python os.system
    r"\bsh\s+-c\s+.*\brm\b",                                                   # shell rm
    r"\bpv\s+</dev/",                                                          # pv < /dev
    r"\bcat\s+/dev/\w+\s*>",                                                   # cat /dev -> file
    r"\bddrescue\b",                                                           # raw device copier
    r"\bshred\b",                                                              # secure delete
]

# Executables allowed as launcher by default.  Shells are deliberately NOT
# here: a shell launcher makes static command validation meaningless.
ALLOWED_LAUNCHERS = {
    "python3", "python", "gcc", "g++", "cc", "clang", "clang++", "make",
    "cmake", "ld", "ar", "objcopy", "cp", "mv", "mkdir", "rm", "touch",
    "cat", "chmod", "chown", "ln", "shutil",
}
# Note: `rm`/`mv`/`chmod`/`chown` remain allowed as launchers for benign use
# (cleaning a workdir); the denylist regexes above catch destructive forms.

_DENY_RE = [re.compile(p) for p in HARD_DENY_REGEX]


def _scan_denylist(command: str) -> Tuple[bool, str]:
    low = command.lower()
    for sub in HARD_DENY_SUBSTRINGS:
        if sub in low:
            return True, f"denied: forbidden token '{sub}'"
    for rx in _DENY_RE:
        m = rx.search(command)
        if m:
            return True, f"denied: forbidden pattern '{m.group(0)}'"
    return False, ""


def _resolve_executable(parts: List[str], project_dir: Path) -> Tuple[bool, str]:
    """Validate the executable of a command against the launcher policy."""
    if not parts:
        return False, "empty command"
    exe = parts[0]
    # Absolute path inside the project dir is trusted (user-authored harness).
    if exe.startswith("/") or exe.startswith("./") or exe.startswith("../"):
        p = Path(exe)
        try:
            resolved = p.resolve()
            proj_resolved = project_dir.resolve()
            if resolved == proj_resolved or proj_resolved in resolved.parents:
                return True, ""
        except Exception:
            pass
        return False, f"denied: executable outside project directory: {exe}"
    # Bare command must be on the allowlist.
    base = Path(exe).name
    if base in ALLOWED_LAUNCHERS:
        return True, ""
    return False, f"denied: launcher '{exe}' not allowed (shells and arbitrary binaries are blocked by default)"


def check_command(
    command: str | List[str],
    project: Dict[str, Any] | None = None,
    project_dir: str | Path | None = None,
    config: FrameworkConfig | None = None,
) -> Tuple[bool, str]:
    """Validate a command before execution.

    Returns (ok, reason).  `project` is the project spec dict; `project_dir`
    is the resolved project directory (used to trust user-authored harness
    programs).  `config` is the framework config.
    """
    cfg = config or get_config()

    if isinstance(command, str):
        cmd_str = command
        parts = command.split()
    else:
        parts = list(command)
        cmd_str = " ".join(parts)

    # ── Global safety-off (double action: config flag + env var) ──
    if cfg.global_safety_off:
        return True, "GLOBAL SAFETY OFF (config.json + KAISEN_SAFETY_OFF) — guardrails disabled"

    # ── Hard denylist — ALWAYS enforced (even if project opted out) ──
    bad, reason = _scan_denylist(cmd_str)
    if bad:
        return False, reason

    # ── Project-level opt-out ──
    guard = (project or {}).get("guardrails", {})
    if guard.get("enabled", True) is False:
        return True, "project guardrails disabled by project owner"

    # ── Launcher policy ──
    if project_dir is not None:
        ok, reason = _resolve_executable(parts, Path(project_dir))
        if not ok:
            return False, reason
    else:
        # Framework-level command with no project context: enforce launcher
        # allowlist only (python/gcc/etc. — all benign in the framework core).
        if parts and parts[0].startswith(("/", "./", "../")):
            return True, ""
        if parts and Path(parts[0]).name in ALLOWED_LAUNCHERS:
            return True, ""
        return False, f"denied: launcher '{parts[0] if parts else ''}' not allowed"

    # ── Project extra denylist (user-supplied, e.g. their own forbidden cmds) ──
    for extra in guard.get("deny_extra", []) or []:
        if extra in cmd_str:
            return False, f"denied: project rule '{extra}'"

    # ── Protected data files (NO-CHANGE policy) ──
    # `data.protected_files` lists project-relative paths that are input
    # data — the original must never be written, only copies may be
    # modified.  Deny any command that references such a file together
    # with a write operator (shell redirect, tee/dd/mv/cp/rm/touch/
    # chmod/chown/truncate/sed -i, ...).
    if project_dir is not None:
        proj = Path(project_dir)
        protected = (project or {}).get("data", {}).get("protected_files", []) or []
        write_ops = ("tee ", "dd of=", "truncate", "sed -i", "perl -i",
                     ">", ">>", "2>", "1>", ":>", "cat >")
        mutators = {"cp", "mv", "rm", "touch", "chmod", "chown", "truncate", "tee", "ln", "dd"}
        for rel in protected:
            rel = str(rel)
            target = (proj / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
            hit = False
            if str(target) in cmd_str or rel in cmd_str:
                hit = True
            if not hit:
                continue
            if any(op in cmd_str for op in write_ops):
                return False, f"denied: protected data file '{rel}' must never be modified (original data; work on copies)"
            if parts and Path(parts[0]).name in mutators:
                return False, f"denied: '{parts[0]}' may not touch protected data file '{rel}' (original data; work on copies)"

    return True, ""


def guardrail_state(config: FrameworkConfig | None = None) -> Dict[str, Any]:
    """Expose guardrail status for the GUI (read-only view)."""
    cfg = config or get_config()
    return {
        "global_off": cfg.global_safety_off,
        "global_off_configured": bool(cfg.safety.get("global_off")),
        "hard_deny_rules": len(HARD_DENY_SUBSTRINGS) + len(HARD_DENY_REGEX),
        "allowed_launchers": sorted(ALLOWED_LAUNCHERS),
    }
