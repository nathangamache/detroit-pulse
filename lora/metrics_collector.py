#!/usr/bin/env python3

import os
import threading
import time
from datetime import datetime, timezone

import structlog
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
log = structlog.get_logger()

_engine = None
_lock   = threading.Lock()
_thread: threading.Thread | None = None

FLUSH_INTERVAL_SECONDS = 3600  # hourly


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(os.getenv("DATABASE_URL"))
    return _engine


def _current_hour_bucket() -> str:
    """ISO timestamp truncated to the current hour."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:00:00+00:00")


def _get_model_version() -> str:
    """Read current active model version from .env flags."""
    lora_enabled = os.getenv("LORA_NORMALIZE_ENABLED", "false").lower() == "true"
    shadow       = os.getenv("LORA_SHADOW_MODE",       "false").lower() == "true"
    if lora_enabled:
        return os.getenv("LORA_NORMALIZE_MODEL", "lora-unknown")
    elif shadow:
        return "base+shadow"
    return "base"


def flush_counters():
    """
    Pull counters from normalize_address module and write to pipeline_metrics.
    Also pulls correlation counts from the correlation engine's Redis keys.
    """
    try:
        # Import counters from normalize module (in-process)
        from llm.normalize import get_counters, reset_counters
        counters = get_counters()
        reset_counters()
    except Exception as e:
        log.warning("Could not read normalize counters", error=str(e))
        counters = {}

    # Pull correlation counts from Redis
    corr_new          = 0
    corr_update       = 0
    corr_unassociated = 0
    geocode_high      = 0
    geocode_medium    = 0
    geocode_failed    = 0

    try:
        import redis as _redis
        r = _redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                            decode_responses=True)
        # These keys are incremented by the pipeline worker in its processing loop
        # (we add INCR calls in pipeline_worker_v3.py separately)
        corr_new          = int(r.getdel("metrics:corr:new")          or 0)
        corr_update       = int(r.getdel("metrics:corr:update")       or 0)
        corr_unassociated = int(r.getdel("metrics:corr:unassociated") or 0)
        geocode_high      = int(r.getdel("metrics:geo:high")          or 0)
        geocode_medium    = int(r.getdel("metrics:geo:medium")        or 0)
        geocode_failed    = int(r.getdel("metrics:geo:failed")        or 0)
    except Exception as e:
        log.warning("Could not read Redis metrics counters", error=str(e))

    bucket        = _current_hour_bucket()
    model_version = _get_model_version()

    row = {
        "bucket":              bucket,
        "model_version":       model_version,
        "normalize_total":     counters.get("normalize_total",    0),
        "normalize_no_loc":    counters.get("normalize_no_loc",   0),
        "geocode_high":        geocode_high,
        "geocode_medium":      geocode_medium,
        "geocode_failed":      geocode_failed,
        "corr_new":            corr_new,
        "corr_update":         corr_update,
        "corr_unassociated":   corr_unassociated,
        "norm_latency_ms_sum": counters.get("norm_latency_ms_sum", 0),
    }

    try:
        eng = get_engine()
        with eng.connect() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_metrics (
                    bucket, model_version,
                    normalize_total, normalize_no_loc,
                    geocode_high, geocode_medium, geocode_failed,
                    corr_new, corr_update, corr_unassociated,
                    norm_latency_ms_sum
                ) VALUES (
                    :bucket, :model_version,
                    :normalize_total, :normalize_no_loc,
                    :geocode_high, :geocode_medium, :geocode_failed,
                    :corr_new, :corr_update, :corr_unassociated,
                    :norm_latency_ms_sum
                )
                ON CONFLICT (bucket, model_version) DO UPDATE SET
                    normalize_total     = pipeline_metrics.normalize_total     + EXCLUDED.normalize_total,
                    normalize_no_loc    = pipeline_metrics.normalize_no_loc    + EXCLUDED.normalize_no_loc,
                    geocode_high        = pipeline_metrics.geocode_high        + EXCLUDED.geocode_high,
                    geocode_medium      = pipeline_metrics.geocode_medium      + EXCLUDED.geocode_medium,
                    geocode_failed      = pipeline_metrics.geocode_failed      + EXCLUDED.geocode_failed,
                    corr_new            = pipeline_metrics.corr_new            + EXCLUDED.corr_new,
                    corr_update         = pipeline_metrics.corr_update         + EXCLUDED.corr_update,
                    corr_unassociated   = pipeline_metrics.corr_unassociated   + EXCLUDED.corr_unassociated,
                    norm_latency_ms_sum = pipeline_metrics.norm_latency_ms_sum + EXCLUDED.norm_latency_ms_sum
            """), row)
            conn.commit()
        log.info("Pipeline metrics flushed",
                 bucket=bucket, model=model_version, **{
                     k: v for k, v in row.items()
                     if k not in ("bucket", "model_version")
                 })
    except Exception as e:
        log.error("Failed to flush pipeline metrics", error=str(e))


def _run_loop():
    """Background thread: flush every hour, aligned to clock hours."""
    while True:
        now   = time.time()
        # Sleep until the next hour boundary + 5 seconds
        next_hour = (now // 3600 + 1) * 3600 + 5
        sleep_for = next_hour - now
        log.debug("Metrics collector sleeping", seconds=int(sleep_for))
        time.sleep(sleep_for)
        flush_counters()


def start_metrics_collector():
    """Start the background flush thread. Call once from main.py lifespan."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run_loop, daemon=True, name="metrics-collector")
    _thread.start()
    log.info("Metrics collector started")


def get_recent_metrics(hours: int = 168) -> list[dict]:
    """
    Return hourly metrics for the last N hours.
    Used by the admin panel and promote_model.py to display trends.
    """
    try:
        eng = get_engine()
        with eng.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    bucket,
                    model_version,
                    normalize_total,
                    normalize_no_loc,
                    geocode_high,
                    geocode_medium,
                    geocode_failed,
                    corr_new,
                    corr_update,
                    corr_unassociated,
                    norm_latency_ms_sum,
                    CASE WHEN normalize_total > 0
                         THEN ROUND(geocode_high::NUMERIC / normalize_total, 4)
                         ELSE 0 END AS geo_high_rate,
                    CASE WHEN normalize_total > 0
                         THEN ROUND(normalize_no_loc::NUMERIC / normalize_total, 4)
                         ELSE 0 END AS no_loc_rate,
                    CASE WHEN (corr_new + corr_update) > 0
                         THEN ROUND(corr_update::NUMERIC / (corr_new + corr_update), 4)
                         ELSE 0 END AS merge_rate,
                    CASE WHEN normalize_total > 0
                         THEN norm_latency_ms_sum / normalize_total
                         ELSE 0 END AS avg_latency_ms
                FROM pipeline_metrics
                WHERE bucket >= NOW() - INTERVAL ':hours hours'
                ORDER BY bucket DESC, model_version
            """.replace(":hours", str(hours)))).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        log.error("Failed to read pipeline metrics", error=str(e))
        return []


if __name__ == "__main__":
    print("Flushing counters now (test run)...")
    flush_counters()
    print("Done. Check pipeline_metrics table.")

    print("\nRecent metrics (last 24h):")
    rows = get_recent_metrics(hours=24)
    for r in rows:
        print(f"  {r['bucket']}  model={r['model_version']}  "
              f"total={r['normalize_total']}  "
              f"geo_high={r['geo_high_rate']:.1%}  "
              f"merge={r['merge_rate']:.1%}")
