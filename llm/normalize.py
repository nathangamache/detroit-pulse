import json
import os
import re
import time
from datetime import datetime, timezone

import structlog
from ollama import Client
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
log = structlog.get_logger()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
BASE_MODEL      = os.getenv("LORA_BASE_MODEL",     "qwen2.5:7b-instruct")
LORA_MODEL      = os.getenv("LORA_NORMALIZE_MODEL", "detroit-normalize-v1")
LORA_ENABLED    = os.getenv("LORA_NORMALIZE_ENABLED", "false").lower() == "true"
SHADOW_MODE     = os.getenv("LORA_SHADOW_MODE",       "false").lower() == "true"

_client: Client | None = None
_db_engine = None

def _get_db_engine():
    global _db_engine
    if _db_engine is None:
        _db_engine = create_engine(
            os.getenv("DATABASE_URL"),
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
        )
    return _db_engine

# Circuit breaker (carried over from existing normalize.py)
_cb_failure_count    = 0
_cb_open_until       = 0.0
CB_FAILURE_THRESHOLD = 3
CB_OPEN_SECONDS      = 60

# ── Metrics counters (flushed to DB every hour by metrics_collector.py) ──────
_counters: dict = {
    "normalize_total":   0,
    "normalize_no_loc":  0,
    "norm_latency_ms_sum": 0,
    "shadow_disagree":   0,
}


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(host=OLLAMA_BASE_URL)
    return _client


# ── Prompt (must match training data format exactly) ─────────────────────────

NORMALIZE_SYSTEM = """You are a Detroit metro area dispatch address normalizer.
Convert dispatch shorthand into full, geocodable location strings.

Metro Detroit geography reference:
- Mile roads run east-west: 6 Mile, 7 Mile, 8 Mile (state line),
  9 Mile, 10 Mile, 11 Mile, 12 Mile, 13 Mile, 14 Mile, 15 Mile
- Major north-south corridors: Woodward Avenue, Gratiot Avenue, Mound Road,
  Van Dyke Avenue, Dequindre Road, John R Street, Schoenherr Road,
  Beeline Highway, Livernois Avenue, Outer Drive, Harper Avenue
- Major diagonal / east-west roads: Northwestern Highway, Telegraph Road,
  Ford Road, Michigan Avenue, Ecorse Road, Cherry Hill Road,
  Plymouth Road, Tireman Avenue, Vernor Highway, Ann Arbor Road

CRITICAL - NUMBER DISAMBIGUATION:
A number is part of an address ONLY when it is immediately preceded or
followed by a street name or clear address context.
Numbers describing rounds, floors, unit numbers, times, or people counts
are NEVER part of an address.

If no address is detectable, return exactly: NO_LOCATION
Return ONLY the address string. No explanation."""


def _call_model(model: str, transcript: str) -> tuple[str, int]:
    """Call Ollama and return (result, latency_ms)."""
    t0 = time.time()
    try:
        client   = get_client()
        response = client.chat(
            model    = model,
            messages = [
                {"role": "system", "content": NORMALIZE_SYSTEM},
                {"role": "user",   "content": f"Transmission: {transcript}"},
            ],
            options  = {"temperature": 0, "num_predict": 64},
        )
        raw = response["message"]["content"].strip()
        ms  = int((time.time() - t0) * 1000)
        return _sanitize(raw), ms
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        log.error("Normalize call failed", model=model, error=str(e))
        return "NO_LOCATION", ms


def _sanitize(raw: str) -> str:
    """Clean up model output — handles fences, prefixes, multi-line, etc."""
    if not raw:
        return "NO_LOCATION"
    raw = re.sub(r"```[a-z]*", "", raw).strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            for key in ("address", "location", "normalized", "result"):
                if key in obj:
                    raw = str(obj[key])
                    break
        except Exception:
            pass
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return "NO_LOCATION"
    raw = lines[0]
    for prefix in ("the address is", "the location is", "normalized address:",
                   "address:", "location:", "result:", "output:", "answer:"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):].strip()
            break
    raw = raw.strip("\"'`")
    raw = re.split(r"\.\s+[A-Z]", raw)[0].rstrip(".")
    if len(raw) > 120:
        raw = raw[:120]
    words = raw.split()
    if len(words) > 12 or (len(words) > 3 and not any(
        c.isdigit() or w.lower() in (
            "road", "avenue", "street", "drive", "boulevard", "highway",
            "mile", "rd", "ave", "st", "dr", "blvd", "hwy", "no_location",
        )
        for c in raw for w in words
    )):
        return "NO_LOCATION"
    return raw if raw else "NO_LOCATION"


def _log_shadow(
    chunk_id: str | None,
    feed_id:  str,
    transcript: str,
    base_result: str, base_ms: int,
    lora_result: str, lora_ms: int,
):
    """Write a shadow comparison row to Postgres (fire-and-forget)."""
    from geocode.geocoder import geocode

    try:
        base_geo = geocode(base_result) if base_result != "NO_LOCATION" else {}
        lora_geo = geocode(lora_result) if lora_result != "NO_LOCATION" else {}

        with _get_db_engine().connect() as conn:
            conn.execute(text("""
                INSERT INTO shadow_log (
                    chunk_id, feed_id, transcript,
                    base_result, lora_result,
                    base_geo_conf, lora_geo_conf,
                    base_geo_source, lora_geo_source,
                    base_lat, base_lng,
                    lora_lat, lora_lng,
                    base_ms, lora_ms
                ) VALUES (
                    :chunk_id, :feed_id, :transcript,
                    :base_result, :lora_result,
                    :base_geo_conf, :lora_geo_conf,
                    :base_geo_source, :lora_geo_source,
                    :base_lat, :base_lng,
                    :lora_lat, :lora_lng,
                    :base_ms, :lora_ms
                )
            """), {
                "chunk_id":        chunk_id,
                "feed_id":         feed_id,
                "transcript":      transcript[:500],
                "base_result":     base_result,
                "lora_result":     lora_result,
                "base_geo_conf":   base_geo.get("confidence"),
                "lora_geo_conf":   lora_geo.get("confidence"),
                "base_geo_source": base_geo.get("source"),
                "lora_geo_source": lora_geo.get("source"),
                "base_lat":        base_geo.get("lat"),
                "base_lng":        base_geo.get("lng"),
                "lora_lat":        lora_geo.get("lat"),
                "lora_lng":        lora_geo.get("lng"),
                "base_ms":         base_ms,
                "lora_ms":         lora_ms,
            })
            conn.commit()
            log.debug("Shadow row written",
                      feed_id=feed_id,
                      base=base_result[:40],
                      lora=lora_result[:40],
                      agreed=(base_result == lora_result))
    except Exception as e:
        log.warning("Shadow log write failed", error=str(e), feed_id=feed_id,
                    base=base_result[:40], lora=lora_result[:40])


def normalize_address(
    transcript:   str,
    feed_id:      str = "",
    chunk_id:     str | None = None,
) -> str:
    """
    Normalize a raw scanner transcript to a geocodable address string.

    Behaviour controlled by .env flags:
        LORA_NORMALIZE_ENABLED=false  → base model only
        LORA_SHADOW_MODE=true         → both models, base result used, diff logged
        LORA_NORMALIZE_ENABLED=true   → LoRA result used
    """
    global _cb_failure_count, _cb_open_until

    # Circuit breaker check
    if time.time() < _cb_open_until:
        log.warning("Normalize circuit breaker OPEN — returning NO_LOCATION")
        return "NO_LOCATION"

    # ── Base model call (always runs) ─────────────────────────────────────────
    t_base_start = time.time()
    base_result, base_ms = _call_model(BASE_MODEL, transcript)
    _counters["normalize_total"]      += 1
    _counters["norm_latency_ms_sum"]  += base_ms
    if base_result == "NO_LOCATION":
        _counters["normalize_no_loc"] += 1

    if base_result == "ERROR":
        _cb_failure_count += 1
        if _cb_failure_count >= CB_FAILURE_THRESHOLD:
            _cb_open_until = time.time() + CB_OPEN_SECONDS
            log.error("Normalize circuit breaker OPENED",
                      failures=_cb_failure_count)
        return "NO_LOCATION"
    else:
        _cb_failure_count = 0

    # ── Production LoRA path ──────────────────────────────────────────────────
    if LORA_ENABLED:
        lora_result, lora_ms = _call_model(LORA_MODEL, transcript)
        if lora_result != "ERROR":
            log.debug("normalize_address via LoRA",
                      feed_id=feed_id,
                      result=lora_result,
                      lora_ms=lora_ms)
            return lora_result
        else:
            log.warning("LoRA call failed, falling back to base", feed_id=feed_id)
            return base_result

    # ── Shadow mode path ──────────────────────────────────────────────────────
    if SHADOW_MODE:
        try:
            lora_result, lora_ms = _call_model(LORA_MODEL, transcript)
            if lora_result != "ERROR":
                if lora_result != base_result:
                    _counters["shadow_disagree"] += 1
                    log.info("Shadow divergence",
                             feed_id    = feed_id,
                             base       = base_result,
                             lora       = lora_result,
                             base_ms    = base_ms,
                             lora_ms    = lora_ms,
                             transcript = transcript[:80])
                # Log ALL comparisons to DB — not just disagreements.
                # Agreements are needed for the shadow_analysis agreement rate metric.
                _log_shadow(
                    chunk_id    = chunk_id,
                    feed_id     = feed_id,
                    transcript  = transcript,
                    base_result = base_result,
                    base_ms     = base_ms,
                    lora_result = lora_result,
                    lora_ms     = lora_ms,
                )
        except Exception as e:
            log.warning("Shadow LoRA call failed", error=str(e))
        # Always return base result in shadow mode
        return base_result

    # ── Base-only path ────────────────────────────────────────────────────────
    return base_result


def get_counters() -> dict:
    """Return current counters (called by metrics_collector.py)."""
    return dict(_counters)


def reset_counters():
    """Reset after metrics_collector flushes to DB."""
    global _counters
    _counters = {k: 0 for k in _counters}