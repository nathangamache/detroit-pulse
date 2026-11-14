"""Transcription stage: one Whisper pass per transmission file.

SDR sources (rtl_airband split_on_transmission, trunk-recorder) already deliver
one file per transmission, so the old fixed 25 s chunker and its VAD cut-point
search are gone. faster-whisper's own Silero VAD (onnxruntime) trims silence.

Defaults are tuned for an older CPU-only server (2x Xeon E5-2687W v2, AVX, no AVX2, no GPU):
small.en / int8 / 8 threads, about 3.8 s per transmission. medium.en is the
accuracy option (about 10 s per transmission). Every setting is in .env.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import text

from core import bus
from core.config_files import channel_by_id
from core.db import get_engine
from core.settings import Settings, get_settings
from core.workqueue import ST_DONE, ST_QUEUED, ST_TRANSCRIBED, ST_TRANSCRIBING, claim, release
from transcribe.vocab import build_prompt

log = structlog.get_logger()

_model_lock = threading.Lock()


@dataclass
class TranscriptResult:
    text: str
    duration_s: float | None
    language_probability: float | None
    elapsed_s: float


def load_model(s: Settings | None = None):
    """Load faster-whisper with the configured CPU settings (import kept local: it's heavy)."""
    from faster_whisper import WhisperModel

    s = s or get_settings()
    log.info("loading whisper", model=s.whisper_model, device=s.whisper_device,
             compute_type=s.whisper_compute_type, cpu_threads=s.whisper_cpu_threads)
    return WhisperModel(
        s.whisper_model,
        device=s.whisper_device,
        compute_type=s.whisper_compute_type,
        cpu_threads=s.whisper_cpu_threads,
        num_workers=s.whisper_num_workers,
        download_root=str(s.whisper_download_root) if s.whisper_download_root else None,
    )


def transcribe_file(model, path: str, prompt: str, s: Settings | None = None) -> TranscriptResult:
    s = s or get_settings()
    t0 = time.perf_counter()
    segments, info = model.transcribe(
        path,
        language="en",
        beam_size=s.whisper_beam_size,
        initial_prompt=prompt or None,
        # each file is one transmission; context comes from the prompt tail instead
        condition_on_previous_text=False,
        vad_filter=s.whisper_vad_filter,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
        no_speech_threshold=0.6,
    )
    text_out = " ".join(seg.text.strip() for seg in segments).strip()
    return TranscriptResult(
        text=text_out,
        duration_s=getattr(info, "duration", None),
        language_probability=getattr(info, "language_probability", None),
        elapsed_s=time.perf_counter() - t0,
    )


class ContextTails:
    """Last transcript per channel, used as a short prompt tail for the next transmission."""

    def __init__(self, window_s: int):
        self.window_s = window_s
        self._d: dict[str, tuple[float, str]] = {}

    def get(self, channel_id: str) -> str:
        hit = self._d.get(channel_id)
        if not hit or time.time() - hit[0] > self.window_s:
            return ""
        return hit[1]

    def put(self, channel_id: str, transcript: str) -> None:
        if transcript:
            self._d[channel_id] = (time.time(), transcript)


def _store_result(chunk_id: str, res: TranscriptResult, model_name: str) -> None:
    empty = not res.text
    with get_engine().begin() as c:
        c.execute(text("""
            UPDATE transcript_chunks
               SET raw_transcript = :tx,
                   status = :st,
                   transcribed_at = now(),
                   whisper_model = :m,
                   duration_s = coalesce(duration_s, :dur),
                   processing_ms = :ms,
                   error = NULL,
                   -- empty transcripts are persisted as finished non-incidents, never dropped
                   prefilter = CASE WHEN :empty THEN 'empty' ELSE prefilter END,
                   has_incident = CASE WHEN :empty THEN false ELSE has_incident END,
                   extract_source = CASE WHEN :empty THEN 'none' ELSE extract_source END,
                   extracted_at = CASE WHEN :empty THEN now() ELSE extracted_at END
             WHERE chunk_id = :cid"""), {
            "tx": res.text, "st": ST_DONE if empty else ST_TRANSCRIBED, "m": model_name,
            "dur": res.duration_s, "ms": int(res.elapsed_s * 1000), "empty": empty, "cid": chunk_id,
        })


def process_one(model, row: dict, tails: ContextTails, s: Settings) -> TranscriptResult | None:
    chunk_id = str(row["chunk_id"])
    channel_id = row.get("channel_id") or row.get("feed_id") or ""
    path = row.get("audio_path")
    if not path or not os.path.exists(path):
        release(chunk_id, ST_QUEUED, error=f"audio file missing: {path}")
        return None
    channel = channel_by_id(channel_id)
    prompt = build_prompt(channel, tails.get(channel_id))
    try:
        with _model_lock:
            res = transcribe_file(model, path, prompt, s)
    except Exception as e:  # noqa: BLE001 - corrupt/partial audio must not kill the worker
        log.warning("transcription failed", chunk_id=chunk_id[:8], error=str(e))
        release(chunk_id, ST_QUEUED, error=f"whisper: {e}")
        return None
    _store_result(chunk_id, res, s.whisper_model)
    tails.put(channel_id, res.text)
    if s.sdr_delete_after_transcribe and row.get("source") == "sdr_files":
        try:
            os.remove(path)
        except OSError:
            pass
    bus.publish_debug("transcribe", channel_id, chunk_id=chunk_id, chars=len(res.text),
                      elapsed_s=round(res.elapsed_s, 2))
    if res.text:
        bus.ring("extract")
    log.info("transcribed", chunk_id=chunk_id[:8], channel=channel_id, chars=len(res.text),
             elapsed_s=round(res.elapsed_s, 2), audio_s=res.duration_s)
    return res


def run_transcribe_worker(stop: threading.Event | None = None, model=None) -> None:
    s = get_settings()
    model = model or load_model(s)
    tails = ContextTails(s.whisper_context_window_s)
    stop = stop or threading.Event()
    log.info("transcribe worker ready", model=s.whisper_model)
    while not stop.is_set():
        rows = claim(ST_QUEUED, ST_TRANSCRIBING, limit=1)
        if not rows:
            bus.wait_for_ring("transcribe", 2)
            continue
        process_one(model, rows[0], tails, s)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
