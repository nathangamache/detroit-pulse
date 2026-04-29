import os
import structlog
import redis
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")
UNIT_KEY_PREFIX = "unit:"

# Default TTL — used for specific per-station/per-precinct feeds
UNIT_TTL_DEFAULT = int(os.getenv("UNIT_TTL_SECONDS", "28800"))  # 8 hours

# Per-feed TTL overrides.
# County-wide and multi-agency feeds get short TTLs because the same unit
# designator can appear across many simultaneous unrelated calls.
# Specific station/precinct feeds get the full default window.
FEED_UNIT_TTL = {
    # County-wide — units bounce between many calls, short window
    "wayneco_public_safety":   1800,   # 30 min
    "wayneco_downriver":       2700,   # 45 min — multi-city
    "washtenaw_metro":         2700,   # 45 min — multi-city
    "washtenaw_livingston":    2700,   # 45 min — multi-county
    # City-wide feeds — now in FEEDS_REQUIRE_JUDGE_ON_UNIT_MATCH so use
    # shorter TTLs to match. DPD/DFD feeds carry many simultaneous calls
    # across all precincts — unit IDs can't anchor incidents for hours.
    "wayneco_detroit_police_fire":     3600,   # 1 hour
    "wayneco_detroit_police_dispatch": 3600,   # 1 hour
    "wayneco_detroit_fire":            3600,   # 1 hour
    "wayneco_detroit_ems":             3600,   # 1 hour
    # Suburban city-specific feeds — these are focused enough for full window
    "wayneco_dearborn":                UNIT_TTL_DEFAULT,
    "wayneco_westland_gardencity":     UNIT_TTL_DEFAULT,
    "wayneco_grossepointe":            UNIT_TTL_DEFAULT,
    "wayneco_plymouthnorthville":      3600,   # 1 hour — moderate volume
    "wayneco_southwestern":            UNIT_TTL_DEFAULT,
    "wayneco_romulus":                 UNIT_TTL_DEFAULT,
    "wayneco_northville_plymouth_city": UNIT_TTL_DEFAULT,
    "wayneco_franklin_bingham":        UNIT_TTL_DEFAULT,
    "oaklandco_royaloak_fire":         UNIT_TTL_DEFAULT,
}

_MIN_UNIT_ID_LENGTH = 2

_UNIT_ID_BLACKLIST = {
    "UNKNOWN", "UNIT", "CAR", "NONE", "NULL", "N/A", "NA", "UNK",
    "COPY", "CLEAR", "RADIO", "DISPATCH", "CONTROL", "BASE",
    "CHANNEL", "FREQ", "FREQUENCY",
}

_ADDRESS_WORD_INDICATORS = {
    "avenue", "ave", "street", "st", "road", "rd", "boulevard", "blvd",
    "drive", "dr", "lane", "ln", "court", "ct", "place", "pl", "way",
    "highway", "hwy", "expressway", "freeway", "trail", "terrace",
    "dexter", "nashville", "gratiot", "mound", "woodward", "jefferson",
    "michigan", "warren", "mcnichols", "fenkell", "joy", "schoolcraft",
    "plymouth", "telegraph", "livernois", "wyoming", "greenfield",
    "evergreen", "southfield", "lahser", "inkster", "middlebelt",
    "merriman", "newburgh", "beech", "haggerty", "napier", "sheldon",
    "beck", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "thirteen", "fourteen", "fifteen",
}

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def _get_unit_ttl(feed_id: str | None) -> int:
    """Return the appropriate unit TTL for a given feed."""
    if not feed_id:
        return UNIT_TTL_DEFAULT
    # Exact match first
    if feed_id in FEED_UNIT_TTL:
        return FEED_UNIT_TTL[feed_id]
    # Prefix match
    for prefix, ttl in FEED_UNIT_TTL.items():
        if feed_id.startswith(prefix):
            return ttl
    return UNIT_TTL_DEFAULT


def _is_valid_unit_id(unit_id: str) -> bool:
    """
    Returns True if the unit_id looks like a real unit designator.
    Fix #15 — word-boundary matching for address indicators.
    """
    uid = unit_id.upper().strip()

    if ":" in uid:
        uid = uid.split(":", 1)[1]

    if len(uid) < _MIN_UNIT_ID_LENGTH:
        return False
    if uid.isdigit() and len(uid) < 3:
        return False
    if uid in _UNIT_ID_BLACKLIST:
        return False

    stripped = uid.replace("-", "").replace(" ", "")
    if stripped.isdigit() and len(stripped) <= 2:
        return False

    uid_lower  = uid.lower()
    uid_tokens = set(
        token for token in uid_lower.replace("-", " ").split()
        if token
    )
    if uid_tokens & _ADDRESS_WORD_INDICATORS:
        return False

    parts         = uid.replace(" ", "-").split("-")
    numeric_parts = [p for p in parts if p.isdigit()]
    if len(numeric_parts) >= 3 and len(parts) >= 3:
        return False

    has_digit = any(c.isdigit() for c in uid)
    known_letter_prefix = any(
        uid.startswith(p) for p in (
            "ENGINE", "LADDER", "MEDIC", "CHIEF", "RESCUE",
            "TRUCK", "SQUAD", "BATTALION", "DEPUTY",
            "E", "L", "M", "T", "R", "A", "B", "C", "D",
        )
    )
    if not has_digit and not known_letter_prefix:
        return False

    return True


def assign(unit_id: str, incident_id: str, feed_id: str | None = None) -> None:
    r   = get_redis()
    key = f"{UNIT_KEY_PREFIX}{unit_id.upper()}"
    ttl = _get_unit_ttl(feed_id)
    r.setex(key, ttl, incident_id)
    log.debug("Unit assigned",
              unit_id=unit_id,
              incident_id=str(incident_id)[:8],
              ttl_seconds=ttl)


def assign_many(
    unit_ids:    list[str],
    incident_id: str,
    feed_id:     str | None = None,
) -> None:
    if not unit_ids:
        return
    r   = get_redis()
    ttl = _get_unit_ttl(feed_id)
    pipe = r.pipeline()
    for uid in unit_ids:
        key = f"{UNIT_KEY_PREFIX}{uid.upper()}"
        pipe.setex(key, ttl, incident_id)
    pipe.execute()
    log.debug("Units assigned",
              count=len(unit_ids),
              incident_id=str(incident_id)[:8],
              ttl_seconds=ttl)


def lookup(unit_id: str) -> str | None:
    r   = get_redis()
    key = f"{UNIT_KEY_PREFIX}{unit_id.upper()}"
    return r.get(key)


def lookup_many(unit_ids: list[str]) -> dict[str, str]:
    if not unit_ids:
        return {}
    r    = get_redis()
    pipe = r.pipeline()
    for uid in unit_ids:
        pipe.get(f"{UNIT_KEY_PREFIX}{uid.upper()}")
    results = pipe.execute()
    return {
        uid: iid
        for uid, iid in zip(unit_ids, results)
        if iid is not None
    }


def release(unit_id: str) -> None:
    r   = get_redis()
    key = f"{UNIT_KEY_PREFIX}{unit_id.upper()}"
    r.delete(key)
    log.debug("Unit released", unit_id=unit_id)


def release_many(unit_ids: list[str]) -> None:
    if not unit_ids:
        return
    r    = get_redis()
    pipe = r.pipeline()
    for uid in unit_ids:
        pipe.delete(f"{UNIT_KEY_PREFIX}{uid.upper()}")
    pipe.execute()
    log.debug("Units released", count=len(unit_ids))


def active_units() -> dict[str, str]:
    r    = get_redis()
    keys = r.keys(f"{UNIT_KEY_PREFIX}*")
    if not keys:
        return {}
    pipe = r.pipeline()
    for k in keys:
        pipe.get(k)
    values     = pipe.execute()
    prefix_len = len(UNIT_KEY_PREFIX)
    return {
        k[prefix_len:]: v
        for k, v in zip(keys, values)
        if v is not None
    }