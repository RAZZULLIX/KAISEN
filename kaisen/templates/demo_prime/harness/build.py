#!/usr/bin/env python3
# Build step: compile (cacheable by ccache when engine.build_cache is on)
# then link.  Splitting the -c phase lets the engine reuse objects across
# generations instead of paying a full rebuild for a one-literal change.
import os
import subprocess
import sys


def main():
    if len(sys.argv) != 3:
        print("Usage: build.py <candidate> <artifact>", file=sys.stderr)
        sys.exit(1)
    candidate, artifact = sys.argv[1], sys.argv[2]
    obj = os.path.join(os.path.dirname(artifact), "program.o")
    compile_cmd = [
        "gcc", "-O3", "-std=c11", "-Wall", "-Werror", "-Wno-unused-result",
        "-c", candidate, "-o", obj,
    ]
    link_cmd = ["gcc", obj, "-o", artifact, "-lm"]
    r1 = subprocess.run(compile_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sys.stderr.write(r1.stderr.decode())
    if r1.returncode != 0:
        sys.exit(r1.returncode)
    r2 = subprocess.run(link_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sys.stderr.write(r2.stderr.decode())
    if r2.returncode != 0:
        sys.exit(r2.returncode)


if __name__ == "__main__":
    main()
