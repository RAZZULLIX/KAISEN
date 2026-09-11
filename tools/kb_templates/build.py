#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KernelBench build gate: compile the candidate AND force one real forward.

Two failure modes matter, and BOTH must surface here — the build stage is
where KAISEN's deterministic autofix ladder and the LLM repair pass are
wired in:

  1. the extension does not compile  -> kb_eval.build_compile_cache
  2. it compiles only LAZILY (`load_inline` called inside `forward()`), the
     class cannot be constructed, or the first call raises -> forced with
     one seeded forward pass here.

Usage: build.py <candidate> <artifact>
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import kb_common as kb  # noqa: E402


def _to_device(x, torch):
    if not isinstance(x, torch.Tensor):
        return x
    return x.to(dtype=torch.float32).to(device="cuda")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: build.py <candidate> <artifact>", file=sys.stderr)
        return 2
    cand_path, artifact = Path(sys.argv[1]), Path(sys.argv[2])
    src = kb.read_candidate(str(cand_path))
    if not src.strip():
        print("EMPTY CANDIDATE", file=sys.stderr)
        return 1

    kb.pin_gpu()
    kb_eval = kb.load_kb_eval()

    # 1) compile gate — one build at a time per project (torch extension dirs
    #    collide when several pipelines of the same project build in parallel).
    with kb.FileLock(kb.BUILD_LOCK, timeout=kb.LOCK_TIMEOUT):
        ok, log, err = kb_eval.build_compile_cache(
            src, verbose=False, build_dir=str(kb.BUILD_DIR)
        )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(log or "", encoding="utf-8")
    if not ok:
        tail = (log or "").strip()
        if tail:
            print(tail[-6000:], file=sys.stderr)
        print(f"COMPILE FAIL: {err}", file=sys.stderr)
        return 1

    # 2) run gate — construct ModelNew and run ONE seeded forward.
    import torch  # noqa: E402  (import after pin_gpu)

    try:
        with kb.gpu_lock():
            context: dict = {}
            ModelNew = kb_eval.load_custom_model(
                src, context, build_directory=str(kb.BUILD_DIR)
            )
            if ModelNew is None:
                print("LOAD FAIL: ModelNew not found (syntax error?)",
                      file=sys.stderr)
                return 1
            ref_ctx: dict = {}
            _Model, get_init_inputs, get_inputs = \
                kb_eval.load_original_model_and_inputs(kb.read_reference(), ref_ctx)
            torch.manual_seed(42)
            init_inputs = [_to_device(x, torch) for x in get_init_inputs()]
            torch.manual_seed(42)
            inputs = [_to_device(x, torch) for x in get_inputs()]
            with torch.no_grad():
                model = ModelNew(*init_inputs).to(device="cuda",
                                                  dtype=torch.float32)
                model(*inputs)
            torch.cuda.synchronize()
    except Exception as e:
        print("RUN FAIL: " + "".join(
            traceback.format_exception_only(type(e), e)).strip(), file=sys.stderr)
        print(traceback.format_exc()[-4000:], file=sys.stderr)
        return 1

    print("OK build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
