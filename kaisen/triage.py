# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Campaign bug triage — turn campaign_bugs.jsonl into an actionable report.

The campaign driver captures every stage failure (build_fail, verify_fail,
score_fail, ...) with its diagnostic detail. Triage groups those rows by
(project, outcome, signature) so a human or AI can tell at a glance:

  * CANDIDATE bugs — the expected noise: a generation's code is wrong on a
    specific fuzz input (FUZZ MISMATCH). The LLM sees these via its own
    feedback; they need no action unless they cluster suspiciously.
  * HARNESS bugs — something in the framework/toolchain is broken: every
    generation of a project fails the same stage the same way (e.g. build
    always fails = toolchain problem; score never parses = spec bug). These
    must be fixed, or the campaign silently burns generations.

Each group carries a reproduction pointer: the runs/gen_NNNN/ artifact dir
and the exact failing input, so any FUZZ MISMATCH can be re-run in seconds:

    <project>/harness/fuzz_verify.py  (rebuild candidate first, then run)

CLI:  python3 -m kaisen.triage [--bugs FILE] [--project PID] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import FRAMEWORK_ROOT

BUGS_FILE = FRAMEWORK_ROOT / "campaign_bugs.jsonl"
PROJECTS_DIR = FRAMEWORK_ROOT / "projects"

# a suspect flag is only actionable while the failure is CURRENT — a group
# whose last row is older than this was (presumably) already fixed and must
# not keep shouting in every report.
STALE_AFTER_S = 15 * 60

# signature extraction: what makes two failures "the same kind"
FUZZ_RE = re.compile(
    r"FUZZ (MISMATCH|CRASH|TIMEOUT) case=(\d+) tag=([\w:-]+) input=(\[[^\]]*\])")
BUILD_RE = re.compile(r"(error:|warning:.*error|cannot find|-v[^ ]* error)")


def load_bugs(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def signature(row: Dict[str, Any]) -> str:
    """A short identity for a failure kind — clusters across generations."""
    detail = str(row.get("detail") or "")
    m = FUZZ_RE.search(detail)
    if m:
        # same case+tag failing repeatedly = stable repro; the input is the
        # discriminator that matters for triage
        return f"fuzz:{m.group(1).lower()}:{m.group(3)}:{m.group(4)}"
    if row.get("outcome") == "build_fail":
        # first compiler error line, truncated
        for ln in detail.splitlines():
            if "error" in ln.lower():
                return "build:" + ln.strip()[:120]
        return "build:" + detail[:120].replace("\n", " ")
    if row.get("outcome") == "score_fail":
        return "score:" + detail[:120].replace("\n", " ")
    return str(row.get("outcome", "?")) + ":" + detail[:80].replace("\n", " ")


def gen_dir(project: str, iteration: Any) -> Optional[Path]:
    """runs/gen_NNNN for a project's iteration (best-effort naming)."""
    try:
        n = int(iteration)
    except (TypeError, ValueError):
        return None
    base = PROJECTS_DIR / project / "runs"
    if not base.is_dir():
        return None
    # The engine creates runs/gen_NNNNNN (6-digit, engine.py _make_gen_dir);
    # the legacy 4-digit and bare forms are probed only for old archives.
    for pat in (f"gen_{n:06d}", f"gen_{n:04d}", f"gen_{n}"):
        p = base / pat
        if p.is_dir():
            return p
    return None


def triage(bugs: List[Dict[str, Any]], project_filter: Optional[str] = None,
           limit: int = 25) -> Dict[str, Any]:
    groups: Dict[tuple, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0, "iterations": [], "sample_detail": "",
        "first_ts": None, "last_ts": None,
    })
    for row in bugs:
        pid = str(row.get("project") or "?")
        if project_filter and pid != project_filter:
            continue
        key = (pid, str(row.get("outcome") or "?"), signature(row))
        g = groups[key]
        g["count"] += 1
        g["iterations"].append(row.get("iteration"))
        if not g["sample_detail"]:
            g["sample_detail"] = str(row.get("detail") or "")[:400]
        ts = row.get("ts")
        g["first_ts"] = ts if (g["first_ts"] is None or (ts or 0) < g["first_ts"]) else g["first_ts"]
        g["last_ts"] = ts if (g["last_ts"] is None or (ts or 0) > g["last_ts"]) else g["last_ts"]

    rows_out: List[Dict[str, Any]] = []
    for (pid, outcome, sig), g in sorted(groups.items(),
                                         key=lambda kv: -kv[1]["count"]):
        iters = sorted(i for i in g["iterations"] if i is not None)
        repro = None
        if iters:
            d = gen_dir(pid, iters[-1])
            if d is not None:
                repro = str(d.relative_to(FRAMEWORK_ROOT))
        rows_out.append({
            "project": pid, "outcome": outcome, "signature": sig,
            "count": g["count"], "iterations": iters,
            "sample_detail": g["sample_detail"], "repro_dir": repro,
            "last_ts": g["last_ts"],
        })
    # harness-bug heuristic: a project whose ONLY failures are one repeated
    # stage signature on >= 3 consecutive generations is suspect — the LLM
    # cannot fix a broken build toolchain or an unparseable score step.
    per_project: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows_out:
        per_project[r["project"]].append(r)
    now = time.time()
    suspects: List[Dict[str, Any]] = []
    for pid, rs in per_project.items():
        if len(rs) == 1 and rs[0]["count"] >= 3:
            iters = rs[0]["iterations"]
            consecutive = max_run(iters)
            # freshness gate: an old streak is history, not a live suspect
            age = now - (rs[0]["last_ts"] or 0)
            if consecutive >= 3 and age <= STALE_AFTER_S:
                suspects.append({
                    "project": pid,
                    "outcome": rs[0]["outcome"],
                    "signature": rs[0]["signature"],
                    "count": rs[0]["count"],
                    "max_consecutive": consecutive,
                    "hint": ("HARNESS BUG? every attempt fails the same way — "
                             "check toolchain/spec, not candidates"),
                })
    return {
        "total_rows": len(bugs),
        "groups": rows_out[:limit],
        "harness_suspects": suspects,
    }


def max_run(sorted_iters: List[Any]) -> int:
    best = cur = 0
    prev = None
    for x in sorted_iters:
        try:
            xi = int(x)
        except (TypeError, ValueError):
            continue
        cur = cur + 1 if (prev is not None and xi == prev + 1) else 1
        best = max(best, cur)
        prev = xi
    return best


def render(report: Dict[str, Any]) -> str:
    lines = [f"BUGS {report['total_rows']} row(s) — "
             f"{len(report['groups'])} group(s) shown"]
    if report["harness_suspects"]:
        lines.append("")
        lines.append("!! HARNESS SUSPECTS (fix these first):")
    for g in report["groups"]:
        iters = g["iterations"]
        shown = ",".join(str(i) for i in iters[:8]) + ("…" if len(iters) > 8 else "")
        kind = "CANDIDATE" if g["outcome"] == "verify_fail" else "STAGE"
        age = (f" (last {int((time.time() - g['last_ts']) / 60)}m ago)"
               if g.get("last_ts") else "")
        lines.append(f"{g['project']}  {g['outcome']} x{g['count']} [{kind}]"
                     f"{age} gens={shown}")
        lines.append(f"    sig: {g['signature'][:160]}")
        detail = g["sample_detail"].replace("\n", " ⏎ ")
        lines.append(f"    e.g.: {detail[:240]}")
        if g["repro_dir"]:
            lines.append(f"    repro: rebuild candidate from {g['repro_dir']}, "
                         f"then run harness/fuzz_verify.py on it")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bugs", default=str(BUGS_FILE))
    ap.add_argument("--project", default=None)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    bugs = load_bugs(Path(args.bugs))
    report = triage(bugs, project_filter=args.project, limit=args.limit)
    if args.as_json:
        print(json.dumps(report, indent=1))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
