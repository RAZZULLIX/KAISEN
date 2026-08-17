# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Shared low-level helpers: JSON IO (atomic), hashing, formatting, process
supervision with timeout / memory cap / process-tree kill."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import select
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def load_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        # Tolerate whole-line `#` comments (config.example.json documents
        # the environment variables above the JSON block).
        lines = [ln for ln in text.splitlines(keepends=True) if not ln.lstrip().startswith("#")]
        return json.loads("".join(lines))
    except Exception:
        return default


def save_json(path: str | Path, data: Any) -> None:
    """Atomic write: temp file in same dir + rename, so a crash never
    leaves a half-written state file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def file_sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def format_bytes(n: Optional[float]) -> str:
    if n is None:
        return "N/A"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def get_process_tree_rss_bytes(proc: subprocess.Popen) -> int:
    if psutil is None:
        return 0
    try:
        p = psutil.Process(proc.pid)
        total = p.memory_info().rss
        for child in p.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0


def kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

class RunAbort(Exception):
    """Raised by a run_subprocess progress callback to kill the command
    early (e.g. a candidate already provably worse than the champion)."""


def run_subprocess(
    cmd: List[str],
    timeout: Optional[float] = None,
    memory_limit_bytes: Optional[int] = None,
    cwd: Optional[str | Path] = None,
    env: Optional[Dict[str, str]] = None,
    poll_interval: float = 0.5,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    progress_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a command with supervision. Returns a dict:
    {ok, returncode, stdout, stderr, timed_out, mem_exceeded, seconds, max_rss_bytes}.

    Pipes are drained incrementally (no 64KiB pipe-fill deadlock) and,
    when `progress` is given, it is called every poll cycle with
    {"elapsed": seconds, "rss": bytes, "live": {key: value}} where `live`
    collects key=value fields from the most recent output line prefixed
    with `progress_token` (the per-project live-telemetry protocol).
    """
    start = time.time()
    max_rss = 0
    live: Dict[str, str] = {}
    out_parts: List[bytes] = []
    err_parts: List[bytes] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            bufsize=0,
        )
    except Exception as e:
        return {
            "ok": False, "returncode": None, "stdout": "", "stderr": str(e),
            "timed_out": False, "mem_exceeded": False,
            "seconds": time.time() - start, "max_rss_bytes": 0,
        }

    fds: Dict[int, str] = {}
    line_bufs: Dict[str, bytes] = {}
    for name, pipe in (("out", proc.stdout), ("err", proc.stderr)):
        if pipe is not None:
            fd = pipe.fileno()
            fds[fd] = name
            line_bufs[name] = b""

    def scan_line(line: bytes) -> None:
        if not progress_token:
            return
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith(progress_token):
            return
        for tok in text[len(progress_token):].split():
            if "=" not in tok:
                continue
            key, _, val = tok.partition("=")
            if key:
                live[key] = val

    def drain(timeout_s: float = 0.0) -> None:
        while fds:
            try:
                ready, _, _ = select.select(list(fds), [], [], timeout_s)
            except (OSError, ValueError):
                return
            if not ready:
                return
            for fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except (OSError, BlockingIOError):
                    continue
                name = fds.get(fd)
                if name is None:
                    continue
                if chunk:
                    line_bufs[name] += chunk
                    *lines, line_bufs[name] = line_bufs[name].split(b"\n")
                    for line in lines:
                        scan_line(line)
                        if name == "out":
                            out_parts.append(line + b"\n")
                        else:
                            err_parts.append(line + b"\n")
                else:
                    # EOF on this pipe.
                    fds.pop(fd, None)
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    timed_out = False
    mem_exceeded = False
    aborted = False
    try:
        while True:
            drain()
            rss = get_process_tree_rss_bytes(proc)
            if rss > max_rss:
                max_rss = rss
            elapsed = time.time() - start
            if progress is not None:
                try:
                    progress({"elapsed": elapsed, "rss": rss, "live": dict(live), "pid": proc.pid})
                except RunAbort:
                    aborted = True
                    kill_process_tree(proc)
                    drain(2.0)
                    break
                except Exception:
                    pass
            exited = proc.poll() is not None
            if exited and not fds:
                break
            if timeout is not None and elapsed > timeout:
                timed_out = True
                kill_process_tree(proc)
                drain(2.0)
                break
            if memory_limit_bytes is not None and rss > memory_limit_bytes:
                mem_exceeded = True
                kill_process_tree(proc)
                drain(2.0)
                break
            time.sleep(poll_interval)
    except Exception:
        kill_process_tree(proc)
        drain(2.0)
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        for fd in list(fds):
            try:
                os.close(fd)
            except OSError:
                pass

    stdout = "".join(p.decode("utf-8", "replace") for p in out_parts)
    stderr = "".join(p.decode("utf-8", "replace") for p in err_parts)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "mem_exceeded": mem_exceeded,
        "aborted": aborted,
        "max_rss_bytes": max_rss,
    }
