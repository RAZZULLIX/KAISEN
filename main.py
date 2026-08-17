#!/usr/bin/env python3
# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""KAISEN framework entry point.

    python main.py                          # GUI only (pick a project there)
    python main.py --project compression    # run compression + GUI
    python main.py --project sorter --multi 2 --workers 4
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    parser = argparse.ArgumentParser(description="KAISEN framework")
    parser.add_argument("--project", default=None, help="project id to run (see projects/)")
    parser.add_argument("--no-server", action="store_true", help="do not start the GUI server")
    parser.add_argument("--host", default=None, help="GUI bind host")
    parser.add_argument("--port", type=int, default=None, help="GUI port")
    parser.add_argument("--kai", action="store_true",
                        help="run the KAI protocol stdio server (LLM-facing API)")
    parser.add_argument("--workers", type=int, default=None, help="worker process count (overrides project/config defaults)")
    args = parser.parse_args()

    if args.kai:
        from kaisen.kai import main as kai_main
        kai_main(host=args.host, port=args.port)
        return

    from kaisen.config import get_config
    from kaisen.llm import ModelOrchestrator
    from kaisen.projects import ProjectRegistry

    cfg = get_config()
    registry = ProjectRegistry()
    orchestrator = ModelOrchestrator(cfg)

    engine = None
    if args.project:
        from kaisen.engine import ProjectEngine
        project = registry.require(args.project)
        n_workers = args.workers if args.workers is not None else project.default_workers
        multi = args.multi if args.multi is not None else project.default_multi
        engine = ProjectEngine(project, orchestrator, registry, worker_count=n_workers)
        print(f"[KAISEN] starting project '{project.id}' with {n_workers} workers, {multi} generators")
        # Default: start PAUSED — the user presses play.  Override in
        # config.json ("engine": {"start_paused": false}).
        engine.start(multi=multi, paused=cfg.engine_start_paused)

    if args.no_server:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        from kaisen.server import DashboardServer
        eff_host = args.host or cfg.server_host
        eff_port = args.port or int(cfg.server_port)
        server = DashboardServer(
            registry, cfg, engine,
            host=eff_host,
            port=eff_port,
        )
        print(f"[KAISEN] dashboard on http://{eff_host}:{eff_port}")
        if server.api_key_set:
            print("[KAISEN] server password active — clients need Authorization (Bearer/Basic)")
        try:
            server.start()
        except KeyboardInterrupt:
            pass
        finally:
            if engine:
                engine.stop()


if __name__ == "__main__":
    main()
