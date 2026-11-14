"""Periodic housekeeping: idle-resolve incidents (in Postgres, with a broadcast), recover stuck queue rows.

Replaces two old resolvers that disagreed: correlation/resolver.py only touched Redis
(default 4 h, and parsed "Z" timestamps as local time) and api/auto_resolver.py only
touched Postgres after 48 h, so resolved incidents lingered on the map. There is now ONE
idle timeout, INCIDENT_IDLE_RESOLVE_S (default 4 h), applied to Postgres, followed by a
summary-on-close and an incident:resolve event.
"""
from __future__ import annotations

import threading

import structlog
from sqlalchemy import text

from core import bus
from core.db import get_engine
from core.settings import get_settings
from core.workqueue import requeue_all_stale
from correlation.engine import incident_dict
from extract.summary import summarize_incident

log = structlog.get_logger()


def resolve_idle(summary_client=None, meter=None) -> list[str]:
    s = get_settings()
    with get_engine().begin() as c:
        ids = [str(r[0]) for r in c.execute(text("""
            UPDATE incidents SET status = 'RESOLVED', resolved_at = now()
             WHERE status = 'ACTIVE' AND last_updated < now() - make_interval(secs => :idle)
         RETURNING incident_id"""), {"idle": s.incident_idle_resolve_s})]
    for iid in ids:
        try:
            summarize_incident(iid, "resolve", client=summary_client, meter=meter)
        except Exception as e:  # noqa: BLE001
            log.warning("summary on close failed", incident_id=iid[:8], error=str(e))
        with get_engine().connect() as c:
            inc = incident_dict(c, iid)
        if inc:
            bus.publish_event("incident:resolve", inc)
    if ids:
        log.info("idle incidents resolved", count=len(ids))
    return ids


def housekeeping_once(summary_client=None, meter=None) -> dict:
    out = {"resolved": resolve_idle(summary_client, meter), "requeued": requeue_all_stale()}
    try:
        from geocode.cache import GeocodeCache
        out["geocache_purged"] = GeocodeCache().purge_expired()
    except Exception:  # noqa: BLE001
        out["geocache_purged"] = 0
    return out


def run_resolver_loop(stop: threading.Event | None = None, summary_client=None, meter=None) -> None:
    stop = stop or threading.Event()
    s = get_settings()
    while not stop.is_set():
        try:
            housekeeping_once(summary_client, meter)
        except Exception as e:  # noqa: BLE001
            log.error("housekeeping failed", error=str(e))
        stop.wait(s.resolver_interval_s)
