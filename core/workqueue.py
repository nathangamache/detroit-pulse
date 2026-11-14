"""Durable work queue on transcript_chunks.status (Postgres, FOR UPDATE SKIP LOCKED).

    queued --transcribe--> transcribed --extract--> done
         \\__ error (after MAX_ATTEMPTS)          \\__ error

Every transmission is persisted the moment it is ingested, so a crash, a
Claude outage or an empty transcript can never silently drop it (the old
pipeline lost audio in the chunker, never stored has_incident=false rows, and
let RETRY items expire unwritten).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from core.db import get_engine
from db.models import ST_DONE, ST_ERROR, ST_EXTRACTING, ST_QUEUED, ST_TRANSCRIBED, ST_TRANSCRIBING

MAX_ATTEMPTS = 3


def enqueue_transmission(*, source: str, source_key: str, channel_id: str,
                         captured_at: datetime, audio_path: str | None = None,
                         transcript: str | None = None, freq_hz: int | None = None,
                         duration_s: float | None = None, county: str = "") -> str | None:
    """Insert one transmission. Returns its chunk_id, or None if source_key was already seen.

    Audio rows start as `queued` (needs Whisper); text-only rows (fixtures) start as
    `transcribed`.
    """
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    cid = uuid.uuid4()
    status = ST_TRANSCRIBED if transcript is not None and audio_path is None else ST_QUEUED
    with get_engine().begin() as c:
        row = c.execute(text("""
            INSERT INTO transcript_chunks
                (chunk_id, feed_id, channel_id, "timestamp", captured_at, source, source_key,
                 audio_path, raw_transcript, freq_hz, duration_s, status,
                 transcribed_at)
            VALUES (:cid, :ch, :ch, :ts, :ts, :src, :key, :audio, :tx, :freq, :dur, :st,
                    CASE WHEN :st = 'transcribed' THEN now() END)
            ON CONFLICT (source_key) DO NOTHING
            RETURNING chunk_id"""), {
            "cid": cid, "ch": channel_id, "ts": captured_at, "src": source, "key": source_key,
            "audio": audio_path, "tx": transcript, "freq": freq_hz, "dur": duration_s, "st": status,
        }).first()
    return str(row[0]) if row else None


def claim(from_status: str, to_status: str, limit: int = 1,
          order_by: str = "captured_at") -> list[dict[str, Any]]:
    """Atomically move up to `limit` rows from one status to another and return them."""
    assert order_by in ("captured_at", "created_at")
    with get_engine().begin() as c:
        rows = c.execute(text(f"""
            UPDATE transcript_chunks t
               SET status = :to, attempts = t.attempts + 1, claimed_at = now()
             WHERE t.chunk_id IN (
                   SELECT chunk_id FROM transcript_chunks
                    WHERE status = :frm
                    ORDER BY {order_by} NULLS LAST
                    LIMIT :lim
                    FOR UPDATE SKIP LOCKED)
         RETURNING t.chunk_id, t.channel_id, t.feed_id, t.captured_at, t.audio_path,
                   t.raw_transcript, t.freq_hz, t.duration_s, t.attempts, t.source,
                   t.created_at"""), {"frm": from_status, "to": to_status, "lim": limit}).mappings().all()
    return [dict(r) for r in rows]


def count_waiting(status: str) -> int:
    with get_engine().connect() as c:
        return int(c.execute(text("SELECT count(*) FROM transcript_chunks WHERE status = :s"),
                             {"s": status}).scalar() or 0)


def oldest_waiting_age_s(status: str) -> float | None:
    with get_engine().connect() as c:
        v = c.execute(text("""SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))
                              FROM transcript_chunks WHERE status = :s"""), {"s": status}).scalar()
    return float(v) if v is not None else None


def release(chunk_id: str, back_to: str, error: str | None = None) -> None:
    """Return a claimed row to the queue (or mark it failed after MAX_ATTEMPTS)."""
    with get_engine().begin() as c:
        c.execute(text("""
            UPDATE transcript_chunks
               SET status = CASE WHEN attempts >= :max THEN :err ELSE :back END,
                   error = :e
             WHERE chunk_id = :cid"""),
                  {"max": MAX_ATTEMPTS, "err": ST_ERROR, "back": back_to, "e": (error or "")[:2000],
                   "cid": chunk_id})


def requeue_stale(working: str, back_to: str, older_than_s: int = 900) -> int:
    """Rows stuck in a working state (worker crashed) go back to the queue."""
    with get_engine().begin() as c:
        res = c.execute(text("""
            UPDATE transcript_chunks SET status = :back
             WHERE status = :w AND coalesce(claimed_at, created_at) < now() - make_interval(secs => :age)"""),
                        {"back": back_to, "w": working, "age": older_than_s})
        return res.rowcount or 0


def requeue_all_stale() -> int:
    return (requeue_stale(ST_TRANSCRIBING, ST_QUEUED) + requeue_stale(ST_EXTRACTING, ST_TRANSCRIBED))


__all__ = ["enqueue_transmission", "claim", "release", "count_waiting", "oldest_waiting_age_s",
           "requeue_stale", "requeue_all_stale", "MAX_ATTEMPTS",
           "ST_QUEUED", "ST_TRANSCRIBING", "ST_TRANSCRIBED", "ST_EXTRACTING", "ST_DONE", "ST_ERROR"]
