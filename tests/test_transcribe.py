"""Transcription stage: queue handling with a fake model, prompt budget, and an optional real faster-whisper run."""
from __future__ import annotations

import math
import os
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core.config_files import Channel
from transcribe.vocab import PROMPT_CHAR_BUDGET, build_prompt
from transcribe.worker import ContextTails, process_one


def _wav(path: Path, seconds: float = 1.0, tone: bool = True) -> Path:
    rate = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)) if tone else 0)
                          for i in range(int(rate * seconds)))
        w.writeframes(frames)
    return path


class FakeModel:
    def __init__(self, text_out):
        self.text_out = text_out
        self.prompts = []

    def transcribe(self, path, **kw):
        self.prompts.append(kw.get("initial_prompt"))
        segs = [SimpleNamespace(text=self.text_out)] if self.text_out else []
        return iter(segs), SimpleNamespace(duration=1.0, language_probability=0.99)


def test_prompt_fits_whisper_window_and_keeps_specific_terms_last():
    ch = Channel(id="x", label="x", vocab=["Zzzspecific Street"])
    p = build_prompt(ch, "Engine 5 on scene " * 40)
    hints, _, tail = p.partition(" [Previous transmission:")
    assert len(hints) <= PROMPT_CHAR_BUDGET + 1
    assert hints.rstrip(".").endswith("Zzzspecific Street")          # most specific right before the tail
    assert len(tail) <= 200


def test_process_one_stores_transcript_and_empty_rows(db, tmp_path):
    from core.workqueue import claim, enqueue_transmission
    from datetime import datetime, timezone
    from core.settings import get_settings
    a = _wav(tmp_path / "a_20261114_101500.wav")
    b = _wav(tmp_path / "b_20261114_101600.wav", tone=False)
    now = datetime.now(timezone.utc)
    enqueue_transmission(source="sdr_files", source_key=str(a), channel_id="c1", captured_at=now, audio_path=str(a))
    enqueue_transmission(source="sdr_files", source_key=str(b), channel_id="c1", captured_at=now, audio_path=str(b))
    tails = ContextTails(300)
    s = get_settings()
    rows = claim("queued", "transcribing", limit=2)
    process_one(FakeModel("Engine 5 respond to Packard and State"), rows[0], tails, s)
    m2 = FakeModel("")
    process_one(m2, rows[1], tails, s)
    assert "Packard and State" in m2.prompts[0]                           # previous transmission as context
    with db.connect() as c:
        got = c.execute(text("SELECT status, raw_transcript, prefilter, has_incident FROM transcript_chunks "
                             "ORDER BY source_key")).all()
    assert got[0][0] == "transcribed" and got[0][1].startswith("Engine 5")
    assert got[1][0] == "done" and got[1][2] == "empty" and got[1][3] is False   # stored, never dropped


def test_missing_audio_is_retried_then_errors(db, tmp_path):
    from datetime import datetime, timezone
    from core.settings import get_settings
    from core.workqueue import claim, enqueue_transmission
    enqueue_transmission(source="sdr_files", source_key="gone", channel_id="c1",
                         captured_at=datetime.now(timezone.utc), audio_path=str(tmp_path / "gone.mp3"))
    for _ in range(3):
        rows = claim("queued", "transcribing")
        process_one(FakeModel("x"), rows[0], ContextTails(300), get_settings())
    with db.connect() as c:
        assert c.execute(text("SELECT status, attempts FROM transcript_chunks")).one() == ("error", 3)


WHISPER_CACHE = os.environ.get("PULSE_TEST_WHISPER_CACHE", "/tmp/claude-1000/-home-nathangamache-projects-godseye/"
                               "bb8d91ed-0113-56f6-b9ce-cd00cf94aa80/scratchpad/hw-bench/models/whisper")
JFK = Path(WHISPER_CACHE).parent.parent / "audio" / "jfk.wav"


@pytest.mark.whisper
@pytest.mark.skipif(not (Path(WHISPER_CACHE).exists() and JFK.exists()), reason="no local whisper cache / sample")
def test_real_faster_whisper_cpu_int8(monkeypatch):
    """tiny.en, int8, 4 threads, from the already-downloaded cache (no network). Run under nice."""
    monkeypatch.setenv("WHISPER_MODEL", "tiny.en")
    monkeypatch.setenv("WHISPER_CPU_THREADS", "4")
    monkeypatch.setenv("WHISPER_DOWNLOAD_ROOT", WHISPER_CACHE)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from core.settings import reset_settings_cache, get_settings
    reset_settings_cache()
    from transcribe.worker import load_model, transcribe_file
    s = get_settings()
    assert (s.whisper_device, s.whisper_compute_type) == ("cpu", "int8")
    model = load_model(s)
    res = transcribe_file(model, str(JFK), build_prompt(None), s)
    assert "country" in res.text.lower() and res.duration_s and res.duration_s > 10
