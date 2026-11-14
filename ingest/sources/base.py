"""Ingest source interface.

A source turns some external input into rows in transcript_chunks (status
`queued` for audio, `transcribed` for text) or, for rail telemetry, rows in
rail_telemetry. Sources run in their own thread until `stop` is set.

After each completed poll a source beats, about every INGEST_HEARTBEAT_S, into the
`liveness` table (core/liveness.py): its own heartbeat ('blind' when it can't see its
input) and its receivers' heartbeat files. A source beats at once, after the poll that saw
it, when what it must report right away changes (`urgent`: sdr_files' recordings still being
written). The beat's time is when that poll began (`checked_at`), so it never claims to have
looked later than it did. /v1/rail/activity calls a window quiet only when those runs cover
all of it, and only up to the newest beat.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

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
            self.maybe_beat(force=self.urgent() != getattr(self, "_beat_urgent", None))
            if n == 0:
                stop.wait(interval_s)

    def mark_checked(self) -> None:
        """Call at the start of poll_once: the beat after it says the input was looked at then."""
        self.checked_at = datetime.now(timezone.utc)

    # ── heartbeat ───────────────────────────────────────────────────────────
    def blind_reason(self) -> str | None:
        """Why this source can't see its input right now (None when it can)."""
        return None

    def beat_detail(self) -> dict:
        return {}

    def urgent(self) -> object:
        """What the next beat must report as soon as it changes (compared after every poll)."""
        return None

    def receiver_names(self) -> set[str]:
        """Receivers whose output this source ingests (their heartbeats are recorded with it)."""
        return set()

    def beat(self) -> None:
        from core.liveness import record_ingest
        urgent = self.urgent()
        record_ingest(self.name, blind=self.blind_reason(), detail=self.beat_detail(),
                      receivers=self.receiver_names(), at=getattr(self, "checked_at", None))
        self._beat_urgent = urgent

    def maybe_beat(self, force: bool = False) -> bool:
        """Beat if INGEST_HEARTBEAT_S has passed since the last one (or `force`). A failed beat is
        logged and never stops ingestion (it only means no quiet verdict until the next one
        lands); after one, `force` waits for the usual interval too."""
        from core.settings import get_settings
        now = time.monotonic()
        last = getattr(self, "_last_beat", None)
        interval = get_settings().ingest_heartbeat_s
        failed = getattr(self, "_beat_failed", False)
        if last is not None and now - last < interval and (not force or failed):
            return False
        self._last_beat = now
        try:
            self.beat()
        except Exception:  # noqa: BLE001
            self._beat_failed = True
            log.warning("ingest heartbeat failed", source=self.name, exc_info=True)
            return False
        self._beat_failed = False
        return True

    def status(self) -> dict:
        """Health info for /feeds_status."""
        return {"source": self.name}
