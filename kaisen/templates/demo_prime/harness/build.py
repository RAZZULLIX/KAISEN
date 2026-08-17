#!/usr/bin/env python3
import subprocess
import sys


def main():
    if len(sys.argv) != 3:
        print("Usage: build.py <candidate> <artifact>", file=sys.stderr)
        sys.exit(1)
    candidate, artifact = sys.argv[1], sys.argv[2]
    cmd = [
        "gcc", "-O3", "-std=c11", "-Wall", "-Werror", "-Wno-unused-result",
        candidate, "-o", artifact, "-lm",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sys.stderr.write(result.stderr.decode())
    if result.returncode != 0:
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
