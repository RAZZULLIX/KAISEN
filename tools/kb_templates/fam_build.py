#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Family pack BUILD gate: compile the pack and force one forward per kernel.

The build stage is where KAISEN's autofix ladder + LLM repair are wired, so
every failure that CAN be located here belongs here:

  * duplicate `load_inline(name=...)` extension names (silent torch cache
    collision across kernels of the pack) -> reject before compiling;
  * a kernel that does not compile;
  * a class that cannot be constructed or whose first forward raises
    (this catches LAZY compilation inside forward()).

Usage: build.py <candidate> <artifact>
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fam_common as kb  # noqa: E402


def _to_device(x, torch):
    if not isinstance(x, torch.Tensor):
        return x
    return x.to(dtype=torch.float32).to(device="cuda")


def exec_candidate(ctx: dict, src: str, build_dir) -> None:
    """KernelBench's own loading trick: pin the extension cache, then exec
    the pack in the caller's context."""
    prefixed = ("import os\n"
                f"os.environ['TORCH_EXTENSIONS_DIR'] = '{build_dir}'\n") + src
    exec(compile(prefixed, "<candidate>", "exec"), ctx)  # noqa: S102


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: build.py <candidate> <artifact>", file=sys.stderr)
        return 2
    cand_path, artifact = Path(sys.argv[1]), Path(sys.argv[2])
    src = kb.read_candidate(str(cand_path))
    if not src.strip():
        print("EMPTY CANDIDATE", file=sys.stderr)
        return 1

    dupes = kb.duplicate_ext_names(src)
    if dupes:
        print(f"DUPLICATE EXTENSION NAMES {dupes}: every kernel's "
              f"load_inline(name=...) must be UNIQUE — torch caches by name "
              f"and would grade the wrong binary.", file=sys.stderr)
        return 1

    kb.pin_gpu()
    kb_eval = kb.load_kb_eval()
    import torch  # noqa: E402  (after pin_gpu)

    gen = kb.current_generation(cand_path)
    problems = kb.verify_problems(gen)
    print(f"build+forward gate (gen {gen}): "
          + ", ".join(f"[{q['index']}]" for q in problems))
    log: list[str] = []
    failed: list[str] = []
    ok_count = 0

    with kb.FileLock(kb.BUILD_LOCK, timeout=kb.LOCK_TIMEOUT), kb.gpu_lock():
        for p in problems:
            tag = f"[{p['index']:>3}] {p['name']}"
            try:
                ctx: dict = {}
                _M, get_init_inputs, get_inputs = \
                    kb_eval.load_original_model_and_inputs(
                        kb.reference_src(p), ctx)
                exec_candidate(ctx, src, kb.BUILD_DIR)
                cls = ctx.get(p["class"])
                if cls is None:
                    raise RuntimeError(f"class {p['class']} not found in pack")
                torch.manual_seed(42)
                init_inputs = [_to_device(x, torch) for x in get_init_inputs()]
                torch.manual_seed(42)
                inputs = [_to_device(x, torch) for x in get_inputs()]
                with torch.no_grad():
                    model = cls(*init_inputs).to(device="cuda",
                                                 dtype=torch.float32)
                    model(*inputs)
                torch.cuda.synchronize()
                ok_count += 1
                log.append(f"ok   {tag}")
            except Exception as e:
                detail = "".join(
                    traceback.format_exception_only(type(e), e)).strip()
                tail = traceback.format_exc().strip().splitlines()[-3:]
                log.append(f"FAIL {tag}: {detail}")
                failed.append(f"{tag}: {detail} || {' / '.join(tail)}")

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("\n".join(log) + "\n", encoding="utf-8")
    for line in log:
        print(line)
    if failed:
        print("\n".join(failed)[-7000:], file=sys.stderr)
    if kb.STRICT and failed:
        print(f"BUILD FAIL: {len(failed)}/{len(problems)} kernels broken",
              file=sys.stderr)
        return 1
    if ok_count == 0:
        print("BUILD FAIL: no kernel in the pack compiled and ran",
              file=sys.stderr)
        return 1
    print(f"OK build ({ok_count}/{len(problems)} kernels live)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
