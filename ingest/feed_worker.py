import json
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import redis
import soundfile as sf
import structlog
import torch
import yaml
from dotenv import load_dotenv

# Global lock for VAD inference — PyTorch models are not thread-safe
# when called concurrently even on CPU
_vad_lock = threading.Lock()

load_dotenv()
log = structlog.get_logger()

REDIS_URL     = os.getenv("REDIS_URL",           "redis://localhost:6379/0")
CHUNK_SECONDS = int(os.getenv("AUDIO_CHUNK_SECONDS", "25"))
SAMPLE_RATE   = 16000
AUDIO_DIR     = Path("/tmp/detroit-pulse/audio")

TARGET_CHUNK_SECONDS  = CHUNK_SECONDS
MIN_CHUNK_SECONDS     = 10
MAX_CHUNK_SECONDS     = 40
SILENCE_SEARCH_WINDOW = 4
OVERLAP_SECONDS       = 3


def load_feeds(config_path: str = "ingest/feeds.yaml") -> list[dict]:
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return [f for f in config["feeds"] if f.get("enabled", True)]


def get_stream_url(feed: dict) -> str | None:
    if feed.get("test_stream_url"):
        return feed["test_stream_url"]
    bid = feed.get("broadcastify_feed_id", "REPLACE_ME")
    if str(bid) != "REPLACE_ME":
        return f"https://broadcastify.cdnstream1.com/{bid}"
    return None


def pull_raw_audio(stream_url: str, output_path: str, duration: int) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i",  stream_url,
        "-t",  str(duration),
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-f",  "wav",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True,
                                timeout=duration + 30)
        if result.returncode != 0:
            log.warning("ffmpeg error",
                        stderr=result.stderr.decode()[:300])
            return False
        return Path(output_path).exists()
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timeout")
        return False
    except Exception as e:
        log.error("ffmpeg exception", error=str(e))
        return False


def find_vad_cut_point(
    audio:          np.ndarray,
    vad_model,
    vad_utils,
    target_sample:  int,
    search_samples: int,
) -> int:
    """
    Find the best cut point near target_sample by locating a silence
    gap within +/- search_samples. Returns sample index to cut at.
    """
    get_speech_timestamps, *_ = vad_utils

    search_start = max(0, target_sample - search_samples)
    search_end   = min(len(audio), target_sample + search_samples)
    search_audio = torch.FloatTensor(audio[search_start:search_end])

    try:
        with _vad_lock:
            speech_ts = get_speech_timestamps(
                search_audio,
                vad_model,
                sampling_rate           = SAMPLE_RATE,
                min_silence_duration_ms = 300,
            )
    except Exception:
        return target_sample

    if not speech_ts:
        return target_sample

    best_cut      = target_sample
    best_distance = search_samples

    for i in range(len(speech_ts) - 1):
        gap_mid  = search_start + (speech_ts[i]["end"] + speech_ts[i+1]["start"]) // 2
        distance = abs(gap_mid - target_sample)
        if distance < best_distance:
            best_distance = distance
            best_cut      = search_start + speech_ts[i]["end"]

    return best_cut


def chunk_audio_vad(
    audio_path:     str,
    vad_model,
    vad_utils,
    overlap_buffer: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Load audio, prepend overlap buffer, find VAD-aware cut point,
    return (chunk_to_transcribe, next_overlap_buffer).
    """
    audio, sr = sf.read(audio_path, dtype="float32")

    if overlap_buffer is not None and len(overlap_buffer) > 0:
        audio = np.concatenate([overlap_buffer, audio])

    target_sample  = int(TARGET_CHUNK_SECONDS  * SAMPLE_RATE)
    min_samples    = int(MIN_CHUNK_SECONDS     * SAMPLE_RATE)
    search_samples = int(SILENCE_SEARCH_WINDOW * SAMPLE_RATE)

    if len(audio) < min_samples:
        return audio, np.array([])

    if len(audio) <= target_sample + search_samples:
        return audio, np.array([])

    cut   = find_vad_cut_point(audio, vad_model, vad_utils,
                                target_sample, search_samples)
    chunk = audio[:cut]
    tail  = audio[cut:]

    overlap_samples = int(OVERLAP_SECONDS * SAMPLE_RATE)
    next_overlap    = tail[-overlap_samples:] if len(tail) > overlap_samples else tail

    return chunk, next_overlap


def save_chunk(audio: np.ndarray, path: str):
    sf.write(path, audio, SAMPLE_RATE)


def enqueue_chunk(r: redis.Redis, feed_id: str,
                  chunk_path: str, timestamp: float):
    payload = json.dumps({
        "feed_id":    feed_id,
        "chunk_path": chunk_path,
        "timestamp":  timestamp,
    })
    r.lpush("queue:transcription", payload)
    log.info("Chunk enqueued", feed_id=feed_id)


def run_feed_worker(feed: dict, vad_model=None, vad_utils=None):
    """
    Main feed worker loop. VAD model/utils passed in from orchestrator.
    If not provided (e.g. running standalone), loads its own — but this
    should only happen during direct testing, not in production.
    """
    from ingest.vad import has_speech
    from api.broadcaster import publish_debug

    feed_id    = feed["id"]
    stream_url = get_stream_url(feed)

    if not stream_url:
        log.error("No stream URL configured", feed_id=feed_id)
        return

    # Only load VAD if not provided — avoids CUDA segfault in threaded use
    if vad_model is None or vad_utils is None:
        log.warning("VAD model not provided — loading locally (CPU)",
                    feed_id=feed_id)
        vad_model, vad_utils = torch.hub.load(
            repo_or_dir = "snakers4/silero-vad",
            model       = "silero_vad",
            force_reload = False,
            trust_repo  = True,
        )
        vad_model = vad_model.cpu()

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    r = redis.from_url(REDIS_URL)

    worker_log = structlog.get_logger().bind(feed_id=feed_id)
    worker_log.info("Feed worker starting", stream_url=stream_url)

    delay                = 5
    max_delay            = 120
    consecutive_failures = 0
    overlap_buffer       = None

    pull_duration = TARGET_CHUNK_SECONDS + SILENCE_SEARCH_WINDOW + 2

    while True:
        raw_path  = str(AUDIO_DIR / f"{feed_id}_raw_{int(time.time())}.wav")
        timestamp = time.time()

        publish_debug("ingest", feed_id,
                      status     = "pulling",
                      stream_url = stream_url)

        success = pull_raw_audio(stream_url, raw_path, pull_duration)

        if not success:
            consecutive_failures += 1
            delay = min(delay * 2, max_delay)
            worker_log.warning("Chunk pull failed — backing off",
                               consecutive_failures = consecutive_failures,
                               retry_in             = delay)
            publish_debug("ingest", feed_id,
                          status               = "failed",
                          consecutive_failures = consecutive_failures)
            time.sleep(delay)
            continue

        consecutive_failures = 0
        delay = 5

        try:
            chunk, overlap_buffer = chunk_audio_vad(
                raw_path, vad_model, vad_utils, overlap_buffer
            )
        except Exception as e:
            worker_log.error("VAD chunking failed", error=str(e))
            Path(raw_path).unlink(missing_ok=True)
            continue

        Path(raw_path).unlink(missing_ok=True)

        if len(chunk) == 0:
            worker_log.debug("Empty chunk after VAD — skipping")
            continue

        chunk_path = str(AUDIO_DIR / f"{feed_id}_{int(time.time())}.wav")
        save_chunk(chunk, chunk_path)

        if not has_speech(chunk_path):
            worker_log.debug("No speech detected — skipping chunk")
            Path(chunk_path).unlink(missing_ok=True)
            continue

        chunk_duration = round(len(chunk) / SAMPLE_RATE, 1)
        publish_debug("ingest", feed_id,
                      status          = "complete",
                      chunk_duration_s = chunk_duration,
                      has_overlap     = overlap_buffer is not None
                                        and len(overlap_buffer) > 0)

        enqueue_chunk(r, feed_id, chunk_path, timestamp)
        time.sleep(1)


if __name__ == "__main__":
    import sys
    feeds = load_feeds()
    if not feeds:
        print("No enabled feeds in feeds.yaml")
        sys.exit(1)
    feed = feeds[0]
    print(f"Starting worker for: {feed['name']}")
    run_feed_worker(feed)