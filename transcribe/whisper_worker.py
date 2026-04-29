import json
import os
import time
from pathlib import Path

import redis
import structlog
from faster_whisper import WhisperModel
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

REDIS_URL     = os.getenv("REDIS_URL",     "redis://localhost:6379/0")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
DEVICE        = "cuda"
COMPUTE_TYPE  = "float16"

VOCAB_HINTS_PATH     = Path("transcribe/vocab_hints.txt")
CONTEXT_WINDOW_WORDS = 30
CONTEXT_REDIS_TTL    = 300
CONTEXT_KEY_PREFIX   = "whisper:context:"

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def load_vocab_hints() -> str:
    if VOCAB_HINTS_PATH.exists():
        return VOCAB_HINTS_PATH.read_text().strip()
    return ""


def get_context_tail(feed_id: str) -> str:
    r   = get_redis()
    key = f"{CONTEXT_KEY_PREFIX}{feed_id}"
    return r.get(key) or ""


def set_context_tail(feed_id: str, transcript: str):
    r     = get_redis()
    key   = f"{CONTEXT_KEY_PREFIX}{feed_id}"
    words = transcript.split()
    tail  = " ".join(words[-CONTEXT_WINDOW_WORDS:]) \
            if len(words) > CONTEXT_WINDOW_WORDS else transcript
    r.setex(key, CONTEXT_REDIS_TTL, tail)


def build_initial_prompt(vocab_hints: str, context_tail: str) -> str:
    parts = []
    if vocab_hints:
        parts.append(vocab_hints)
    if context_tail:
        parts.append(f"[Previous transmission: {context_tail}]")
    return " ".join(parts)


def transcribe_chunk(
    chunk_path:    str,
    vocab_hints:   str,
    feed_id:       str           = "",
    whisper_model: WhisperModel  = None,
) -> str | None:
    """
    Transcribe a single audio chunk.
    Uses pre-loaded model if provided, otherwise loads its own
    (only for standalone testing).
    """
    if whisper_model is None:
        log.warning("No whisper model provided — loading locally")
        whisper_model = WhisperModel(
            WHISPER_MODEL,
            device       = DEVICE,
            compute_type = COMPUTE_TYPE,
        )

    context_tail = get_context_tail(feed_id)
    prompt       = build_initial_prompt(vocab_hints, context_tail)

    try:
        segments, info = whisper_model.transcribe(
            chunk_path,
            language                   = "en",
            condition_on_previous_text = True,
            initial_prompt             = prompt,
            vad_filter                 = True,
            vad_parameters             = {
                "min_silence_duration_ms": 300,
                "speech_pad_ms":           200,
            },
        )

        transcript = " ".join(seg.text.strip() for seg in segments).strip()

        log.info("Transcription complete",
            feed_id              = feed_id,
            language_probability = round(info.language_probability, 2),
            transcript_length    = len(transcript),
            had_context          = bool(context_tail),
        )

        if transcript:
            set_context_tail(feed_id, transcript)

        return transcript if transcript else None

    except Exception as e:
        log.error("Transcription failed",
                  chunk_path=chunk_path, error=str(e))
        return None


def enqueue_transcript(
    r:          redis.Redis,
    feed_id:    str,
    chunk_path: str,
    timestamp:  float,
    transcript: str,
):
    payload = json.dumps({
        "feed_id":    feed_id,
        "chunk_path": chunk_path,
        "timestamp":  timestamp,
        "transcript": transcript,
    })
    r.lpush("queue:normalization", payload)
    log.info("Transcript enqueued", feed_id=feed_id)


def run_whisper_worker(whisper_model: WhisperModel = None):
    """
    Main loop. Accepts pre-loaded model from orchestrator.
    """
    from api.broadcaster import publish_debug

    r           = redis.from_url(REDIS_URL)
    vocab_hints = load_vocab_hints()

    log.info("Whisper worker starting", model=WHISPER_MODEL)

    # Only load if not provided — should only happen in standalone testing
    if whisper_model is None:
        log.warning("No model passed in — loading locally (not recommended in production)")
        whisper_model = WhisperModel(
            WHISPER_MODEL,
            device       = DEVICE,
            compute_type = COMPUTE_TYPE,
        )

    while True:
        item = r.brpop("queue:transcription", timeout=5)
        if item is None:
            continue

        _, raw = item
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Invalid JSON in transcription queue")
            continue

        feed_id    = payload["feed_id"]
        chunk_path = payload["chunk_path"]
        timestamp  = payload["timestamp"]

        log.info("Processing chunk", feed_id=feed_id)
        publish_debug("transcription", feed_id, status="starting")

        t0         = time.time()
        transcript = transcribe_chunk(
            chunk_path    = chunk_path,
            vocab_hints   = vocab_hints,
            feed_id       = feed_id,
            whisper_model = whisper_model,
        )
        elapsed = round(time.time() - t0, 1)

        Path(chunk_path).unlink(missing_ok=True)

        if transcript:
            publish_debug("transcription", feed_id,
                status     = "complete",
                elapsed_s  = elapsed,
                transcript = transcript,
                source     = "REAL AUDIO",
            )
            enqueue_transcript(r, feed_id, chunk_path, timestamp, transcript)
        else:
            publish_debug("transcription", feed_id,
                status    = "silent",
                elapsed_s = elapsed,
                source    = "REAL AUDIO",
            )
            log.info("Empty transcript — discarding", feed_id=feed_id)


if __name__ == "__main__":
    run_whisper_worker()