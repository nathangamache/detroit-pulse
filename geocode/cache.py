import hashlib
import json
import os
import structlog
import redis
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL   = 60 * 60 * 24 * 30  # 30 days in seconds
KEY_PREFIX  = "geocache:"

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def _make_key(address: str) -> str:
    """
    Normalize and hash the address string into a stable cache key.
    Lowercased + stripped so minor whitespace differences don't miss cache.
    """
    normalized = address.lower().strip()
    digest = hashlib.md5(normalized.encode()).hexdigest()
    return f"{KEY_PREFIX}{digest}"


def get(address: str) -> dict | None:
    """
    Return cached geocoding result for address, or None if not cached.
    """
    r = get_redis()
    key = _make_key(address)
    raw = r.get(key)
    if raw is None:
        return None
    try:
        result = json.loads(raw)
        log.debug("Geocache hit", address=address[:60])
        return result
    except json.JSONDecodeError:
        return None


def set(address: str, result: dict) -> None:
    """
    Cache a geocoding result with 30-day TTL.
    """
    r = get_redis()
    key = _make_key(address)
    r.setex(key, CACHE_TTL, json.dumps(result))
    log.debug("Geocache set", address=address[:60])


def stats() -> dict:
    """
    Return cache stats — key count and memory usage.
    """
    r = get_redis()
    keys = r.keys(f"{KEY_PREFIX}*")
    return {
        "cached_addresses": len(keys),
        "ttl_days": CACHE_TTL // 86400,
    }
