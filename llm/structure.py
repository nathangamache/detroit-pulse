import json
import math
import os
import structlog
from ollama import Client
from dotenv import load_dotenv
from llm.prompts import (
    STRUCTURE_SYSTEM, STRUCTURE_USER,
    format_active_incidents, format_feed_context,
)

load_dotenv()
log = structlog.get_logger()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
QWEN_MODEL      = os.getenv("QWEN_MODEL", "qwen2.5:7b-instruct")

_client: Client | None = None

# Fix #18 — shared engine singleton instead of creating per call
_shared_engine = None

def _get_shared_engine():
    global _shared_engine
    if _shared_engine is None:
        from sqlalchemy import create_engine
        _shared_engine = create_engine(
            os.getenv("DATABASE_URL"),
            pool_size=3,
            max_overflow=2,
            pool_pre_ping=True,
        )
    return _shared_engine


FALLBACK_STRUCTURE = {
    "has_incident":       False,
    "correlation_action": "UNASSOCIATED",
    "incident_id":        None,
    "incident_type":      "UNKNOWN",
    "priority":           "UNKNOWN",
    "units_added":        [],
    "units_cleared":      [],
    "summary_update":     "Could not parse transcript.",
}


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(host=OLLAMA_BASE_URL)
    return _client


def structure_transcript(
    transcript:         str,
    normalized_address: str,
    geocoded_address:   str,
    lat:                float | None,
    lng:                float | None,
    county:             str,
    feed_id:            str,
    active_incidents:   list[dict],
) -> dict:
    client       = get_client()
    feed_context = format_feed_context(feed_id)
    system       = STRUCTURE_SYSTEM.format(feed_context=feed_context)

    incidents_context = format_active_incidents(active_incidents)
    lat_str = str(round(lat, 6)) if lat is not None else "unknown"
    lng_str = str(round(lng, 6)) if lng is not None else "unknown"

    user_content = STRUCTURE_USER.format(
        transcript         = transcript,
        normalized_address = normalized_address,
        geocoded_address   = geocoded_address,
        lat                = lat_str,
        lng                = lng_str,
        county             = county,
        feed_id            = feed_id,
        active_incidents   = incidents_context,
    )

    try:
        response = client.chat(
            model    = QWEN_MODEL,
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_content},
            ],
            options  = {
                "temperature": 0.1,
                "num_predict": 800,  # Fix #13 — was 512, raised to prevent truncation
            },
        )

        raw = response["message"]["content"].strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        # Fix #12 — catch JSON errors with full context logging
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as je:
            log.error(
                "LLM structuring returned invalid JSON — chunk will be UNASSOCIATED",
                error            = str(je),
                raw_preview      = raw[:200],
                transcript_preview = transcript[:100],
                feed_id          = feed_id,
            )
            return {**FALLBACK_STRUCTURE, 'units_added': [], 'units_cleared': []}

        required = [
            "has_incident", "correlation_action", "incident_type",
            "priority", "units_added", "units_cleared", "summary_update",
        ]
        for key in required:
            if key not in result:
                result[key] = FALLBACK_STRUCTURE[key]

        if "incident_id" not in result:
            result["incident_id"] = None

        log.info("Transcript structured",
                 feed_id            = feed_id,
                 has_incident       = result["has_incident"],
                 correlation_action = result["correlation_action"],
                 incident_type      = result.get("incident_type"))
        return result

    except json.JSONDecodeError as e:
        # Fix #12 — outer catch for any remaining JSON issues
        log.error("LLM returned invalid JSON (outer)",
                  error=str(e), feed_id=feed_id,
                  transcript_preview=transcript[:100])
        return {**FALLBACK_STRUCTURE, 'units_added': [], 'units_cleared': []}
    except Exception as e:
        log.error("Structuring failed", error=str(e), feed_id=feed_id)
        return {**FALLBACK_STRUCTURE, 'units_added': [], 'units_cleared': []}


def generate_incident_summary(
    incident: dict,
    chunks:   list[dict],
    feed_id:  str = "",
) -> str:
    """
    Generate a concise news-brief-style summary of an incident from all
    its transcript chunks. Regenerated on every UPDATE so it always
    reflects the full picture.
    """
    client       = get_client()
    feed_context = format_feed_context(feed_id)

    if not chunks:
        return incident.get("summary", "")

    sorted_chunks = sorted(chunks, key=lambda x: x.get("timestamp", ""))
    chunks_text   = "\n".join([
        f"[{c.get('correlation_action', 'CHUNK')} at "
        f"{str(c.get('timestamp', ''))[:19]}] "
        f"{c.get('raw_transcript', '').strip()}"
        for c in sorted_chunks
        if c.get("raw_transcript")
    ])

    system = f"""You are a Detroit metro area public safety incident reporter.
Given a series of dispatch radio transcripts for a single incident,
write a clear, informative summary that tells someone exactly what happened
and what the current situation is — like a brief from a newsroom editor.

Rules:
- 2-4 sentences maximum
- Lead with what the incident actually is and where it happened
- Include the most important developments from the transcript history
- End with the current status if it can be determined
- Use plain English — translate radio codes
- Unit numbers only worth mentioning if they add context (e.g. multiple agencies)
- Be specific: "working structure fire with extension to exposure" is better
  than "fire reported"
- Do not use template phrases like "It was originally reported that"
- Do not say "units are responding" as the only content
- If injuries are mentioned, include that
- If the incident escalated or de-escalated, say so

Bad: "Engine 30 is responding to a fire on Plymouth Road."
Good: "A working structure fire was reported at a residential address on
  Plymouth Road. Multiple units responded with reports of flames showing.
  A second alarm was requested and crews are actively working the scene."

Feed context: {feed_context}"""

    user = f"""Incident type: {incident.get('incident_type', 'UNKNOWN')}
Location: {incident.get('address_full') or incident.get('address_raw') or 'Unknown'}
Priority: {incident.get('priority', 'UNKNOWN')}

Transcript chunks (oldest to newest):
{chunks_text}"""

    try:
        response = client.chat(
            model    = QWEN_MODEL,
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            options  = {"temperature": 0.3, "num_predict": 250},
        )

        result = response["message"]["content"].strip().strip('"\'')
        if result.startswith("Summary:"):
            result = result[8:].strip()

        log.info("Incident summary generated",
                 incident_id    = str(incident.get("incident_id", ""))[:8],
                 summary_length = len(result))
        return result

    except Exception as e:
        log.error("Summary generation failed", error=str(e))
        return incident.get("summary", "")


# ── Correlation judge helpers ─────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float,
                   lat2: float, lng2: float) -> float:
    R    = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a    = (math.sin(dlat / 2) ** 2 +
            math.cos(math.radians(lat1)) *
            math.cos(math.radians(lat2)) *
            math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _incident_age_minutes(opened_at: str) -> float | None:
    try:
        from datetime import datetime, timezone
        dt    = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 60
    except Exception:
        return None


def _build_distance_str(
    lat: float | None, lng: float | None,
    inc_lat, inc_lng,
    location_is_inferred: bool = False,
) -> tuple[str, float | None]:
    """
    Returns (display_string, dist_km_or_None).
    Fix #17 — returns the numeric distance separately so callers
    don't need to re-parse the string.
    """
    if lat and lng and inc_lat and inc_lng:
        try:
            dist_km = _haversine_km(lat, lng, float(inc_lat), float(inc_lng))
            note    = " (location is inferred — approximate)" \
                      if location_is_inferred else ""
            return f"{dist_km:.2f} km apart{note}", dist_km
        except Exception:
            pass
    return "one or both locations unknown", None


# ── LLM Correlation Judge ─────────────────────────────────────────────────

def llm_correlation_judge(
    transcript:           str,
    normalized_address:   str,
    geocoded_address:     str,
    lat:                  float | None,
    lng:                  float | None,
    incident_type:        str,
    units:                list,
    feed_id:              str,
    timestamp:            str,
    active_incidents:     list,
    recent_chunks:        list[dict] | None = None,
    location_is_inferred: bool = False,
) -> dict:
    """
    Judge whether a new chunk matches an existing active incident.

    Fix #16 — evaluates ALL candidates and returns the highest-confidence
    match rather than stopping at the first YES.

    Fix #17 — distance parsed as a separate numeric value, not re-parsed
    from a string, eliminating the ValueError on unknown distances.
    """
    if not active_incidents:
        return {"match": False, "incident_id": None,
                "confidence": "HIGH", "reason": "No active incidents to compare"}

    # Idea 2 — reduced candidate window from 6 to 3.
    # On a busy feed with simultaneous calls, a chunk is far more likely
    # to belong to a very recent incident than one opened many minutes ago.
    candidates = sorted(
        active_incidents,
        key=lambda x: x.get("opened_at", ""),
        reverse=True,
    )[:3]

    # Fix #16 — collect all matches, pick best at end
    all_matches: list[dict] = []

    for inc in candidates:
        inc_id     = inc["incident_id"]
        inc_type   = inc.get("incident_type", "UNKNOWN")
        inc_addr   = inc.get("address_full") or inc.get("address_raw") or "Unknown"
        inc_lat    = inc.get("lat")
        inc_lng    = inc.get("lng")
        inc_opened = inc.get("opened_at", "")
        inc_units  = inc.get("units", [])

        # Fix #17 — get numeric distance separately
        dist_str, dist_km = _build_distance_str(
            lat, lng, inc_lat, inc_lng,
            location_is_inferred=location_is_inferred,
        )

        age_mins = _incident_age_minutes(inc_opened)
        age_str  = f"{int(age_mins)} minutes ago" \
                   if age_mins is not None else "unknown age"

        if recent_chunks:
            last3 = sorted(recent_chunks,
                           key=lambda x: x.get("timestamp", ""),
                           reverse=True)[:3]
            recent_text = "\n".join(
                f"  [{c.get('timestamp', '')[:19]}] "
                f"{c.get('raw_transcript', '')[:120]}"
                for c in last3
            )
        else:
            recent_text = "  (no prior chunks available)"

        # Idea 2 — conservative judge prompt. Requires explicit positive
        # evidence before returning YES. Default to NO when uncertain.
        # Multiple simultaneous calls on the same radio feed are common —
        # a new transmission is more likely a DIFFERENT call than the same one
        # unless there is a clear shared signal (same location, same unit,
        # clear narrative continuation).
        system = (
            "You are a Detroit metro area public safety dispatch correlation engine.\n"
            "A single radio channel carries MULTIPLE SIMULTANEOUS CALLS.\n"
            "Your job: determine if a new radio transmission belongs to an "
            "EXISTING incident or is a DIFFERENT call entirely.\n\n"
            "Answer YES only if you have EXPLICIT POSITIVE EVIDENCE:\n"
            "  - Same specific address or location\n"
            "  - Same unit ID mentioned\n"
            "  - Clear narrative continuation (e.g. update on same patient, "
            "same suspect, same fire)\n\n"
            "Answer NO if:\n"
            "  - Different address or location\n"
            "  - Different incident type (medical vs shooting vs fire etc)\n"
            "  - No shared units and no shared location\n"
            "  - You are uncertain — when in doubt, answer NO\n\n"
            "Answer YES or NO only. No explanation."
        )

        user = (
            f"EXISTING INCIDENT:\n"
            f"Type: {inc_type}\n"
            f"Address: {inc_addr}\n"
            f"Opened: {age_str}\n"
            f"Units on scene: {', '.join(inc_units) or 'none'}\n"
            f"Recent transmissions:\n{recent_text}\n\n"
            f"NEW TRANSMISSION:\n"
            f"Type: {incident_type}\n"
            f"Address: {geocoded_address or normalized_address}\n"
            f"Units mentioned: {', '.join(units) if units else 'none'}\n"
            f"Transcript: {transcript[:300]}\n"
            f"Distance from incident: {dist_str}\n\n"
            f"Does this new transmission belong to the EXISTING incident above, "
            f"or is it a DIFFERENT call? Answer YES (same incident) or NO (different call)."
        )

        try:
            response = get_client().chat(
                model    = QWEN_MODEL,
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                options  = {"temperature": 0.0, "num_predict": 10},
            )

            answer = response["message"]["content"].strip().upper()
            is_yes = answer.startswith("YES")

            if is_yes:
                # Fix #17 — use numeric dist_km directly, no string parsing
                very_close = (dist_km is not None and dist_km < 0.5
                              and not location_is_inferred)

                if very_close:
                    confidence = "HIGH"
                elif inc_type == incident_type and dist_km is not None and dist_km < 5.0:
                    confidence = "HIGH"
                elif inc_type == incident_type and dist_km is not None and dist_km < 15.0:
                    confidence = "MEDIUM"
                elif inc_type == incident_type and dist_km is None:
                    # Same type, unknown distance — MEDIUM
                    confidence = "MEDIUM"
                elif location_is_inferred:
                    confidence = "LOW"
                else:
                    confidence = "LOW"

                if confidence == "LOW":
                    log.info("LLM judge YES but LOW confidence — skipping",
                             inc_id     = inc_id[:8],
                             inc_type   = inc_type,
                             chunk_type = incident_type,
                             dist       = dist_str)
                    # Fix #16 — continue evaluating rather than break
                    continue

                all_matches.append({
                    "match":       True,
                    "incident_id": inc_id,
                    "confidence":  confidence,
                    "reason":      f"{inc_type} at {inc_addr[:50]}, {age_str}, {dist_str}",
                    "dist_km":     dist_km if dist_km is not None else float('inf'),
                })

            log.debug("LLM judge pairwise: NO",
                      inc_id=inc_id[:8], inc_type=inc_type, dist=dist_str)

        except Exception as e:
            log.warning("LLM judge pairwise failed", error=str(e))
            continue

    # Fix #16 — pick best match: HIGH > MEDIUM, then closest distance
    if all_matches:
        def match_sort_key(m):
            conf_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            return (conf_rank.get(m["confidence"], 2), m["dist_km"])

        best = sorted(all_matches, key=match_sort_key)[0]
        log.info("LLM correlation judge: MATCH",
                 incident_id = best["incident_id"][:8],
                 confidence  = best["confidence"],
                 reason      = best["reason"],
                 candidates_evaluated = len(candidates),
                 yes_matches = len(all_matches))
        return {
            "match":       True,
            "incident_id": best["incident_id"],
            "confidence":  best["confidence"],
            "reason":      best["reason"],
        }

    log.debug("LLM correlation judge: NO MATCH",
              candidates_evaluated=len(candidates))
    return {
        "match":       False,
        "incident_id": None,
        "confidence":  "HIGH",
        "reason":      "No matching incident found",
    }


def fetch_recent_chunks_for_judge(incident_id: str, n: int = 3) -> list:
    """
    Fetch last N transcript chunks for an incident.
    Fix #18 — uses shared engine singleton instead of creating per call.
    """
    try:
        from sqlalchemy import text as _text
        engine = _get_shared_engine()
        with engine.connect() as conn:
            rows = conn.execute(_text("""
                SELECT raw_transcript, timestamp::text
                FROM transcript_chunks
                WHERE incident_id::text = :iid
                ORDER BY timestamp DESC LIMIT :n
            """), {"iid": incident_id, "n": n}).fetchall()
            return [{"raw_transcript": r.raw_transcript,
                     "timestamp": r.timestamp} for r in rows]
    except Exception as e:
        log.warning("fetch_recent_chunks failed", error=str(e))
        return []