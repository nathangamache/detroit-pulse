import json
import time
import structlog
import redis

log = structlog.get_logger()

RETRY_KEY_PREFIX    = "queue:retry:"
RETRY_TTL           = 600   # 10 minutes — older context is useless
MAX_RETRY_DEPTH     = 3     # max re-evaluation attempts before UNASSOCIATED
MAX_QUEUE_PER_FEED  = 10    # max pending items per feed


def push_retry(
    r:       redis.Redis,
    feed_id: str,
    chunk:   dict,
    attempt: int = 0,
) -> None:
    """
    Push a chunk onto the retry queue for its feed.
    If the queue is full, the oldest item is dropped to make room.
    """
    key  = f"{RETRY_KEY_PREFIX}{feed_id}"
    item = {
        "chunk":     chunk,
        "attempt":   attempt,
        "queued_at": time.time(),
        "feed_id":   feed_id,
    }

    # Cap queue size — drop oldest if at limit
    current_depth = r.llen(key)
    if current_depth >= MAX_QUEUE_PER_FEED:
        dropped = r.rpop(key)
        if dropped:
            try:
                d = json.loads(dropped)
                log.debug("Retry queue full — dropped oldest item",
                          feed_id    = feed_id,
                          chunk_id   = d.get("chunk", {}).get("chunk_id", "")[:8],
                          queue_size = current_depth)
            except Exception:
                pass

    r.lpush(key, json.dumps(item))
    r.expire(key, RETRY_TTL)

    log.info("Chunk pushed to retry queue",
             feed_id   = feed_id,
             chunk_id  = chunk.get("chunk_id", "")[:8],
             attempt   = attempt,
             queue_depth = r.llen(key))


def pop_retry_queue(r: redis.Redis, feed_id: str) -> list[dict]:
    """
    Pop and return all non-expired, non-exhausted retry items for a feed.
    Items are returned oldest-first (they were pushed LIFO, so we drain
    the whole list and sort by queued_at).

    Expired items (age > RETRY_TTL) are discarded.
    Exhausted items (attempt >= MAX_RETRY_DEPTH) are discarded.

    Call this at the start of processing each new chunk from the feed,
    before the new chunk itself is processed.
    """
    key   = f"{RETRY_KEY_PREFIX}{feed_id}"
    now   = time.time()
    items = []

    # Drain the entire queue
    while True:
        raw = r.rpop(key)
        if raw is None:
            break
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue

        age     = now - item.get("queued_at", 0)
        attempt = item.get("attempt", 0)

        if age > RETRY_TTL:
            log.debug("Retry item expired — discarding",
                      feed_id  = feed_id,
                      chunk_id = item.get("chunk", {}).get("chunk_id", "")[:8],
                      age_s    = round(age, 1))
            continue

        if attempt >= MAX_RETRY_DEPTH:
            log.info("Retry item exhausted max attempts — routing to UNASSOCIATED",
                     feed_id  = feed_id,
                     chunk_id = item.get("chunk", {}).get("chunk_id", "")[:8],
                     attempts = attempt)
            continue

        items.append(item)

    # Sort oldest first so earlier chunks get correlated before later ones
    items.sort(key=lambda x: x.get("queued_at", 0))

    if items:
        log.info("Retry queue drained",
                 feed_id = feed_id,
                 count   = len(items))

    return items


def queue_depth(r: redis.Redis, feed_id: str) -> int:
    """Return the number of items currently in the retry queue for a feed."""
    return r.llen(f"{RETRY_KEY_PREFIX}{feed_id}")


def all_queue_depths(r: redis.Redis) -> dict[str, int]:
    """
    Return depth of all active retry queues.
    Useful for monitoring — large queues indicate a feed with many
    ambiguous chunks, possibly a sign of heavy simultaneous call volume.
    """
    keys   = r.keys(f"{RETRY_KEY_PREFIX}*")
    prefix = len(RETRY_KEY_PREFIX)
    result = {}
    if keys:
        pipe = r.pipeline()
        for k in keys:
            pipe.llen(k)
        lengths = pipe.execute()
        for k, length in zip(keys, lengths):
            feed_id = k[prefix:]
            if length > 0:
                result[feed_id] = length
    return result


def clear_feed_queue(r: redis.Redis, feed_id: str) -> int:
    """
    Clear all pending retry items for a feed.
    Used when a feed restarts or when the pipeline is reset.
    Returns number of items cleared.
    """
    key   = f"{RETRY_KEY_PREFIX}{feed_id}"
    count = r.llen(key)
    if count > 0:
        r.delete(key)
        log.info("Retry queue cleared", feed_id=feed_id, items_cleared=count)
    return count