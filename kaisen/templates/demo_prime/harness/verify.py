#!/usr/bin/env python3
import subprocess
import sys

# (n, expected count of primes below n)
KNOWN = [(10, 4), (100, 25), (1000, 168), (10000, 1229), (100000, 9592)]


def main():
    if len(sys.argv) != 2:
        print("Usage: verify.py <artifact>", file=sys.stderr)
        sys.exit(1)
    artifact = sys.argv[1]
    for n, expected in KNOWN:
        try:
            r = subprocess.run([artifact, str(n)], capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            print(f"timeout for n={n}", file=sys.stderr)
            sys.exit(1)
        if r.returncode != 0:
            print(f"exit {r.returncode} for n={n}", file=sys.stderr)
            sys.exit(1)
        tokens = r.stdout.decode(errors="replace").split()
        if not tokens:
            print(f"no output for n={n}", file=sys.stderr)
            sys.exit(1)
        try:
            got = int(tokens[-1])
        except ValueError:
            print(f"unparseable output for n={n}: {r.stdout[:120]!r}", file=sys.stderr)
            sys.exit(1)
        if got != expected:
            print(f"wrong count for n={n}: got {got}, expected {expected}", file=sys.stderr)
            sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
