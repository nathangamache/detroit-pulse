#!/usr/bin/env python3
"""Detroit Pulse pipeline: ingest -> transcribe -> extract -> (resolver), as threads in one process.

    uv run python run_pipeline.py                         # all stages
    uv run python run_pipeline.py --stages ingest,transcribe
    uv run python run_pipeline.py --stages extract,resolver --sources fixture

Stages talk only through Postgres (transcript_chunks.status) plus Redis doorbells,
so they can also run as separate processes. On this shared box run it niced:

    nice -n 19 uv run python run_pipeline.py
"""
from __future__ import annotations

import argparse
import signal
import threading
import time

import structlog

from core.logging import setup_logging
from core.settings import get_settings

log = structlog.get_logger()
ALL_STAGES = ["ingest", "transcribe", "extract", "resolver"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default=",".join(ALL_STAGES))
    ap.add_argument("--sources", default=None, help="override INGEST_SOURCES")
    a = ap.parse_args()
    setup_logging()
    s = get_settings()
    stages = [x.strip() for x in a.stages.split(",") if x.strip()]
    stop = threading.Event()
    threads: list[threading.Thread] = []

    if "ingest" in stages:
        from ingest.runner import start_sources
        names = [x.strip() for x in a.sources.split(",")] if a.sources else None
        threads += start_sources(stop, names)

    if "transcribe" in stages:
        from transcribe.worker import load_model, run_transcribe_worker
        model = load_model(s)                     # load once, in the main thread
        for i in range(max(1, s.whisper_num_workers)):
            threads.append(threading.Thread(target=run_transcribe_worker, args=(stop, model),
                                            name=f"transcribe-{i}", daemon=True))

    worker = None
    if "extract" in stages:
        from extract.worker import ExtractWorker, run_extract_worker
        worker = ExtractWorker()
        threads.append(threading.Thread(target=run_extract_worker, args=(stop, worker), name="extract", daemon=True))

    if "resolver" in stages:
        from correlation.resolver import run_resolver_loop
        client = worker.client if worker else None
        meter = worker.meter if worker else None
        threads.append(threading.Thread(target=run_resolver_loop, args=(stop, client, meter), name="resolver",
                                        daemon=True))

    for t in threads:
        if not t.is_alive():
            t.start()
    log.info("pipeline running", stages=stages, threads=[t.name for t in threads])

    def _shutdown(sig, _frame):
        log.info("shutting down", signal=sig)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    while not stop.is_set():
        stop.wait(60)
        dead = [t.name for t in threads if not t.is_alive() and not t.name.startswith("ingest-broadcastify")]
        if dead and not stop.is_set():
            log.warning("stage threads not alive", dead=dead)
    for t in threads:
        t.join(timeout=10)
    time.sleep(0.1)


if __name__ == "__main__":
    main()
