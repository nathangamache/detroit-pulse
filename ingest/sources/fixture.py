"""`fixture` source: replay synthetic transmissions (text or audio) for tests and demos.

A fixture file is JSONL, one transmission per line:

    {"channel_id": "aa_fire", "text": "Engine 5 respond ...", "offset_s": 0}
    {"channel_id": "aa_fire", "audio": "relative/or/absolute.wav", "offset_s": 12.5}

`offset_s` is seconds after the replay start (captured_at = start + offset).
Text lines skip Whisper and go straight to extraction; audio lines are queued
for transcription like SDR files. Only synthetic fixtures belong in the repo.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import bus
from core.workqueue import enqueue_transmission
from ingest.sources.base import Source


class FixtureSource(Source):
    name = "fixture"

    def __init__(self, path: Path, start: datetime | None = None, run_id: str | None = None):
        self.path = Path(path)
        self.start = start or datetime.now(timezone.utc)
        self.run_id = run_id or self.start.strftime("%Y%m%dT%H%M%S")
        self._done = False
        self.ingested = 0

    def items(self) -> list[dict]:
        out = []
        for i, line in enumerate(self.path.read_text().splitlines()):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = json.loads(line)
            d.setdefault("id", f"{self.path.stem}:{i}")
            out.append(d)
        return out

    def poll_once(self) -> int:
        if self._done:
            return 0
        n = 0
        for d in self.items():
            at = self.start + timedelta(seconds=float(d.get("offset_s", 0)))
            audio = d.get("audio")
            if audio and not Path(audio).is_absolute():
                audio = str((self.path.parent / audio).resolve())
            cid = enqueue_transmission(
                source=self.name, source_key=f"fixture:{self.run_id}:{d['id']}",
                channel_id=d.get("channel_id", "fixture"), captured_at=at,
                audio_path=audio, transcript=None if audio else d.get("text", ""),
                freq_hz=d.get("freq_hz"))
            if cid:
                n += 1
        self._done = True
        self.ingested += n
        bus.ring("transcribe")
        bus.ring("extract")
        return n

    def status(self) -> dict:
        return {"source": self.name, "path": str(self.path), "ingested": self.ingested}
