import os
import structlog
import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

BROADCASTIFY_API_KEY = os.getenv("BROADCASTIFY_API_KEY", "")
FEEDS_CONFIG = "ingest/feeds.yaml"

router = APIRouter(prefix="/audio", tags=["audio"])

_feeds_cache: dict = {}


def load_feeds() -> dict:
    global _feeds_cache
    if not _feeds_cache:
        with open(FEEDS_CONFIG) as f:
            config = yaml.safe_load(f)
        _feeds_cache = {
            feed["id"]: feed
            for feed in config["feeds"]
        }
    return _feeds_cache


def get_stream_url(feed: dict) -> str | None:
    """
    Return the best available stream URL for a feed.
    Prefers test_stream_url during development before API access.
    Falls back to API-based URL when broadcastify_feed_id is configured.
    """
    # Direct test URL takes priority
    if feed.get("test_stream_url"):
        return feed["test_stream_url"]

    # API-based URL
    bid = feed.get("broadcastify_feed_id", "REPLACE_ME")
    if bid != "REPLACE_ME":
        return f"https://broadcastify.cdnstream1.com/{bid}"

    return None


@router.get("/feeds")
def list_audio_feeds():
    feeds = load_feeds()
    return [
        {
            "id":      fid,
            "name":    f["name"],
            "county":  f["county"],
            "area":    f["area"],
            "enabled": f.get("enabled", True),
        }
        for fid, f in feeds.items()
    ]


@router.get("/stream/{feed_id}")
async def proxy_stream(feed_id: str, request: Request):
    """
    Proxy the audio stream for a given feed to the dashboard.
    Handles both direct MP3 streams and HLS streams.
    LOCALHOST ONLY.
    """
    # Invalidate cache so yaml changes are picked up
    global _feeds_cache
    _feeds_cache = {}

    feeds = load_feeds()
    if feed_id not in feeds:
        raise HTTPException(status_code=404, detail=f"Feed {feed_id} not found")

    feed = feeds[feed_id]
    if not feed.get("enabled", True):
        raise HTTPException(status_code=403, detail=f"Feed {feed_id} is disabled")

    stream_url = get_stream_url(feed)
    if not stream_url:
        raise HTTPException(
            status_code=503,
            detail=f"Feed {feed_id} not yet configured"
        )

    log.info("Proxying audio stream", feed_id=feed_id, stream_url=stream_url)

    async def stream_generator():
        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 detroit-pulse/0.1",
                "Icy-MetaData": "0",
            }
        ) as client:
            async with client.stream("GET", stream_url) as response:
                if response.status_code != 200:
                    log.error(
                        "Stream returned non-200",
                        status=response.status_code,
                        feed_id=feed_id,
                    )
                    return
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    return StreamingResponse(
        stream_generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control":               "no-cache",
            "Access-Control-Allow-Origin":  "http://localhost",
            "Transfer-Encoding":           "chunked",
            "X-Content-Type-Options":      "nosniff",
            "X-Feed-Id":                   feed_id,
        },
    )


@router.get("/status/{feed_id}")
def feed_status(feed_id: str):
    global _feeds_cache
    _feeds_cache = {}
    feeds = load_feeds()
    if feed_id not in feeds:
        raise HTTPException(status_code=404, detail="Feed not found")

    feed = feeds[feed_id]
    stream_url = get_stream_url(feed)

    return {
        "feed_id":    feed_id,
        "name":       feed["name"],
        "enabled":    feed.get("enabled", True),
        "configured": stream_url is not None,
        "stream_url": stream_url,
        "county":     feed["county"],
    }
