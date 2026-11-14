"""Ingest source interface.

A source turns some external input into rows in transcript_chunks (status
`queued` for audio, `transcribed` for text) or, for rail telemetry, rows in
rail_telemetry. Sources run in their own thread until `stop` is set.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod


class Source(ABC):
    name: str = "source"

    @abstractmethod
    def poll_once(self) -> int:
        """Ingest whatever is ready now; return how many items were ingested."""

    def run(self, stop: threading.Event, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            n = self.poll_once()
            if n == 0:
                stop.wait(interval_s)

    def status(self) -> dict:
        """Health info for /feeds_status."""
        return {"source": self.name}
