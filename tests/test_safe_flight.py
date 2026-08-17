"""SAFE FLIGHT — the whole autonomous loop, end to end, real processes.

One real ProjectEngine (producer + worker pool + pipeline) driven by a
fake LLM server that serves a scripted candidate sequence. This proves
the nine flight-safety properties mechanically:

  1. autonomous candidate generation (only LLM calls drive the loop)
  2. execution of candidates (real gcc build + real artifact runs)
  3. objective external to the candidate (score = harness wall-time;
     a candidate that lies about its rounds cannot win)
  4. automatic measurement (parse rules -> metrics)
  5. automatic selection (fitness + hysteresis -> champion)
  6. winner becomes next baseline (champion code feeds the next prompt)
  7. repetition without human intervention (generations keep flowing)
  8. verification separate from optimization (wrong output rejected,
     never champion)
  9. user-supplied harness (spec declares build/verify/score programs)
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kaisen.config import FrameworkConfig
from kaisen.engine import ProjectEngine
from kaisen.llm import ModelOrchestrator
from kaisen.projects import ProjectRegistry

# ----------------------------------------------------------------------
# scripted candidates (markers make winner identity provable)
# ----------------------------------------------------------------------

C_BROKEN = "int main(void) { this is not valid C at all !!! }\n"

C_SLOW = """
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* SLOW_V1 */
int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "verify") == 0) {
        long sum = 0; for (long i = 1; i <= 1000; i++) sum += i;
        printf("RESULT %ld\\n", sum); return 0;
    }
    volatile long s = 0;
    for (long i = 0; i < 200000000L; i++) s += i * 2654435761L;
    printf("ROUNDS 200000000\\nCHECKSUM %ld\\n", s);
    return 0;
}
"""

C_FAST = """
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* FAST_V1 */
int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "verify") == 0) {
        long sum = 0; for (long i = 1; i <= 1000; i++) sum += i;
        printf("RESULT %ld\\n", sum); return 0;
    }
    volatile long s = 0;
    for (long i = 0; i < 60000000L; i++) s += i * 2654435761L;
    printf("ROUNDS 60000000\\nCHECKSUM %ld\\n", s);
    return 0;
}
"""

C_WRONG = """
#include <stdio.h>
#include <string.h>
/* WRONG_V1 — correct-looking but outputs garbage in verify mode */
int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "verify") == 0) {
        printf("RESULT 123456\\n"); return 0;
    }
    volatile long s = 0;
    for (long i = 0; i < 10000000L; i++) s += i;
    printf("ROUNDS 10000000\\nCHECKSUM %ld\\n", s);
    return 0;
}
"""

C_FASTER = """
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* FAST_V2 */
int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "verify") == 0) {
        long sum = 0; for (long i = 1; i <= 1000; i++) sum += i;
        printf("RESULT %ld\\n", sum); return 0;
    }
    volatile long s = 0;
    for (long i = 0; i < 30000000L; i++) s += i * 2654435761L;
    printf("ROUNDS 30000000\\nCHECKSUM %ld\\n", s);
    return 0;
}
"""

C_LIAR = """
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* LIAR_V1 — actually slow, but CLAIMS a billion rounds */
int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "verify") == 0) {
        long sum = 0; for (long i = 1; i <= 1000; i++) sum += i;
        printf("RESULT %ld\\n", sum); return 0;
    }
    volatile long s = 0;
    for (long i = 0; i < 500000000L; i++) s += i * 2654435761L;
    printf("ROUNDS 999999999\\nCHECKSUM %ld\\n", s);
    return 0;
}
"""

CANDIDATES = [C_BROKEN, C_SLOW, C_FAST, C_WRONG, C_FASTER, C_LIAR]

VERIFY_PY = """#!/usr/bin/env python3
import subprocess, sys
artifact = sys.argv[1]
r = subprocess.run([artifact, "verify"], capture_output=True, text=True, timeout=60)
out = (r.stdout or "") + (r.stderr or "")
if "RESULT 500500" not in out:
    print("FAIL: expected RESULT 500500")
    sys.exit(1)
print("OK")
"""

SCORE_PY = """#!/usr/bin/env python3
import subprocess, sys, time
artifact = sys.argv[1]
t0 = time.perf_counter()
r = subprocess.run([artifact, "0"], capture_output=True, text=True, timeout=300)
elapsed_ms = (time.perf_counter() - t0) * 1000.0
print(f"KAISEN_PROGRESS time_ms={elapsed_ms:.2f}")
print(f"time_ms={elapsed_ms:.2f}")
"""

SPEC = {
    "id": "safeflight", "name": "SafeFlight",
    "language": "c",
    "artifact_name": "program",
    "steps": {
        "build": {"program": "gcc",
                  "args": ["-O2", "{candidate}", "-o", "{artifact}"],
                  "timeout": 60},
        "verify": [{"program": "harness/verify.py", "args": ["{artifact}"],
                    "timeout": 60}],
        "score": [{"program": "harness/score.py", "args": ["{artifact}"],
                   "timeout": 300,
                   "parse": [{"type": "regex",
                              "pattern": "time_ms=(?P<time_ms>[\\d.]+)"}]}],
    },
    "metrics": {"time_ms": {"direction": "lower", "weight": 1.0}},
    "select": {"hysteresis": 1.0},
    "prompts": {"goal": "a C program: verify mode prints the sum 1..1000, "
                        "score mode loops a workload"},
}


class FakeLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.server.lock:
            self.server.requests += 1
            idx = min(self.server.requests - 1, len(CANDIDATES) - 1)
        code = CANDIDATES[idx]
        payload = json.dumps({"content": "```c\n" + code + "\n```"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
        time.sleep(0.05)
        self.wfile.write(b'data: {"content": "", "stop": true}\n\n')

    def log_message(self, *args):
        pass


class FakeLLMServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLMHandler)
        self.httpd.lock = threading.Lock()   # the handler reads server.lock
        self.httpd.requests = 0              # ... and server.requests
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def requests(self) -> int:
        return self.httpd.requests

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_safe_flight(tmp_path):
    registry = ProjectRegistry(tmp_path / "projects")
    project = registry.create("safeflight", SPEC)
    (project.path / "harness").mkdir(parents=True, exist_ok=True)
    (project.path / "harness" / "verify.py").write_text(VERIFY_PY)
    (project.path / "harness" / "score.py").write_text(SCORE_PY)

    cfg = FrameworkConfig(tmp_path / "config.json")
    (project.path / "harness" / "verify.py").chmod(0o755)
    (project.path / "harness" / "score.py").chmod(0o755)
    with FakeLLMServer() as fake:
        cfg.data["llm"]["servers"] = [{
            "id": "fake", "type": "llama",
            "url": f"http://127.0.0.1:{fake.port}/completion",
            "max_concurrent": 1, "enabled": True,
        }]
        cfg.data["llm"]["active_ids"] = ["fake"]
        orch = ModelOrchestrator(cfg)
        eng = ProjectEngine(project, orchestrator=orch, registry=registry,
                            worker_count=1)
        eng.start(multi=1, paused=False)
        try:
            deadline = time.time() + 240
            while time.time() < deadline:
                outs = [h.get("outcome") for h in eng.state.history]
                if ("build_fail" in outs and "verify_fail" in outs
                        and outs.count("ok") >= 3 and "valid" in outs):
                    break
                time.sleep(0.3)
            time.sleep(1.0)  # let the final result land
            outs = [h.get("outcome") for h in eng.state.history]

            # 1 — autonomous generation: the loop ran on LLM calls alone
            assert fake.requests >= 6
            # 7 — repetition without human intervention
            assert len(outs) >= 6 and eng.state.generation >= 6

            # 2 — execution really happened: broken code failed to build
            assert "build_fail" in outs
            # 8 — verification separate from optimization: wrong output
            # rejected and never became the champion
            assert "verify_fail" in outs

            # 3/4/5 — measurement + selection + objective-external:
            # the champion is the FASTEST measured candidate (FAST_V2);
            # the liar (fake ROUNDS) and the wrong-output one never won.
            champ = (project.best_dir / "program.c").read_text()
            assert "FAST_V2" in champ
            assert "LIAR_V1" not in champ
            assert "WRONG_V1" not in champ
            best = eng.state.best
            assert best["fitness"] is not None and best["metrics"]["time_ms"] > 0

            # 6 — winner becomes next baseline: the generation prompt
            # carries the champion code for the next round.
            prompt = eng._build_prompt(999, champ, project.best_dir / "program.c")
            assert "FAST_V2" in prompt

            # the scoreboard saw it all: attempts for the generation skill
            rows = orch.model_stats()
            gen_rows = [r for r in rows if r["skill"] == "generation"]
            assert gen_rows and gen_rows[0]["attempts"] >= 6
            assert gen_rows[0]["wins"] >= 3
        finally:
            eng.stop()


def test_failure_feedback_includes_constraint_violations(tmp_path):
    """The next generation's prompt carries explicit failure feedback for
    EVERY failure class — including constraint violations (the pitch's
    'model is told why its candidate was rejected' guarantee)."""
    from kaisen.memory import ProjectMemory
    from kaisen.state import ProjectState
    registry = ProjectRegistry(tmp_path / "projects")
    project = registry.create("fb", SPEC)
    st = ProjectState(project)
    st.append_history({"generation": 1, "outcome": "ok", "detail": "fine"})
    st.append_history({"generation": 2, "outcome": "constraint_violated",
                       "detail": "err=0.05 > constraint 0.01"})
    mem = ProjectMemory(project, st)
    blob = mem.build_history_blob()
    assert "[FAILURE FEEDBACK]" in blob
    assert "constraint_violated" in blob
    assert "err=0.05" in blob
