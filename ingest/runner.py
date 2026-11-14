"""Start the configured ingest sources (INGEST_SOURCES) as threads."""
from __future__ import annotations

import threading

import structlog

from core.settings import get_settings
from ingest.sources.base import Source

log = structlog.get_logger()

_ACTIVE: dict[str, Source] = {}


def build_sources(names: list[str] | None = None) -> list[Source]:
    s = get_settings()
    out: list[Source] = []
    for name in names or s.source_list:
        if name == "sdr_files":
            from ingest.sources.sdr_files import SdrFileSource
            out.append(SdrFileSource())
        elif name == "traintap":
            from ingest.sources.traintap import TraintapSource
            out.append(TraintapSource())
        elif name == "fixture":
            from ingest.sources.fixture import FixtureSource
            out.append(FixtureSource(s.fixture_dir / "transmissions.jsonl"))
        elif name == "broadcastify_legacy":
            from ingest.sources.broadcastify_legacy import BroadcastifyLegacySource
            out.append(BroadcastifyLegacySource())
        else:
            log.error("unknown ingest source", source=name)
    return out


def start_sources(stop: threading.Event, names: list[str] | None = None) -> list[threading.Thread]:
    s = get_settings()
    threads = []
    for src in build_sources(names):
        _ACTIVE[src.name] = src
        t = threading.Thread(target=src.run, args=(stop, s.sdr_poll_interval_s),
                             name=f"ingest-{src.name}", daemon=True)
        t.start()
        threads.append(t)
        log.info("ingest source started", source=src.name)
    return threads


def active_sources() -> dict[str, Source]:
    return dict(_ACTIVE)
