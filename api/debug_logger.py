import os
import json
import time
import logging
import logging.handlers
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

LOG_DIR      = Path("logs")
LOG_FILE     = LOG_DIR / "pipeline_debug.log"
MAX_BYTES    = 50 * 1024 * 1024   # 50MB per file
BACKUP_COUNT = 5                   # keep last 5 rotated files

LOG_DIR.mkdir(exist_ok=True)

# Set up a dedicated file logger — separate from structlog
_file_logger = logging.getLogger("detroit_pulse.debug")
_file_logger.setLevel(logging.DEBUG)
_file_logger.propagate = False   # don't double-log to console

_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes    = MAX_BYTES,
    backupCount = BACKUP_COUNT,
    encoding    = "utf-8",
)
_handler.setFormatter(logging.Formatter("%(message)s"))
_file_logger.addHandler(_handler)


def _format_event(event_type: str, data: dict) -> str:
    """
    Format a pipeline event as a human-readable, grep-friendly line.

    Format:
    [TIMESTAMP] [EVENT_TYPE] [FEED_ID] stage=X | key=value key=value ...

    Examples:
    [2026-03-15 20:05:29] [pipeline:debug] [wayneco_detroit_fire] stage=INGEST | status=complete chunk_duration_s=27.3
    [2026-03-15 20:05:29] [incident:new]   [wayneco_detroit_fire] type=STRUCTURE_FIRE priority=HIGH address=Fenkell Ave, Detroit, MI units=Ladder 26,Engine 1
    """
    ts      = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    feed_id = data.get("feed_id", "unknown")

    # Build key=value pairs from the most useful fields
    parts = []

    if event_type == "pipeline:debug":
        stage = data.get("stage", "")
        parts.append(f"stage={stage.upper()}")
        if data.get("status"):
            parts.append(f"status={data['status']}")
        if data.get("transcript"):
            t = data["transcript"].replace("\n", " ").strip()
            parts.append(f'transcript="{t[:120]}"')
        if data.get("normalized") and data["normalized"] != "NO_LOCATION":
            parts.append(f'normalized="{data["normalized"]}"')
        if data.get("lat"):
            parts.append(f"lat={data['lat']} lng={data['lng']}")
        if data.get("confidence"):
            parts.append(f"confidence={data['confidence']}")
        if data.get("source"):
            parts.append(f"source={data['source']}")
        if data.get("has_incident") is not None:
            parts.append(f"has_incident={data['has_incident']}")
        if data.get("incident_type"):
            parts.append(f"incident_type={data['incident_type']}")
        if data.get("correlation_action"):
            parts.append(f"correlation_action={data['correlation_action']}")
        if data.get("summary"):
            s = data["summary"].replace("\n", " ").strip()
            parts.append(f'summary="{s[:100]}"')
        if data.get("elapsed_s"):
            parts.append(f"elapsed={data['elapsed_s']}s")
        if data.get("chunk_duration_s"):
            parts.append(f"chunk_duration={data['chunk_duration_s']}s")
        if data.get("speech_ratio") is not None:
            parts.append(f"speech_ratio={data['speech_ratio']}%")
        if data.get("action"):
            parts.append(f"action={data['action']}")
        if data.get("incident_id"):
            parts.append(f"incident_id={data['incident_id'][:8]}...")

    elif event_type in ("incident:new", "incident:update",
                        "incident:resolve", "incident:unassociated"):
        if data.get("incident_type"):
            parts.append(f"type={data['incident_type']}")
        if data.get("priority"):
            parts.append(f"priority={data['priority']}")
        if data.get("address_full") or data.get("address_raw"):
            addr = data.get("address_full") or data.get("address_raw")
            parts.append(f'address="{addr}"')
        if data.get("lat"):
            parts.append(f"lat={data['lat']} lng={data['lng']}")
        if data.get("units"):
            units = data["units"] if isinstance(data["units"], list) \
                    else [data["units"]]
            parts.append(f"units={','.join(units)}")
        if data.get("summary"):
            s = data["summary"].replace("\n", " ").strip()
            parts.append(f'summary="{s[:100]}"')
        if data.get("incident_id"):
            parts.append(f"incident_id={data['incident_id'][:8]}...")
        if data.get("transcript"):
            t = data["transcript"].replace("\n", " ").strip()
            parts.append(f'transcript="{t[:120]}"')

    detail = " | ".join(parts) if parts else json.dumps(data)[:200]

    return (
        f"[{ts}] "
        f"[{event_type:<24}] "
        f"[{feed_id:<32}] "
        f"{detail}"
    )


def log_event(event_type: str, data: dict):
    """
    Write a pipeline event to the debug log file.
    Called from broadcaster.publish() so every event is captured.
    """
    try:
        line = _format_event(event_type, data)
        _file_logger.debug(line)
    except Exception:
        pass   # Never let logging crash the pipeline


def log_separator(label: str = ""):
    """Write a visual separator — useful for marking pipeline restarts."""
    ts  = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    sep = "─" * 80
    _file_logger.debug(f"\n{sep}")
    _file_logger.debug(f"[{ts}] ── {label} ──")
    _file_logger.debug(f"{sep}")
