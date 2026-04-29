import os
import time
import structlog
from dotenv import load_dotenv
from correlation.incident_store import get_all_active, resolve
from correlation.unit_store import active_units, release

load_dotenv()
log = structlog.get_logger()

# Incidents with no updates for this many seconds are auto-resolved
STALE_INCIDENT_SECONDS = int(os.getenv("INCIDENT_TTL_SECONDS", "14400"))


def prune_stale_incidents() -> list[str]:
    """
    Find and resolve incidents that haven't been updated recently.
    Returns list of resolved incident IDs.
    """
    active = get_all_active()
    resolved_ids = []
    now = time.time()

    for incident in active:
        last_updated_str = incident.get("last_updated", "")
        try:
            import datetime
            last_updated = datetime.datetime.strptime(
                last_updated_str, "%Y-%m-%dT%H:%M:%SZ"
            ).timestamp()
            age_seconds = now - last_updated

            if age_seconds > STALE_INCIDENT_SECONDS:
                iid = incident["incident_id"]
                log.info(
                    "Auto-resolving stale incident",
                    incident_id=iid,
                    age_hours=round(age_seconds / 3600, 1),
                )
                resolve(iid, chunk_id="auto-resolver")
                resolved_ids.append(iid)

        except (ValueError, TypeError):
            continue

    return resolved_ids


def run_resolver_loop(interval_seconds: int = 300):
    """
    Run the resolver on a fixed interval.
    Designed to run as a lightweight background thread.
    """
    log.info("Resolver loop starting", interval_seconds=interval_seconds)
    while True:
        try:
            resolved = prune_stale_incidents()
            if resolved:
                log.info("Pruned stale incidents", count=len(resolved))
        except Exception as e:
            log.error("Resolver error", error=str(e))
        time.sleep(interval_seconds)
