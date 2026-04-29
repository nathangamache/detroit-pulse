import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ── Signal weights — hardcoded, tuned empirically ─────────────────────────
# These weights sum to 1.0 for the positive signals.
# Penalties are additive negatives applied on top.

SIGNAL_WEIGHTS = {
    # Positive signals
    "address_token_overlap":      0.30,  # Jaccard overlap on address tokens
    "geocode_proximity":          0.25,  # derived from geocode_distance_km
    "unit_id_match":              0.20,  # shared namespaced unit IDs
    "type_exact_match":           0.10,  # same incident type
    "is_burst_window":            0.08,  # chunk within 5min of incident open
    "same_feed":                  0.05,  # same feed_id as candidate

    # Additive bonuses (on top of base weights)
    "exact_street_match_bonus":   0.10,  # same primary street name
    "unit_same_feed_bonus":       0.05,  # unit match AND same feed

    # Penalties
    "type_incompatible_penalty":  -0.50, # hard penalty for incompatible types
    "high_volume_unit_penalty":   -0.10, # reduce unit signal on busy feeds
    "old_incident_penalty":       -0.15, # incident >30min since last update
    "far_geocode_penalty":        -0.20, # geocode >5km away
}

# ── Decision thresholds ────────────────────────────────────────────────────
THRESHOLD_STRONG_MATCH = 0.85   # LLM Mode A sanity check only
THRESHOLD_WEAK_MATCH   = 0.50   # LLM Mode B full arbitration
THRESHOLD_AMBIGUOUS    = 0.20   # LLM Mode B with conservative bias
# Below THRESHOLD_AMBIGUOUS → NO_MATCH → straight to NEW, skip judge

# ── Stop words for token overlap ──────────────────────────────────────────
_ADDR_STOP = {
    "at", "the", "a", "an", "for", "on", "in", "near", "and", "or",
    "of", "to", "from", "with", "no", "mi", "michigan", "detroit",
    "road", "rd", "avenue", "ave", "street", "st", "drive", "dr",
    "boulevard", "blvd", "highway", "hwy", "lane", "ln", "court", "ct",
    "usa", "place", "pl", "way", "north", "south", "east", "west",
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
}


@dataclass
class CorrelationSignals:
    """
    All deterministic signals between an incoming chunk and a candidate.
    Computed in microseconds with no external dependencies.
    """

    # ── Address signals ───────────────────────────────────────────────
    address_token_overlap: float = 0.0       # Jaccard 0.0-1.0
    geocode_distance_km: float | None = None  # None if either missing
    has_house_number: bool = False            # incoming addr starts with number
    exact_street_match: bool = False          # same primary street token

    # ── Unit signals ──────────────────────────────────────────────────
    unit_id_match_count: int = 0             # shared namespaced unit IDs
    unit_id_match: bool = False
    unit_same_feed: bool = False             # unit match + same feed

    # ── Temporal signals ──────────────────────────────────────────────
    incident_age_seconds: float = 0.0        # from last_updated
    time_since_last_chunk_s: float = 0.0
    is_burst_window: bool = False            # chunk within 5min of open

    # ── Type signals ──────────────────────────────────────────────────
    type_exact_match: bool = False
    type_compatible: bool = True             # not in INCOMPATIBLE_TYPE_PAIRS

    # ── Feed signals ──────────────────────────────────────────────────
    same_feed: bool = False
    feed_is_high_volume: bool = False

    # ── Composite ─────────────────────────────────────────────────────
    signal_score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)

    def classify(self) -> str:
        """STRONG_MATCH | WEAK_MATCH | AMBIGUOUS | NO_MATCH"""
        if self.signal_score >= THRESHOLD_STRONG_MATCH:
            return "STRONG_MATCH"
        if self.signal_score >= THRESHOLD_WEAK_MATCH:
            return "WEAK_MATCH"
        if self.signal_score >= THRESHOLD_AMBIGUOUS:
            return "AMBIGUOUS"
        return "NO_MATCH"


def compute_signals(
    incoming_transcript:  str,
    incoming_addr:        str,
    geocode_result:       dict,
    units_added_ns:       list[str],
    incident_type:        str,
    feed_id:              str,
    feed_is_high_volume:  bool,
    candidate_incident:   dict,
    incompatible_pairs:   set,
) -> CorrelationSignals:
    """
    Compute all deterministic signals between an incoming chunk
    and a candidate incident. Pure computation — no I/O.
    """
    sig = CorrelationSignals()
    now = datetime.now(timezone.utc)

    candidate_addr = (
        candidate_incident.get("address_full") or
        candidate_incident.get("address_raw") or ""
    )

    # ── Address ───────────────────────────────────────────────────────
    sig.address_token_overlap = _token_overlap(incoming_addr, candidate_addr)
    sig.has_house_number      = bool(
        incoming_addr and incoming_addr.strip() and
        incoming_addr.strip()[0].isdigit()
    )
    sig.exact_street_match = _shares_primary_street(incoming_addr,
                                                     candidate_addr)

    # ── Geocode distance ──────────────────────────────────────────────
    inc_lat  = geocode_result.get("lat")
    inc_lng  = geocode_result.get("lng")
    cand_lat = candidate_incident.get("lat")
    cand_lng = candidate_incident.get("lng")

    if inc_lat and inc_lng and cand_lat and cand_lng:
        try:
            sig.geocode_distance_km = _haversine(
                float(inc_lat), float(inc_lng),
                float(cand_lat), float(cand_lng),
            )
        except Exception:
            pass

    # ── Units ─────────────────────────────────────────────────────────
    candidate_units = set(candidate_incident.get("units", []))
    incoming_units  = set(units_added_ns)
    shared          = candidate_units & incoming_units

    sig.unit_id_match_count = len(shared)
    sig.unit_id_match       = len(shared) > 0
    sig.unit_same_feed      = (
        candidate_incident.get("feed_id") == feed_id and sig.unit_id_match
    )

    # ── Temporal ──────────────────────────────────────────────────────
    last_updated = (
        candidate_incident.get("last_updated") or
        candidate_incident.get("opened_at", "")
    )
    opened_at = candidate_incident.get("opened_at", "")

    if last_updated:
        try:
            dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            sig.incident_age_seconds = (now - dt).total_seconds()
        except Exception:
            pass

    if opened_at:
        try:
            dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            sig.is_burst_window = (now - dt).total_seconds() < 300
        except Exception:
            pass

    # ── Type ──────────────────────────────────────────────────────────
    candidate_type    = candidate_incident.get("incident_type", "UNKNOWN")
    sig.type_exact_match = (
        incident_type == candidate_type and incident_type != "UNKNOWN"
    )
    sig.type_compatible = (
        frozenset({incident_type, candidate_type}) not in incompatible_pairs
    )

    # ── Feed ──────────────────────────────────────────────────────────
    sig.same_feed          = candidate_incident.get("feed_id") == feed_id
    sig.feed_is_high_volume = feed_is_high_volume

    # ── Composite score ───────────────────────────────────────────────
    sig.signal_score, sig.score_reasons = _compute_score(sig)
    return sig


def _compute_score(sig: CorrelationSignals) -> tuple[float, list[str]]:
    """Compute the weighted composite signal score."""
    w       = SIGNAL_WEIGHTS
    score   = 0.0
    reasons = []

    # Hard reject for incompatible types
    if not sig.type_compatible:
        return 0.0, ["incompatible_types"]

    # Address token overlap
    if sig.address_token_overlap > 0:
        contrib = sig.address_token_overlap * w["address_token_overlap"]
        score  += contrib
        reasons.append(f"addr_overlap={sig.address_token_overlap:.2f}")

    # Exact street match bonus
    if sig.exact_street_match:
        score  += w["exact_street_match_bonus"]
        reasons.append("exact_street")

    # Geocode proximity
    if sig.geocode_distance_km is not None:
        d = sig.geocode_distance_km
        if d < 0.2:
            prox = 1.0
        elif d < 0.5:
            prox = 0.85
        elif d < 1.0:
            prox = 0.65
        elif d < 2.0:
            prox = 0.40
        elif d < 5.0:
            prox = 0.15
        else:
            prox = 0.0
            score += w["far_geocode_penalty"]
            reasons.append(f"far_geocode({d:.1f}km)")

        score  += prox * w["geocode_proximity"]
        reasons.append(f"dist={d:.2f}km")

    # Unit match
    if sig.unit_id_match:
        unit_w = w["unit_id_match"]
        if sig.feed_is_high_volume:
            unit_w += w["high_volume_unit_penalty"]
            reasons.append("unit(high_vol_penalty)")
        else:
            reasons.append(f"unit_match(x{sig.unit_id_match_count})")
        if sig.unit_same_feed:
            unit_w += w["unit_same_feed_bonus"]
            reasons.append("unit_same_feed")
        score += unit_w

    # Incident type
    if sig.type_exact_match:
        score  += w["type_exact_match"]
        reasons.append("type_match")

    # Burst window
    if sig.is_burst_window:
        score  += w["is_burst_window"]
        reasons.append("burst_window")

    # Old incident penalty
    if sig.incident_age_seconds > 1800:
        score  += w["old_incident_penalty"]
        reasons.append(f"old({int(sig.incident_age_seconds/60)}min)")

    # Same feed
    if sig.same_feed:
        score  += w["same_feed"]
        reasons.append("same_feed")

    return max(0.0, min(1.0, score)), reasons


def score_all_candidates(
    incoming_transcript:  str,
    incoming_addr:        str,
    geocode_result:       dict,
    units_added_ns:       list[str],
    incident_type:        str,
    feed_id:              str,
    feed_is_high_volume:  bool,
    active_incidents:     list[dict],
    incompatible_pairs:   set,
) -> list[dict]:
    """
    Score all candidate incidents with Layer 1 signals.
    Returns candidates sorted by signal_score descending.
    Each entry: {"incident": dict, "signals": CorrelationSignals}
    """
    scored = []
    for inc in active_incidents:
        sig = compute_signals(
            incoming_transcript = incoming_transcript,
            incoming_addr       = incoming_addr,
            geocode_result      = geocode_result,
            units_added_ns      = units_added_ns,
            incident_type       = incident_type,
            feed_id             = feed_id,
            feed_is_high_volume = feed_is_high_volume,
            candidate_incident  = inc,
            incompatible_pairs  = incompatible_pairs,
        )
        scored.append({"incident": inc, "signals": sig})

    return sorted(scored, key=lambda x: x["signals"].signal_score, reverse=True)


# ── Helpers ───────────────────────────────────────────────────────────────

def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity on meaningful address tokens."""
    def tok(s: str) -> set:
        return {
            w.strip(".,()").lower()
            for w in s.split()
            if len(w) > 2 and w.lower() not in _ADDR_STOP
        }
    t1, t2 = tok(a), tok(b)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


def _shares_primary_street(a: str, b: str) -> bool:
    """True if both addresses share the same longest meaningful token."""
    _suffix = {
        "road", "rd", "avenue", "ave", "street", "st", "drive", "dr",
        "boulevard", "blvd", "highway", "hwy", "lane", "ln", "court",
        "ct", "place", "pl", "way", "mile",
    }

    def primary(s: str) -> str:
        tokens = [
            w.strip(".,()").lower() for w in s.split()
            if not w.isdigit()
            and w.lower() not in _ADDR_STOP
            and w.lower() not in _suffix
            and len(w) > 3
        ]
        return max(tokens, key=len) if tokens else ""

    pa, pb = primary(a), primary(b)
    return bool(pa and pa == pb)


def _haversine(lat1: float, lng1: float,
                lat2: float, lng2: float) -> float:
    """Return distance in km between two lat/lng points."""
    R   = 6371.0
    d   = math.radians
    phi1, phi2 = d(lat1), d(lat2)
    a = (
        math.sin(d(lat2 - lat1) / 2) ** 2 +
        math.cos(phi1) * math.cos(phi2) *
        math.sin(d(lng2 - lng1) / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))