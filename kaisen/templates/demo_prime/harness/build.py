#!/usr/bin/env python3
# Build step: compile (cacheable by ccache when engine.build_cache is on)
# then link.  Splitting the -c phase lets the engine reuse objects across
# generations instead of paying a full rebuild for a one-literal change.
import os
import shutil
import subprocess
import sys

# Compiler resolution: prefer gcc (the template's documented toolchain),
# fall back to cc/clang so hosts with only a generic C compiler still build.
COMPILER_CANDIDATES = ("gcc", "cc", "clang")


def _fail_missing_compiler():
    if os.name == "nt":
        hints = (
            "  Windows + Miniconda/Anaconda:  conda install -c conda-forge gcc\n"
            "  Windows + MSYS2:               pacman -S mingw-w64-ucrt-x86_64-gcc\n"
            "  (then make sure the compiler's bin dir is on PATH)"
        )
    else:
        hints = (
            "  Debian/Ubuntu: apt install build-essential\n"
            "  Fedora/RHEL:   dnf groupinstall 'Development Tools'\n"
            "  macOS:         xcode-select --install"
        )
    print(
        "ERROR: no C compiler found on PATH (tried: " + ", ".join(COMPILER_CANDIDATES) + ").\n"
        "Install a C toolchain first, e.g.:\n" + hints,
        file=sys.stderr,
    )
    sys.exit(2)


def main():
    if len(sys.argv) != 3:
        print("Usage: build.py <candidate> <artifact>", file=sys.stderr)
        sys.exit(1)
    candidate, artifact = sys.argv[1], sys.argv[2]
    compiler = next((c for c in COMPILER_CANDIDATES if shutil.which(c)), None)
    if compiler is None:
        _fail_missing_compiler()
    obj = os.path.join(os.path.dirname(artifact), "program.o")
    compile_cmd = [
        compiler, "-O3", "-std=c11", "-Wall", "-Werror", "-Wno-unused-result",
        "-c", candidate, "-o", obj,
    ]
    link_cmd = [compiler, obj, "-o", artifact, "-lm"]
    try:
        r1 = subprocess.run(compile_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        print(f"ERROR: could not run compiler {compiler!r}: {e}", file=sys.stderr)
        sys.exit(2)
    sys.stderr.write(r1.stderr.decode())
    if r1.returncode != 0:
        sys.exit(r1.returncode)
    try:
        r2 = subprocess.run(link_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        print(f"ERROR: could not run compiler {compiler!r}: {e}", file=sys.stderr)
        sys.exit(2)
    sys.stderr.write(r2.stderr.decode())
    if r2.returncode != 0:
        sys.exit(r2.returncode)


if __name__ == "__main__":
    main()
