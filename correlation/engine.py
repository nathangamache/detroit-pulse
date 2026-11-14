"""Correlation: attach each extracted transmission to an incident, deterministically.

Postgres is the single source of truth for incidents (the old design kept live state
in Redis and let the two diverge: the Redis resolver never updated Postgres, and two
conflicting INCIDENT_TTL defaults decided when things closed).

Per transmission:
  * incident-bearing (incident_type != NONE):
        a unit already on an active incident from the same channel within 30 minutes (compatible
        type, within 3 km) -> UPDATE; otherwise score active incidents updated within INCIDENT_CANDIDATE_WINDOW_S with the
        Layer-1 signals (correlation/signals.py: address overlap, distance, shared units,
        type, burst window, same channel, age penalties); top >= CORRELATION_MATCH_THRESHOLD
        -> UPDATE, otherwise NEW when there is something to anchor it (location, coordinates
        or units), else UNASSOCIATED. The old Ollama CONFIRM/VETO and MATCH/NEW/RETRY calls
        (and the RETRY queue that lost chunks) are gone.
  * status traffic (NONE) naming units already on an active incident from the same
    channel within 30 minutes -> UPDATE (joins the timeline); "clear/in service/available"
    releases those units, and an incident with no units left -> RESOLVE.
  * routine railroad operations stay UNASSOCIATED (served by /v1/rail/activity);
    RAIL_INCIDENT or HIGH-priority rail items become incidents.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from core.config_files import Channel, haversine_km
from core.settings import get_settings
from correlation.signals import score_all_candidates
from extract.schema import ItemResult
from geocode.base import GeoResult

INCOMPATIBLE_TYPE_PAIRS = {
    frozenset(p) for p in [
        ("STRUCTURE_FIRE", "MEDICAL"), ("STRUCTURE_FIRE", "SHOOTING"), ("STRUCTURE_FIRE", "WELFARE_CHECK"),
        ("STRUCTURE_FIRE", "ASSAULT"), ("VEHICLE_FIRE", "MEDICAL"), ("VEHICLE_FIRE", "WELFARE_CHECK"),
        ("SHOOTING", "WELFARE_CHECK"), ("SHOOTING", "MEDICAL"), ("PURSUIT", "WELFARE_CHECK"), ("PURSUIT", "MEDICAL"),
        ("HAZMAT", "ASSAULT"), ("HAZMAT", "SHOOTING"), ("BOMB_THREAT", "MEDICAL"), ("BOMB_THREAT", "WELFARE_CHECK"),
        ("RAIL_INCIDENT", "SHOOTING"), ("RAIL_INCIDENT", "DOMESTIC"),
    ]
}
PRIORITY_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
CONF_RANK = {"FAILED": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
CLEAR_RE = re.compile(r"\b(?:clear(?:ing|ed)?|in\s+service|available|back\s+in\s+quarters|returning\s+to\s+quarters|"
                      r"cancel(?:led)?|disregard|no\s+transport)\b", re.I)
STATUS_ATTACH_WINDOW = timedelta(minutes=30)


@dataclass
class Decision:
    action: str                       # NEW | UPDATE | RESOLVE | UNASSOCIATED
    incident_id: str | None = None
    score: float | None = None
    reasons: list[str] = field(default_factory=list)


def namespace_units(units: list[str], channel: Channel | None, channel_id: str) -> list[str]:
    prefix = (channel.unit_prefix if channel and channel.unit_prefix else channel_id).upper()
    out = []
    for u in units:
        u = u.split(":", 1)[1] if ":" in u else u
        ns = f"{prefix}:{u.strip()}"
        if ns not in out:
            out.append(ns)
    return out


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else (v or "")


def _candidates(c: Connection, at: datetime, window_s: int, limit: int = 200) -> list[dict]:
    rows = c.execute(text("""
        SELECT incident_id, feed_id, channel_id, incident_type, priority, address_raw, address_full, lat, lng,
               units, units_cleared, opened_at, last_updated, geocode_confidence, is_location_inferred
          FROM incidents
         WHERE status = 'ACTIVE' AND last_updated >= :since
         ORDER BY last_updated DESC LIMIT :lim"""), {"since": at - timedelta(seconds=window_s), "lim": limit}).mappings()
    out = []
    for r in rows:
        d = dict(r)
        d["incident_id"] = str(d["incident_id"])
        d["opened_at"] = _iso(d["opened_at"])
        d["last_updated"] = _iso(d["last_updated"])
        d["units"] = list(d["units"] or [])
        d["units_cleared"] = list(d["units_cleared"] or [])
        out.append(d)
    return out


def decide(c: Connection, item: ItemResult, *, channel_id: str, channel: Channel | None, transcript: str,
           geo: GeoResult, captured_at: datetime) -> Decision:
    s = get_settings()
    units_ns = namespace_units(item.units, channel, channel_id)
    cands = _candidates(c, captured_at, s.incident_candidate_window_s)

    if not item.has_incident:
        if not units_ns:
            return Decision("UNASSOCIATED", reasons=["no incident, no units"])
        for inc in cands:
            if inc.get("channel_id", inc["feed_id"]) != channel_id:
                continue
            last = datetime.fromisoformat(inc["last_updated"])
            if captured_at - last > STATUS_ATTACH_WINDOW:
                continue
            shared = set(inc["units"]) & set(units_ns)
            if shared:
                if CLEAR_RE.search(transcript):
                    remaining = set(inc["units"]) - set(inc["units_cleared"]) - shared
                    action = "RESOLVE" if not remaining else "UPDATE"
                    return Decision(action, inc["incident_id"], 1.0, [f"units cleared: {sorted(shared)}"])
                return Decision("UPDATE", inc["incident_id"], 1.0, [f"status from {sorted(shared)}"])
        return Decision("UNASSOCIATED", reasons=["status traffic, no matching active incident"])

    if item.is_rail and item.incident_type == "RAIL_OPERATIONS" and item.priority != "HIGH":
        return Decision("UNASSOCIATED", reasons=["routine rail operations"])

    # unit continuity (the old engine's unit-lookup step): same channel, shared unit, recent,
    # compatible type, and not far away when both sides have coordinates
    for inc in cands:
        if (inc.get("channel_id") or inc["feed_id"]) != channel_id:
            continue
        if captured_at - datetime.fromisoformat(inc["last_updated"]) > STATUS_ATTACH_WINDOW:
            continue
        shared = set(inc["units"]) & set(units_ns)
        if not shared or frozenset({item.incident_type, inc["incident_type"]}) in INCOMPATIBLE_TYPE_PAIRS:
            continue
        if geo.found and inc.get("lat") is not None and haversine_km(geo.lat, geo.lng, inc["lat"], inc["lng"]) > 3.0:
            continue
        return Decision("UPDATE", inc["incident_id"], 0.9, [f"unit continuity {sorted(shared)}"])

    incoming_addr = item.location.display() or ""
    scored = score_all_candidates(
        incoming_transcript=transcript, incoming_addr=incoming_addr, geocode_result=geo.to_dict(),
        units_added_ns=units_ns, incident_type=item.incident_type, feed_id=channel_id,
        feed_is_high_volume=False,
        active_incidents=[{**i, "feed_id": i.get("channel_id") or i["feed_id"]} for i in cands],
        incompatible_pairs=INCOMPATIBLE_TYPE_PAIRS, now=captured_at)
    if scored and scored[0]["signals"].signal_score >= s.correlation_match_threshold:
        top = scored[0]
        return Decision("UPDATE", top["incident"]["incident_id"], round(top["signals"].signal_score, 3),
                        top["signals"].score_reasons)
    if item.location.usable or geo.found or units_ns:
        best = round(scored[0]["signals"].signal_score, 3) if scored else None
        return Decision("NEW", str(uuid.uuid4()), best, ["below match threshold" if scored else "no candidates"])
    return Decision("UNASSOCIATED", reasons=["incident without location, coordinates or units"])


def apply(c: Connection, d: Decision, item: ItemResult, *, chunk_id: str, channel_id: str,
          channel: Channel | None, transcript: str, geo: GeoResult, captured_at: datetime,
          inferred: GeoResult | None = None) -> dict | None:
    """Write the decision to incidents and transcript_chunks. Returns the incident row (dict) if any."""
    units_ns = namespace_units(item.units, channel, channel_id)
    use_geo = geo if geo.found else (inferred if inferred and inferred.found else None)
    lat = use_geo.lat if use_geo else None
    lng = use_geo.lng if use_geo else None
    loc = item.location
    inc_row = None
    if d.action == "NEW":
        c.execute(text("""
            INSERT INTO incidents (incident_id, feed_id, channel_id, county, status, opened_at, last_updated,
                incident_type, priority, address_raw, address_full, city, lat, lng, location, units, units_cleared,
                summary, short_summary, loc_kind, loc_street1, loc_street2, loc_number, loc_city, loc_raw,
                geocode_source, geocode_confidence, is_location_inferred, is_rail, rail)
            VALUES (:id, :ch, :ch, :county, 'ACTIVE', :at, :at, :type, :prio, :araw, :afull, :city, :lat, :lng,
                ST_GeomFromEWKT(:ewkt), :units, '{}', NULL, :ss, :lk, :l1, :l2, :ln, :lc, :lr, :gsrc, :gconf, :inferred, :is_rail,
                CAST(:rail AS jsonb))"""), {
            "id": d.incident_id, "ch": channel_id, "county": (channel.county if channel and channel.county else ""),
            "at": captured_at, "type": item.incident_type, "prio": item.priority, "araw": loc.raw,
            "afull": (use_geo.formatted if use_geo and use_geo.formatted else loc.display()),
            "city": loc.city or (channel.city if channel else None), "lat": lat, "lng": lng, "units": units_ns,
            "ewkt": ewkt(lat, lng),
            "ss": item.short_summary, "lk": loc.kind if loc.usable else None, "l1": loc.street1, "l2": loc.street2,
            "ln": loc.number, "lc": loc.city, "lr": loc.raw, "gsrc": use_geo.source if use_geo else None,
            "gconf": use_geo.confidence if use_geo else None, "inferred": use_geo is inferred and use_geo is not None,
            "is_rail": item.is_rail, "rail": json.dumps(item.rail) if item.rail else None})
    elif d.action in ("UPDATE", "RESOLVE") and d.incident_id:
        cur = c.execute(text("""SELECT priority, incident_type, geocode_confidence, lat, units, units_cleared
                                  FROM incidents WHERE incident_id = :id FOR UPDATE"""), {"id": d.incident_id}).first()
        if cur is None:
            d.action, d.incident_id = "UNASSOCIATED", None
        else:
            prio = item.priority if PRIORITY_RANK.get(item.priority, 0) > PRIORITY_RANK.get(cur[0], 0) else cur[0]
            itype = item.incident_type if cur[1] in ("UNKNOWN", "OTHER") and item.has_incident else cur[1]
            better_loc = use_geo is not None and (cur[3] is None or
                                                  CONF_RANK.get(use_geo.confidence, 0) > CONF_RANK.get(cur[2] or "FAILED", 0))
            units = list(dict.fromkeys(list(cur[4] or []) + units_ns))
            cleared = list(cur[5] or [])
            if d.action == "RESOLVE" or (not item.has_incident and CLEAR_RE.search(transcript)):
                cleared = list(dict.fromkeys(cleared + [u for u in units_ns if u in units]))
            params = {"id": d.incident_id, "at": captured_at, "prio": prio, "type": itype, "units": units,
                      "cleared": cleared, "ss": item.short_summary if item.has_incident else None}
            c.execute(text("""
                UPDATE incidents SET last_updated = GREATEST(last_updated, :at), priority = :prio,
                       incident_type = :type, units = :units, units_cleared = :cleared,
                       short_summary = coalesce(:ss, short_summary)
                 WHERE incident_id = :id"""), params)
            if better_loc:
                c.execute(text("""
                    UPDATE incidents SET lat = :lat, lng = :lng, location = ST_GeomFromEWKT(:ewkt),
                           address_full = coalesce(:afull, address_full), address_raw = coalesce(:araw, address_raw),
                           loc_kind = :lk, loc_street1 = :l1, loc_street2 = :l2, loc_number = :ln, loc_city = :lc,
                           loc_raw = :lr, geocode_source = :gsrc, geocode_confidence = :gconf,
                           is_location_inferred = :inferred
                     WHERE incident_id = :id"""), {
                    "id": d.incident_id, "lat": lat, "lng": lng, "ewkt": ewkt(lat, lng),
                    "afull": use_geo.formatted or loc.display(),
                    "araw": loc.raw, "lk": loc.kind if loc.usable else None, "l1": loc.street1, "l2": loc.street2,
                    "ln": loc.number, "lc": loc.city, "lr": loc.raw, "gsrc": use_geo.source,
                    "gconf": use_geo.confidence, "inferred": use_geo is inferred})
            if d.action == "RESOLVE":
                c.execute(text("UPDATE incidents SET status = 'RESOLVED', resolved_at = :at WHERE incident_id = :id"),
                          {"id": d.incident_id, "at": captured_at})
    c.execute(text("""
        UPDATE transcript_chunks SET incident_id = :iid, correlation_action = :a, correlation_method = 'signals_v5',
               correlation_confidence = :conf
         WHERE chunk_id = :cid"""), {"iid": d.incident_id if d.action != "UNASSOCIATED" else None, "a": d.action,
                                     "conf": None if d.score is None else f"{d.score:.3f}", "cid": chunk_id})
    if d.incident_id and d.action != "UNASSOCIATED":
        inc_row = incident_dict(c, d.incident_id)
    return inc_row


def ewkt(lat: float | None, lng: float | None) -> str | None:
    return None if lat is None or lng is None else f"SRID=4326;POINT({float(lng)} {float(lat)})"


def incident_dict(c: Connection, incident_id: str) -> dict | None:
    r = c.execute(text("""
        SELECT i.*, (SELECT count(*) FROM transcript_chunks t WHERE t.incident_id = i.incident_id) AS chunk_count
          FROM incidents i WHERE i.incident_id = :id"""), {"id": incident_id}).mappings().first()
    if r is None:
        return None
    d = {k: v for k, v in dict(r).items() if k != "location"}
    for k in ("opened_at", "last_updated", "resolved_at", "summary_updated_at"):
        d[k] = _iso(d.get(k)) or None
    d["incident_id"] = str(d["incident_id"])
    d["units"] = list(d.get("units") or [])
    d["units_cleared"] = list(d.get("units_cleared") or [])
    d["chunk_count"] = int(d.get("chunk_count") or 0)
    return d


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
