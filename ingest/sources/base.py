"""Ingest source interface.

A source turns some external input into rows in transcript_chunks (status
`queued` for audio, `transcribed` for text) or, for rail telemetry, rows in
rail_telemetry. Sources run in their own thread until `stop` is set.

After each completed poll a source beats, about every INGEST_HEARTBEAT_S, into the
`liveness` table (core/liveness.py): its own heartbeat ('blind' when it can't see its
input) and its receivers' heartbeat files. /v1/rail/activity calls a window quiet only
when those runs cover all of it.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

import structlog

log = structlog.get_logger()


class Source(ABC):
    name: str = "source"

    @abstractmethod
    def poll_once(self) -> int:
        """Ingest whatever is ready now; return how many items were ingested."""

    def run(self, stop: threading.Event, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            n = self.poll_once()
            self.maybe_beat()
            if n == 0:
                stop.wait(interval_s)

    # ── heartbeat ───────────────────────────────────────────────────────────
    def blind_reason(self) -> str | None:
        """Why this source can't see its input right now (None when it can)."""
        return None

    def beat_detail(self) -> dict:
        return {}

    def receiver_names(self) -> set[str]:
        """Receivers whose output this source ingests (their heartbeats are recorded with it)."""
        return set()

    def beat(self) -> None:
        from core.liveness import record_ingest
        record_ingest(self.name, blind=self.blind_reason(), detail=self.beat_detail(),
                      receivers=self.receiver_names())

    def maybe_beat(self, force: bool = False) -> bool:
        """Beat if INGEST_HEARTBEAT_S has passed since the last one. A failed beat is logged and
        never stops ingestion (it only means no quiet verdict until the next one lands)."""
        from core.settings import get_settings
        now = time.monotonic()
        last = getattr(self, "_last_beat", None)
        if not force and last is not None and now - last < get_settings().ingest_heartbeat_s:
            return False
        self._last_beat = now
        try:
            self.beat()
        except Exception:  # noqa: BLE001
            log.warning("ingest heartbeat failed", source=self.name, exc_info=True)
            return False
        return True

    def status(self) -> dict:
        """Health info for /feeds_status."""
        return {"source": self.name}
