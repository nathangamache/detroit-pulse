import json
import math
import os
import time
import uuid
import structlog
import redis
from dotenv import load_dotenv

from correlation.unit_store import (
    assign_many, lookup_many, release_many, active_units
)
from correlation.incident_store import (
    create  as create_incident,
    get     as get_incident,
    update  as update_incident,
    resolve as resolve_incident,
    get_all_active,
)
from correlation.signals import (
    score_all_candidates,
    THRESHOLD_STRONG_MATCH,
    THRESHOLD_WEAK_MATCH,
    THRESHOLD_AMBIGUOUS,
)
from correlation.retry_queue import push_retry, pop_retry_queue

load_dotenv()
log = structlog.get_logger()

REDIS_URL             = os.getenv("REDIS_URL", "redis://localhost:6379/0")
LOCATION_MATCH_RADIUS = float(os.getenv("LOCATION_MATCH_RADIUS_KM", "0.2"))
UNASSOCIATED_KEY      = "queue:unassociated"

INCIDENT_MAX_AGE_WEAK   = int(os.getenv("INCIDENT_MAX_AGE_WEAK_S",  "14400"))  # 4h
INCIDENT_MAX_AGE_STRONG = int(os.getenv("INCIDENT_MAX_AGE_STRONG_S", "28800")) # 8h

INCOMPATIBLE_TYPE_PAIRS = {
    frozenset({"STRUCTURE_FIRE",  "MEDICAL"}),
    frozenset({"STRUCTURE_FIRE",  "SHOOTING"}),
    frozenset({"STRUCTURE_FIRE",  "WELFARE_CHECK"}),
    frozenset({"STRUCTURE_FIRE",  "ASSAULT"}),
    frozenset({"VEHICLE_FIRE",    "MEDICAL"}),
    frozenset({"VEHICLE_FIRE",    "WELFARE_CHECK"}),
    frozenset({"SHOOTING",        "WELFARE_CHECK"}),
    frozenset({"SHOOTING",        "MEDICAL"}),
    frozenset({"PURSUIT",         "WELFARE_CHECK"}),
    frozenset({"PURSUIT",         "MEDICAL"}),
    frozenset({"HAZMAT",          "ASSAULT"}),
    frozenset({"HAZMAT",          "SHOOTING"}),
    frozenset({"BOMB_THREAT",     "MEDICAL"}),
    frozenset({"BOMB_THREAT",     "WELFARE_CHECK"}),
}

# NEW4 / P1 — feeds where unit matches must go through the LLM judge.
# County-wide and city-wide feeds carry many simultaneous calls.
FEEDS_REQUIRE_JUDGE_ON_UNIT_MATCH = {
    "wayneco_public_safety",
    "wayneco_downriver",
    "wayneco_detroit_police_dispatch",
    "wayneco_detroit_police_fire",
    "wayneco_detroit_fire",
    "wayneco_detroit_ems",
    "washtenaw_metro",
    "washtenaw_livingston",
    "wayneco_southwestern",
}

# NEW2 — plausibility check thresholds for unit matches
UNIT_MATCH_AGE_THRESHOLD_S  = int(os.getenv("UNIT_MATCH_AGE_THRESHOLD_S", "1800"))
UNIT_MATCH_MAX_DISTANCE_KM  = float(os.getenv("UNIT_MATCH_MAX_DISTANCE_KM", "3.0"))

# Fix #22 — agency prefix map for unit ID namespacing
FEED_AGENCY_PREFIX = {
    "wayneco_detroit_police":     "DPD",
    "wayneco_detroit_fire":       "DFD",
    "wayneco_detroit_ems":        "EMS",
    "wayneco_downriver":          "DNR",
    "wayneco_dearborn":           "DBN",
    "wayneco_westland":           "WLD",
    "wayneco_grossepointe":       "GPT",
    "wayneco_plymouthnorthville": "PLY",
    "wayneco_southwestern":       "SWN",
    "wayneco_romulus":            "ROM",
    "wayneco_northville":         "NVL",
    "wayneco_franklin":           "FRK",
    "wayneco_public_safety":      "WCO",
    "oaklandco":                  "OAK",
    "washtenaw":                  "WSH",
}


def _get_agency_prefix(feed_id: str) -> str:
    for key, prefix in FEED_AGENCY_PREFIX.items():
        if feed_id.startswith(key):
            return prefix
    return "UNK"


def _namespace_unit(unit_id: str, feed_id: str) -> str:
    """Fix #22 + double-namespace fix: strip existing prefix before adding."""
    if ":" in unit_id:
        unit_id = unit_id.split(":", 1)[1]
    return f"{_get_agency_prefix(feed_id)}:{unit_id}"


def _namespace_units(units: list, feed_id: str) -> list:
    return [_namespace_unit(u, feed_id) for u in units]


_redis_client: redis.Redis | None = None

DEBUG_CHANNEL = "detroit-pulse:debug"


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _publish_correlation_trace(
    chunk_id:      str,
    feed_id:       str,
    transcript:    str,
    incident_type: str,
    normalized:    str,
    geocoded:      str,
    units:         list,
    decision:      str,
    reason:        str,
    top_score:     float | None,
    top_candidate: dict | None,
    all_scores:    list | None,
    incident_id:   str | None = None,
    extra:         dict | None = None,
) -> None:
    """
    Publish a full correlation decision trace to the debug channel.
    Every chunk emits one trace event showing exactly why it was routed
    to NEW / UPDATE / UNASSOCIATED and what signals drove that decision.
    """
    try:
        candidate_summary = None
        if top_candidate:
            candidate_summary = {
                "id":      top_candidate.get("incident_id", "")[:8],
                "type":    top_candidate.get("incident_type"),
                "address": (top_candidate.get("address_full") or
                            top_candidate.get("address_raw")),
                "feed":    top_candidate.get("feed_id"),
            }

        scored_summary = None
        if all_scores:
            scored_summary = [
                {
                    "id":      sc["incident"]["incident_id"][:8],
                    "score":   round(sc["signals"].signal_score, 3),
                    "class":   sc["signals"].classify(),
                    "reasons": sc["signals"].score_reasons[:4],
                    "type":    sc["incident"].get("incident_type"),
                }
                for sc in all_scores[:6]
            ]

        payload = {
            "stage":         "correlation",
            "feed_id":       feed_id,
            "chunk_id":      chunk_id[:8],
            "transcript":    transcript[:200],
            "incident_type": incident_type,
            "normalized":    normalized,
            "geocoded":      geocoded,
            "units":         units,
            "decision":      decision,
            "reason":        reason,
            "top_score":     top_score,
            "top_candidate": candidate_summary,
            "all_scores":    scored_summary,
            "result_id":     incident_id[:8] if incident_id else None,
        }
        if extra:
            payload.update(extra)

        get_redis().publish(DEBUG_CHANNEL, json.dumps({
            "event":   "pipeline:debug",
            "feed_id": feed_id,
            "data":    payload,
            "ts":      time.time(),
        }))
    except Exception:
        pass  # Never let tracing crash the pipeline


# ── Helpers ───────────────────────────────────────────────────────────────

def _filter_units(units: list) -> list:
    from correlation.unit_store import _is_valid_unit_id
    return [u for u in units if _is_valid_unit_id(u)]


def _incident_accepts_weak_correlation(
    incident:       dict,
    incoming_type:  str | None = None,
    has_unit_match: bool = False,
) -> bool:
    """Fix #20 — age check uses last_updated."""
    from datetime import datetime, timezone

    timestamp_field = incident.get("last_updated") or incident.get("opened_at", "")
    if timestamp_field:
        try:
            if isinstance(timestamp_field, str):
                dt = datetime.fromisoformat(timestamp_field.replace("Z", "+00:00"))
                age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
            else:
                age_seconds = time.time() - float(timestamp_field)

            limit = INCIDENT_MAX_AGE_STRONG if has_unit_match else INCIDENT_MAX_AGE_WEAK
            if age_seconds > limit:
                return False
        except Exception:
            pass

    if incoming_type and not has_unit_match:
        existing_type = incident.get("incident_type", "UNKNOWN")
        if frozenset({incoming_type, existing_type}) in INCOMPATIBLE_TYPE_PAIRS:
            return False

    return True


def _haversine_km(lat1: float, lng1: float,
                   lat2: float, lng2: float) -> float:
    R       = 6371.0
    phi1    = math.radians(lat1)
    phi2    = math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _find_by_location(
    lat:        float,
    lng:        float,
    feed_id:    str | None = None,
    radius_km:  float      = LOCATION_MATCH_RADIUS,
    confidence: str        = "HIGH",
) -> dict | None:
    if confidence == "LOW":
        return None
    active   = get_all_active(feed_id=feed_id)
    closest  = None
    min_dist = float("inf")
    for incident in active:
        ilat = incident.get("lat")
        ilng = incident.get("lng")
        if ilat is None or ilng is None:
            continue
        dist = _haversine_km(lat, lng, ilat, ilng)
        if dist <= radius_km and dist < min_dist:
            min_dist = dist
            closest  = incident
    if closest:
        log.debug("Location proximity candidate",
                  incident_id=closest["incident_id"],
                  distance_km=round(min_dist, 3))
    return closest


def _unit_match_is_plausible(
    matched_incident: dict,
    incoming_lat:     float | None,
    incoming_lng:     float | None,
    feed_id:          str,
) -> tuple[bool, str]:
    """NEW2+NEW5 — check geographic and temporal plausibility of unit match."""
    from datetime import datetime, timezone

    inc_lat = matched_incident.get("lat")
    inc_lng = matched_incident.get("lng")

    if inc_lat is None or inc_lng is None:
        return True, ""
    if incoming_lat is None or incoming_lng is None:
        return True, ""

    try:
        dist_km = _haversine_km(
            incoming_lat, incoming_lng,
            float(inc_lat), float(inc_lng)
        )
    except Exception:
        return True, ""

    if dist_km > UNIT_MATCH_MAX_DISTANCE_KM:
        last_updated = (matched_incident.get("last_updated") or
                        matched_incident.get("opened_at", ""))
        age_seconds  = 0
        if last_updated:
            try:
                dt          = datetime.fromisoformat(
                    last_updated.replace("Z", "+00:00")
                )
                age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
            except Exception:
                pass

        reason = (
            f"unit match demoted: {dist_km:.1f}km away "
            f"(max {UNIT_MATCH_MAX_DISTANCE_KM}km), "
            f"incident {int(age_seconds/60)}min old"
        )
        return False, reason

    return True, ""


def _fetch_recent_chunks(incident_id: str, n: int = 5) -> list[str]:
    """Fetch last N raw transcripts for a candidate incident."""
    try:
        from llm.structure import fetch_recent_chunks_for_judge
        chunks = fetch_recent_chunks_for_judge(incident_id, n)
        return [c.get("raw_transcript", "") for c in chunks if c.get("raw_transcript")]
    except Exception as e:
        log.warning("fetch_recent_chunks failed", error=str(e))
        return []


def _is_noisy_chunk(
    geocode_result:         dict,
    units_added:            list,
    normalized_address:     str,
    unit_inferred_location,
    feed_id:                str = "",
) -> bool:
    """
    Fix #19 — quality gate for both NEW and UPDATE paths.
    NEW3 — stricter gate for county-wide feeds.
    """
    geocode_conf = geocode_result.get("confidence", "LOW")
    has_address  = normalized_address not in ("NO_LOCATION", None, "")

    # Standard gate: completely empty chunk
    if (geocode_conf == "FAILED" and
            not units_added and
            not has_address and
            not unit_inferred_location):
        return True

    # NEW3 — county-wide/city-wide feeds require a geocoded address or
    # unit-inferred location. Units alone are not sufficient to anchor
    # an incident on a feed that carries dozens of simultaneous calls.
    if feed_id in FEEDS_REQUIRE_JUDGE_ON_UNIT_MATCH:
        if not has_address and not unit_inferred_location:
            return True

    return False


def _addresses_clearly_different(addr1: str, addr2: str) -> bool:
    """
    Idea 6 — fast pre-filter. True when two addresses share zero
    meaningful tokens AND incident types are incompatible.
    """
    if not addr1 or not addr2:
        return False
    if addr1 in ("NO_LOCATION", "Unknown") or addr2 in ("NO_LOCATION", "Unknown"):
        return False

    stop = {
        "detroit", "michigan", "mi", "road", "rd", "avenue", "ave",
        "street", "st", "drive", "dr", "boulevard", "blvd", "lane",
        "ln", "highway", "hwy", "court", "ct", "place", "pl", "way",
        "north", "south", "east", "west", "n", "s", "e", "w",
        "and", "at", "the", "a", "of", "in", "on", "for",
        "plymouth", "northville", "livonia", "dearborn", "royal", "oak",
    }

    def tokens(addr):
        return {
            w.strip(".,()").lower()
            for w in addr.split()
            if len(w) > 2 and w.lower() not in stop
        }

    t1 = tokens(addr1)
    t2 = tokens(addr2)
    if not t1 or not t2:
        return False
    return len(t1 & t2) == 0


# ── Phase 1: LLM Arbitrator ───────────────────────────────────────────────

def _run_mode_a_sanity_check(
    transcript:        str,
    geocoded_address:  str,
    incident_type:     str,
    feed_id:           str,
    candidate:         dict,
    signal_score:      float,
    score_reasons:     list,
) -> bool:
    """
    P1-3 Mode A — fast sanity check for STRONG_MATCH candidates.
    Returns True (CONFIRM) or False (VETO).
    The LLM only checks whether the algorithmic conclusion is obviously wrong.
    """
    from llm.structure import get_client
    import os

    inc_type = candidate.get("incident_type", "UNKNOWN")
    inc_addr = candidate.get("address_full") or candidate.get("address_raw") or "Unknown"
    last_upd = candidate.get("last_updated") or candidate.get("opened_at", "")

    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(last_upd.replace("Z", "+00:00"))
        age_min = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    except Exception:
        age_min = 0

    system = (
        "You are a dispatch correlation verifier.\n"
        "The algorithmic system scored this match {:.2f}/1.00 — very high confidence.\n"
        "The score reflects: same address, same location, same incident type.\n\n"
        "Your ONLY job: VETO if and only if this is EGREGIOUSLY WRONG.\n"
        "Answer CONFIRM or VETO only.\n\n"
        "Answer CONFIRM in ALL of these cases:\n"
        "- Same street or intersection mentioned\n"
        "- Same type of incident (both fires, both medicals, etc.)\n"
        "- Any narrative connection to the existing incident\n"
        "- You are unsure\n\n"
        "Answer VETO ONLY if:\n"
        "- Completely different city (e.g. existing=Detroit, new=Ann Arbor)\n"
        "- Completely incompatible types (e.g. existing=STRUCTURE_FIRE, new=MEDICAL)\n\n"
        "The algorithm has very high confidence. Default to CONFIRM."
    ).format(signal_score)

    user = (
        f"Existing incident: {inc_type} at {inc_addr} (opened {age_min}min ago)\n"
        f"Algorithmic signals: {', '.join(score_reasons[:5])}\n\n"
        f"New transmission ({feed_id}):\n{transcript[:300]}\n"
        f"Geocoded: {geocoded_address}\n\n"
        f"Is this match OBVIOUSLY WRONG? Answer CONFIRM or VETO."
    )

    try:
        client   = get_client()
        qwen     = os.getenv("QWEN_MODEL", "qwen2.5:7b-instruct")
        response = client.chat(
            model    = qwen,
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            options  = {"temperature": 0.0, "num_predict": 5},
        )
        answer = response["message"]["content"].strip().upper()
        is_confirm = answer.startswith("CONFIRM") or not answer.startswith("VETO")
        log.info("Mode A sanity check",
                 result     = "CONFIRM" if is_confirm else "VETO",
                 incident_id = candidate["incident_id"][:8],
                 score      = round(signal_score, 3))
        return is_confirm
    except Exception as e:
        log.warning("Mode A sanity check failed — defaulting to CONFIRM", error=str(e))
        return True


def _run_mode_b_arbitration(
    transcript:       str,
    normalized_addr:  str,
    geocoded_address: str,
    geocode_result:   dict,
    incident_type:    str,
    units_added:      list,
    feed_id:          str,
    scored_candidates: list[dict],
    retry_attempt:    int = 0,
) -> dict:
    """
    P1-4 Mode B — full LLM arbitration for WEAK_MATCH/AMBIGUOUS cases.
    Returns: {"decision": "MATCH"|"NEW"|"RETRY", "incident_id": str|None}
    """
    from llm.structure import get_client
    import os

    if not scored_candidates:
        return {"decision": "NEW", "incident_id": None}

    lat = geocode_result.get("lat")
    lng = geocode_result.get("lng")

    # Build candidate context text
    candidate_lines = []
    for i, sc in enumerate(scored_candidates[:4], 1):
        inc     = sc["incident"]
        sig     = sc["signals"]
        iid     = inc["incident_id"]
        recent  = _fetch_recent_chunks(iid, n=3)
        recent_text = "\n".join(
            f"    [{j+1}] {t[:120]}" for j, t in enumerate(recent)
        ) or "    (no prior chunks)"

        # Distance string
        inc_lat = inc.get("lat")
        inc_lng = inc.get("lng")
        if lat and lng and inc_lat and inc_lng:
            try:
                dist_km  = _haversine_km(lat, lng, float(inc_lat), float(inc_lng))
                dist_str = f"{dist_km:.2f}km"
            except Exception:
                dist_str = "unknown"
        else:
            dist_str = "unknown"

        candidate_lines.append(
            f"[{i}] incident_id={iid[:8]}\n"
            f"    L1_score={sig.signal_score:.2f} | "
            f"signals={', '.join(sig.score_reasons[:4])}\n"
            f"    Type={inc.get('incident_type')} | "
            f"Address={inc.get('address_full') or 'unknown'}\n"
            f"    Distance={dist_str} | "
            f"Age={int(sig.incident_age_seconds/60)}min\n"
            f"    Recent transcripts:\n{recent_text}"
        )

    candidates_text = "\n\n".join(candidate_lines)

    retry_note = (
        f"\nNOTE: This chunk has already been retried {retry_attempt} time(s). "
        "Do NOT output RETRY again — choose MATCH or NEW."
        if retry_attempt >= 2 else ""
    )

    system = (
        "You are a Detroit metro dispatch correlation engine.\n\n"
        "IMPORTANT: A single radio channel carries MULTIPLE SIMULTANEOUS CALLS.\n"
        "A new transmission is more likely a DIFFERENT call than the same one\n"
        "unless there is explicit shared evidence.\n\n"
        "OUTPUT exactly one of:\n"
        "  MATCH:<incident_id>   — same incident (use the 8-char ID prefix shown)\n"
        "  NEW                   — different incident\n"
        "  RETRY                 — genuinely ambiguous, need more context\n\n"
        "Use MATCH only with EXPLICIT evidence:\n"
        "  - Same specific address or cross-street\n"
        "  - Same unit ID mentioned\n"
        "  - Clear narrative continuation of the same event\n\n"
        "Use NEW when:\n"
        "  - Different location\n"
        "  - Different incident type\n"
        "  - No shared units, no shared location\n"
        "  - You are uncertain — default to NEW, not MATCH\n\n"
        "Use RETRY only when the transmission is too ambiguous to decide\n"
        "and MORE CONTEXT from the next transmission would genuinely help.\n"
        "Do NOT use RETRY for transmissions that are clearly NEW incidents." +
        retry_note
    )

    user = (
        f"NEW TRANSMISSION:\n"
        f"Feed: {feed_id}\n"
        f"Transcript: {transcript[:400]}\n"
        f"Geocoded: {geocoded_address or normalized_addr}\n"
        f"Type: {incident_type}\n"
        f"Units: {', '.join(units_added) or 'none'}\n\n"
        f"CANDIDATES (ranked by algorithmic score):\n\n"
        f"{candidates_text}\n\n"
        f"Decision:"
    )

    try:
        client   = get_client()
        qwen     = os.getenv("QWEN_MODEL", "qwen2.5:7b-instruct")
        response = client.chat(
            model    = qwen,
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            options  = {"temperature": 0.0, "num_predict": 20},
        )
        raw = response["message"]["content"].strip().upper()
        result = _parse_arbitrator_response(raw, scored_candidates)
        log.info("Mode B arbitration result",
                 decision           = result["decision"],
                 incident_id        = (result.get("incident_id") or "")[:8],
                 raw_response       = raw[:40],
                 candidates_shown   = min(4, len(scored_candidates)),
                 candidates_total   = len(scored_candidates),
                 retry_attempt      = retry_attempt)
        return result
    except Exception as e:
        log.warning("Mode B arbitration failed — defaulting to NEW", error=str(e))
        return {"decision": "NEW", "incident_id": None}


def _parse_arbitrator_response(
    raw: str,
    scored_candidates: list[dict],
) -> dict:
    """
    Parse LLM arbitrator output.
    Handles MATCH:<id>, MATCH <id>, NEW, RETRY.
    Falls back to NEW on any unclear output.
    """
    raw = raw.strip().upper()

    if raw.startswith("MATCH"):
        # Extract incident_id — model may output full UUID or 8-char prefix
        parts = raw.replace("MATCH:", "MATCH ").split(None, 1)
        iid_fragment = parts[1].strip()[:36].lower() if len(parts) > 1 else ""

        # Try to match against known candidates
        for sc in scored_candidates:
            cand_id = sc["incident"]["incident_id"]
            if (iid_fragment and
                    (cand_id.startswith(iid_fragment) or
                     iid_fragment in cand_id)):
                return {"decision": "MATCH", "incident_id": cand_id}

        # Fragment didn't match any candidate — use top scored candidate
        if scored_candidates:
            top_id = scored_candidates[0]["incident"]["incident_id"]
            log.debug("MATCH response ID not matched — using top candidate",
                      fragment=iid_fragment, top_id=top_id[:8])
            return {"decision": "MATCH", "incident_id": top_id}

        return {"decision": "NEW", "incident_id": None}

    if raw.startswith("RETRY"):
        return {"decision": "RETRY", "incident_id": None}

    # NEW or anything else
    return {"decision": "NEW", "incident_id": None}


# ── Main correlation entry point ──────────────────────────────────────────

def correlate(
    chunk_id:               str,
    feed_id:                str,
    county:                 str,
    transcript:             str,
    structured:             dict,
    normalized_address:     str,
    geocode_result:         dict,
    unit_inferred_location: dict | None = None,
    retry_attempt:          int = 0,
) -> dict:
    """
    Phase 1 three-layer correlation engine.

    Layer 1: Deterministic signal scoring (microseconds)
    Layer 3: LLM arbitration — Mode A (sanity check) or Mode B (full)
    RETRY:   ambiguous chunks queued per-feed for next chunk context

    Returns: {"action": "NEW"|"UPDATE"|"RESOLVE"|"UNASSOCIATED"|"RETRY",
              "incident_id": str|None, "incident": dict|None}
    """
    if not structured.get("has_incident"):
        return {"action": "UNASSOCIATED", "incident_id": None, "incident": None}

    llm_action      = structured.get("correlation_action", "UNASSOCIATED")
    units_added     = _filter_units(structured.get("units_added", []))
    units_cleared   = _filter_units(structured.get("units_cleared", []))

    units_added_ns   = _namespace_units(units_added, feed_id)
    units_cleared_ns = _namespace_units(units_cleared, feed_id)
    all_units_ns     = list(set(units_added_ns + units_cleared_ns))

    summary       = structured.get("summary_update", "")
    incident_type = structured.get("incident_type", "UNKNOWN")
    priority      = structured.get("priority", "UNKNOWN")

    lat = geocode_result.get("lat")
    lng = geocode_result.get("lng")

    search_lat = lat if lat is not None else (
        unit_inferred_location["lat"] if unit_inferred_location else None
    )
    search_lng = lng if lng is not None else (
        unit_inferred_location["lng"] if unit_inferred_location else None
    )

    geocode_confidence  = geocode_result.get("confidence", "LOW")
    feed_is_high_volume = feed_id in FEEDS_REQUIRE_JUDGE_ON_UNIT_MATCH

    # ── Quality gate ──────────────────────────────────────────────────
    if _is_noisy_chunk(geocode_result, units_added, normalized_address,
                       unit_inferred_location, feed_id=feed_id):
        log.info("Suppressing — noisy chunk",
                 feed_id=feed_id, transcript=transcript[:80])
        get_redis().lpush(UNASSOCIATED_KEY, json.dumps({
            "chunk_id":   chunk_id,
            "feed_id":    feed_id,
            "transcript": transcript,
            "timestamp":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reason":     "noisy_chunk",
        }))
        return {"action": "UNASSOCIATED", "incident_id": None, "incident": None}

    # ── Get all active incidents ───────────────────────────────────────
    all_active = get_all_active(feed_id=None)

    # ── Step 1: Unit ID match (hard match — only for low-volume feeds) ─
    # For high-volume feeds, unit matches go through Layer 1 + LLM judge
    matched_incident_id = None
    match_was_unit_id   = False

    if all_units_ns and not feed_is_high_volume:
        unit_matches = lookup_many(all_units_ns)
        if unit_matches:
            from collections import Counter
            counts       = Counter(unit_matches.values())
            candidate_id = counts.most_common(1)[0][0]
            matched_inc  = get_incident(candidate_id)

            if matched_inc and not _incident_accepts_weak_correlation(
                    matched_inc, incident_type, has_unit_match=True):
                log.info("Unit match rejected — incident too old/stale",
                         incident_id=candidate_id[:8])
                matched_inc = None

            if matched_inc:
                plausible, reason = _unit_match_is_plausible(
                    matched_inc, lat, lng, feed_id
                )
                if not plausible:
                    log.info("Unit match failed plausibility — routing to Layer 1",
                             reason=reason)
                    matched_inc = None

            if matched_inc:
                matched_incident_id = candidate_id
                match_was_unit_id   = True
                log.debug("Direct unit ID match (low-volume feed)",
                          matched_units=list(unit_matches.keys()),
                          incident_id=matched_incident_id)

    # ── Step 2: RESOLVE path (units_cleared) ──────────────────────────
    if units_cleared_ns and matched_incident_id:
        incident = get_incident(matched_incident_id)
        if incident:
            remaining = set(incident.get("units", [])) - set(units_cleared_ns)
            release_many(units_cleared_ns)
            if units_added_ns:
                assign_many(units_added_ns, matched_incident_id, feed_id=feed_id)
            if not remaining:
                incident = resolve_incident(matched_incident_id, chunk_id)
                if units_added_ns:
                    incident = update_incident(
                        matched_incident_id,
                        units_added   = units_added_ns,
                        units_cleared = [],
                        summary       = summary,
                        chunk_id      = chunk_id,
                        priority      = priority,
                    ) or incident
                return {"action": "RESOLVE",
                        "incident_id": matched_incident_id,
                        "incident": incident}
            else:
                incident = update_incident(
                    matched_incident_id,
                    units_added   = units_added_ns,
                    units_cleared = units_cleared_ns,
                    summary       = summary,
                    chunk_id      = chunk_id,
                    priority      = priority,
                )
                return {"action": "UPDATE",
                        "incident_id": matched_incident_id,
                        "incident": incident}

    # ── Step 3: Layer 1 signal scoring ────────────────────────────────
    # Score all active incidents using deterministic signals.
    #
    # NO_LOCATION gap fix: when normalized_address is NO_LOCATION/empty,
    # address token overlap is 0 and distance is undefined — every candidate
    # scores near 0 and gets auto-routed to NEW.  But same-feed, same-type
    # incidents within the burst window are very likely the same call.
    # We detect this and force them to AMBIGUOUS so the LLM judge sees them.
    no_location = (not normalized_address or
                   normalized_address in ("NO_LOCATION", "Unknown", ""))
    scored = score_all_candidates(
        incoming_transcript = transcript,
        incoming_addr       = (geocode_result.get("formatted") or
                               normalized_address or ""),
        geocode_result      = geocode_result,
        units_added_ns      = units_added_ns,
        incident_type       = incident_type,
        feed_id             = feed_id,
        feed_is_high_volume = feed_is_high_volume,
        active_incidents    = all_active,
        incompatible_pairs  = INCOMPATIBLE_TYPE_PAIRS,
    )

    # NO_LOCATION boost: when we have no address signal, same-feed + same-type
    # incidents within the burst window would have been scored near 0 and
    # skipped entirely. Boost them to AMBIGUOUS so the LLM judge can evaluate.
    if no_location and scored:
        boosted = False
        for sc in scored:
            sig = sc["signals"]
            cand = sc["incident"]
            same_feed = cand.get("feed_id") == feed_id
            same_type = cand.get("incident_type") == incident_type
            if same_feed and same_type and sig.is_burst_window:
                old_score = sig.signal_score
                sig.signal_score = max(sig.signal_score, THRESHOLD_AMBIGUOUS + 0.02)
                sig.score_reasons.insert(0, f"no_loc_type_feed_boost({old_score:.2f})")
                boosted = True
                log.info(
                    "NO_LOCATION: boosted same-feed/type/burst candidate to AMBIGUOUS",
                    candidate_id  = cand["incident_id"][:8],
                    incident_type = incident_type,
                    old_score     = round(old_score, 3),
                    new_score     = round(sig.signal_score, 3),
                    feed_id       = feed_id,
                )
        if boosted:
            scored.sort(key=lambda x: x["signals"].signal_score, reverse=True)

    # If we had a direct unit match (low-volume feed), that candidate gets
    # promoted to the top of the scored list for Mode A check
    if matched_incident_id:
        top_candidate = get_incident(matched_incident_id)
        if top_candidate:
            # Find it in scored list and pull to front
            scored = [s for s in scored
                      if s["incident"]["incident_id"] != matched_incident_id]
            from correlation.signals import compute_signals
            promoted_sig = compute_signals(
                incoming_transcript = transcript,
                incoming_addr       = (geocode_result.get("formatted") or
                                       normalized_address or ""),
                geocode_result      = geocode_result,
                units_added_ns      = units_added_ns,
                incident_type       = incident_type,
                feed_id             = feed_id,
                feed_is_high_volume = False,
                candidate_incident  = top_candidate,
                incompatible_pairs  = INCOMPATIBLE_TYPE_PAIRS,
            )
            # Boost signal score for confirmed unit match
            promoted_sig.signal_score = min(1.0,
                                            promoted_sig.signal_score + 0.20)
            promoted_sig.score_reasons.insert(0, "unit_match_confirmed")
            scored.insert(0, {"incident": top_candidate,
                               "signals": promoted_sig})

    # ── Step 3b: Same-unit burst boost for high-volume feeds ─────────────
    # High-volume feeds (DPD, Detroit Fire, Wayne Co.) skip the direct unit
    # match path (Step 1) and the unit_same_feed_bonus is penalized in Layer 1.
    # This means rapid repeat radio broadcasts (same unit, same feed, within
    # burst window) can score below AMBIGUOUS when geocode also produces
    # different strings for the same address — exactly the duplicate pattern
    # seen in practice.
    #
    # Fix: for high-volume feeds, if a candidate shares at least one unit AND
    # is in the burst window AND is the same type, boost to AMBIGUOUS so the
    # LLM judge evaluates it rather than auto-NEW.
    if feed_is_high_volume and units_added_ns and scored:
        for sc in scored:
            sig  = sc["signals"]
            cand = sc["incident"]
            if sig.signal_score >= THRESHOLD_AMBIGUOUS:
                break  # already above threshold — no boost needed
            candidate_units = set(cand.get("units", []))
            shared_units    = candidate_units & set(units_added_ns)
            same_type       = cand.get("incident_type") == incident_type
            same_feed_cand  = cand.get("feed_id") == feed_id
            if shared_units and sig.is_burst_window and same_feed_cand and same_type:
                old_score = sig.signal_score
                sig.signal_score = max(sig.signal_score, THRESHOLD_AMBIGUOUS + 0.02)
                sig.score_reasons.insert(0,
                    f"hv_unit_burst_boost({old_score:.2f},units={list(shared_units)[:2]})")
                log.info(
                    "High-volume feed: boosted same-unit/type/burst candidate to AMBIGUOUS",
                    candidate_id  = cand["incident_id"][:8],
                    incident_type = incident_type,
                    shared_units  = list(shared_units),
                    old_score     = round(old_score, 3),
                    new_score     = round(sig.signal_score, 3),
                    feed_id       = feed_id,
                )
        scored.sort(key=lambda x: x["signals"].signal_score, reverse=True)

    # ── Step 4: Route based on top candidate score ────────────────────
    if not scored or scored[0]["signals"].signal_score < THRESHOLD_AMBIGUOUS:
        top_score_val = scored[0]["signals"].signal_score if scored else 0.0
        log.info(
            "Layer 1: NO_MATCH — creating new incident",
            top_score       = round(top_score_val, 3),
            feed_id         = feed_id,
            incident_type   = incident_type,
            normalized      = normalized_address,
            candidates_seen = len(scored),
            no_location     = no_location,
            units           = units_added_ns,
        )
        result = _create_new_incident(
            chunk_id, feed_id, county, transcript, normalized_address,
            geocode_result, unit_inferred_location, units_added_ns,
            units_cleared_ns, incident_type, priority, summary, lat, lng,
        )
        _publish_correlation_trace(
            chunk_id      = chunk_id,
            feed_id       = feed_id,
            transcript    = transcript,
            incident_type = incident_type,
            normalized    = normalized_address,
            geocoded      = geocode_result.get("formatted", ""),
            units         = units_added_ns,
            decision      = "NEW",
            reason        = f"NO_MATCH (top_score={top_score_val:.3f}, "
                            f"candidates={len(scored)}, "
                            f"no_location={no_location})",
            top_score     = top_score_val,
            top_candidate = scored[0]["incident"] if scored else None,
            all_scores    = scored[:6] if scored else None,
            incident_id   = result.get("incident_id"),
            extra         = {"no_location": no_location, "units": units_added_ns},
        )
        return result

    top       = scored[0]
    top_score = top["signals"].signal_score
    top_inc   = top["incident"]
    top_sig   = top["signals"]
    classification = top_sig.classify()

    log.info("Layer 1 signal score",
             classification = classification,
             score          = round(top_score, 3),
             reasons        = top_sig.score_reasons[:5],
             candidate_id   = top_inc["incident_id"][:8],
             feed_id        = feed_id)

    # ── STRONG_MATCH path: Mode A sanity check ────────────────────────
    if classification == "STRONG_MATCH":
        confirmed = _run_mode_a_sanity_check(
            transcript       = transcript,
            geocoded_address = geocode_result.get("formatted", ""),
            incident_type    = incident_type,
            feed_id          = feed_id,
            candidate        = top_inc,
            signal_score     = top_score,
            score_reasons    = top_sig.score_reasons,
        )
        if confirmed:
            result = _update_existing(
                top_inc["incident_id"], units_added_ns, units_cleared_ns,
                summary, chunk_id, priority, feed_id,
            )
            _publish_correlation_trace(
                chunk_id=chunk_id, feed_id=feed_id, transcript=transcript,
                incident_type=incident_type, normalized=normalized_address,
                geocoded=geocode_result.get("formatted",""), units=units_added_ns,
                decision="UPDATE", reason=f"STRONG_MATCH Mode A CONFIRM (score={top_score:.3f})",
                top_score=top_score, top_candidate=top_inc, all_scores=scored[:6],
                incident_id=top_inc["incident_id"],
                extra={"classification": "STRONG_MATCH", "mode": "A"},
            )
            return result
        else:
            log.info("Mode A VETO — creating new incident",
                     vetoed_id=top_inc["incident_id"][:8])
            result = _create_new_incident(
                chunk_id, feed_id, county, transcript, normalized_address,
                geocode_result, unit_inferred_location, units_added_ns,
                units_cleared_ns, incident_type, priority, summary, lat, lng,
            )
            _publish_correlation_trace(
                chunk_id=chunk_id, feed_id=feed_id, transcript=transcript,
                incident_type=incident_type, normalized=normalized_address,
                geocoded=geocode_result.get("formatted",""), units=units_added_ns,
                decision="NEW", reason=f"STRONG_MATCH Mode A VETO (score={top_score:.3f})",
                top_score=top_score, top_candidate=top_inc, all_scores=scored[:6],
                incident_id=result.get("incident_id"),
                extra={"classification": "STRONG_MATCH", "mode": "A", "vetoed": top_inc["incident_id"][:8]},
            )
            return result

    # ── WEAK_MATCH / AMBIGUOUS path: Mode B full arbitration ──────────
    # Filter scored candidates to only include plausible ones
    # (Idea 6: pre-filter obviously unrelated incidents)
    mode_b_candidates = []
    for sc in scored:
        cand_addr = (sc["incident"].get("address_full") or
                     sc["incident"].get("address_raw") or "")
        if _addresses_clearly_different(
                geocode_result.get("formatted") or normalized_address,
                cand_addr):
            cand_type = sc["incident"].get("incident_type", "UNKNOWN")
            if frozenset({incident_type, cand_type}) in INCOMPATIBLE_TYPE_PAIRS:
                continue
        mode_b_candidates.append(sc)

    if not mode_b_candidates:
        log.debug("All candidates filtered by Idea 6 — creating new incident")
        return _create_new_incident(
            chunk_id, feed_id, county, transcript, normalized_address,
            geocode_result, unit_inferred_location, units_added_ns,
            units_cleared_ns, incident_type, priority, summary, lat, lng,
        )

    result = _run_mode_b_arbitration(
        transcript        = transcript,
        normalized_addr   = normalized_address,
        geocoded_address  = geocode_result.get("formatted", ""),
        geocode_result    = geocode_result,
        incident_type     = incident_type,
        units_added       = units_added_ns,
        feed_id           = feed_id,
        scored_candidates = mode_b_candidates,
        retry_attempt     = retry_attempt,
    )

    if result["decision"] == "MATCH" and result.get("incident_id"):
        if top_score >= THRESHOLD_WEAK_MATCH:
            upd = _update_existing(
                result["incident_id"], units_added_ns, units_cleared_ns,
                summary, chunk_id, priority, feed_id,
            )
            _publish_correlation_trace(
                chunk_id=chunk_id, feed_id=feed_id, transcript=transcript,
                incident_type=incident_type, normalized=normalized_address,
                geocoded=geocode_result.get("formatted",""), units=units_added_ns,
                decision="UPDATE",
                reason=f"Mode B MATCH ({classification}, score={top_score:.3f})",
                top_score=top_score, top_candidate=top_inc,
                all_scores=mode_b_candidates[:6],
                incident_id=result["incident_id"],
                extra={"classification": classification, "mode": "B",
                       "llm_decision": "MATCH"},
            )
            return upd
        else:
            log.info("Mode B MATCH but score too low — creating new incident",
                     score=top_score, threshold=THRESHOLD_WEAK_MATCH)
            new_inc = _create_new_incident(
                chunk_id, feed_id, county, transcript, normalized_address,
                geocode_result, unit_inferred_location, units_added_ns,
                units_cleared_ns, incident_type, priority, summary, lat, lng,
            )
            _publish_correlation_trace(
                chunk_id=chunk_id, feed_id=feed_id, transcript=transcript,
                incident_type=incident_type, normalized=normalized_address,
                geocoded=geocode_result.get("formatted",""), units=units_added_ns,
                decision="NEW",
                reason=f"Mode B MATCH rejected — score {top_score:.3f} < threshold {THRESHOLD_WEAK_MATCH}",
                top_score=top_score, top_candidate=top_inc,
                all_scores=mode_b_candidates[:6],
                incident_id=new_inc.get("incident_id"),
                extra={"classification": classification, "mode": "B",
                       "llm_decision": "MATCH", "score_too_low": True},
            )
            return new_inc

    if result["decision"] == "RETRY":
        log.info("Mode B RETRY — queuing chunk for next feed transmission",
                 feed_id=feed_id, chunk_id=chunk_id, attempt=retry_attempt)
        r = get_redis()
        push_retry(r, feed_id, {
            "chunk_id":               chunk_id,
            "feed_id":                feed_id,
            "county":                 county,
            "transcript":             transcript,
            "structured":             structured,
            "normalized_address":     normalized_address,
            "geocode_result":         geocode_result,
            "unit_inferred_location": unit_inferred_location,
        }, attempt=retry_attempt)
        _publish_correlation_trace(
            chunk_id=chunk_id, feed_id=feed_id, transcript=transcript,
            incident_type=incident_type, normalized=normalized_address,
            geocoded=geocode_result.get("formatted",""), units=units_added_ns,
            decision="RETRY",
            reason=f"Mode B RETRY (attempt {retry_attempt})",
            top_score=top_score, top_candidate=top_inc,
            all_scores=mode_b_candidates[:6], incident_id=None,
            extra={"classification": classification, "mode": "B",
                   "retry_attempt": retry_attempt},
        )
        return {"action": "UNASSOCIATED", "incident_id": None, "incident": None}

    # NEW
    new_inc = _create_new_incident(
        chunk_id, feed_id, county, transcript, normalized_address,
        geocode_result, unit_inferred_location, units_added_ns,
        units_cleared_ns, incident_type, priority, summary, lat, lng,
    )
    _publish_correlation_trace(
        chunk_id=chunk_id, feed_id=feed_id, transcript=transcript,
        incident_type=incident_type, normalized=normalized_address,
        geocoded=geocode_result.get("formatted",""), units=units_added_ns,
        decision="NEW",
        reason=f"Mode B NEW ({classification}, score={top_score:.3f})",
        top_score=top_score, top_candidate=top_inc,
        all_scores=mode_b_candidates[:6],
        incident_id=new_inc.get("incident_id"),
        extra={"classification": classification, "mode": "B", "llm_decision": "NEW"},
    )
    return new_inc


# ── Action helpers ────────────────────────────────────────────────────────

def _update_existing(
    incident_id:     str,
    units_added_ns:  list,
    units_cleared_ns: list,
    summary:         str,
    chunk_id:        str,
    priority:        str,
    feed_id:         str,
) -> dict:
    incident = update_incident(
        incident_id,
        units_added   = units_added_ns,
        units_cleared = units_cleared_ns,
        summary       = summary,
        chunk_id      = chunk_id,
        priority      = priority,
    )
    if units_added_ns:
        assign_many(units_added_ns, incident_id, feed_id=feed_id)
    return {"action": "UPDATE", "incident_id": incident_id, "incident": incident}


def _create_new_incident(
    chunk_id:               str,
    feed_id:                str,
    county:                 str,
    transcript:             str,
    normalized_address:     str,
    geocode_result:         dict,
    unit_inferred_location: dict | None,
    units_added_ns:         list,
    units_cleared_ns:       list,
    incident_type:          str,
    priority:               str,
    summary:                str,
    lat:                    float | None,
    lng:                    float | None,
) -> dict:
    final_lat = lat if lat is not None else (
        unit_inferred_location["lat"] if unit_inferred_location else None
    )
    final_lng = lng if lng is not None else (
        unit_inferred_location["lng"] if unit_inferred_location else None
    )
    address_full = geocode_result.get("formatted") or normalized_address
    city         = _extract_city(address_full)

    incident = create_incident(
        feed_id       = feed_id,
        county        = county,
        incident_type = incident_type,
        priority      = priority,
        address_raw   = normalized_address,
        address_full  = address_full,
        city          = city,
        lat           = final_lat,
        lng           = final_lng,
        units         = units_added_ns,
        summary       = summary,
        chunk_id      = chunk_id,
    )
    if units_added_ns:
        assign_many(units_added_ns, incident["incident_id"], feed_id=feed_id)

    return {"action": "NEW",
            "incident_id": incident["incident_id"],
            "incident": incident}


def _extract_city(address: str) -> str:
    if not address:
        return "Unknown"
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        return parts[-3]
    return parts[0] if parts else "Unknown"