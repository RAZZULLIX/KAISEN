#!/usr/bin/env python3
import subprocess
import sys
import time

N = 1000000
BUDGET = 5.0


def main():
    if len(sys.argv) != 2:
        print("Usage: score.py <artifact>", file=sys.stderr)
        sys.exit(1)
    artifact = sys.argv[1]
    rounds = 0
    start = time.perf_counter()
    last_report = start
    while True:
        try:
            r = subprocess.run([artifact, str(N)], capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            print("run timed out", file=sys.stderr)
            sys.exit(1)
        if r.returncode != 0:
            print("run failed", file=sys.stderr)
            sys.exit(1)
        rounds += 1
        now = time.perf_counter()
        if now - start >= BUDGET:
            break
        if now - last_report >= 1.0:
            avg_ms = (now - start) * 1000 / rounds
            print(f"KAISEN_PROGRESS time_ms={avg_ms:.1f} rounds={rounds}", flush=True)
            last_report = now
    total_ms = (time.perf_counter() - start) * 1000
    print(f"time_ms={total_ms / rounds:.1f}")
    print(f"rounds={rounds}")


if __name__ == "__main__":
    main()
