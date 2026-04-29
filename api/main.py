import asyncio
import json
import os
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import redis.asyncio as aioredis

from api.routes.incidents import router as incidents_router
from api.routes.ws        import router as ws_router, get_manager
from api.routes.audio     import router as audio_router
from api.routes.admin     import router as admin_router
from api.routes.external  import router as external_router
from api.auto_resolver    import start_auto_resolver

load_dotenv()
log = structlog.get_logger()

REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PUBSUB_CHANNEL = "detroit-pulse:events"

# Fix #39 — explicit origins required when allow_credentials=True.
# Wildcard "*" with credentials is rejected by browsers.
ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:80",
    "http://localhost:8000",
    "http://localhost:8080",
    "http://localhost:5173",
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "http://192.168.1.92",
    "http://192.168.1.92:80",
    "https://detroit-pulse.gamachecloud.com",
]

_extra = os.getenv("EXTRA_ALLOWED_ORIGINS", "")
if _extra:
    ALLOWED_ORIGINS += [o.strip() for o in _extra.split(",") if o.strip()]


async def redis_subscriber():
    """Subscribe to Redis pub/sub and forward events to WebSocket clients."""
    r      = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe(PUBSUB_CHANNEL)
    manager = get_manager()
    log.info("Redis subscriber started", channel=PUBSUB_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            payload = json.loads(message["data"])
            event   = payload.get("event")
            data    = payload.get("data", {})
            await manager.broadcast(event, data)
        except Exception as e:
            log.error("Redis subscriber error", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(redis_subscriber())
    start_auto_resolver()
    # Start hourly pipeline metrics collector (writes to pipeline_metrics table)
    try:
        from lora.metrics_collector import start_metrics_collector
        start_metrics_collector()
    except Exception as e:
        log.warning("Metrics collector failed to start", error=str(e))
    log.info("Detroit Pulse API starting")
    yield
    task.cancel()
    log.info("Detroit Pulse API shutting down")


app = FastAPI(
    title       = "Detroit Pulse",
    description = "Real-time metro Detroit public safety intelligence dashboard",
    version     = "0.1.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── API routes (registered before static files so they take priority) ──────
app.include_router(incidents_router)
app.include_router(ws_router)
app.include_router(audio_router)
app.include_router(admin_router)
app.include_router(external_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "detroit-pulse"}


# ── Static file serving ────────────────────────────────────────────────────

FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")

if os.path.exists(FRONTEND_DIST):

    # Explicit favicon route — must be registered before the SPA catch-all.
    # Without this, StaticFiles(html=True) returns index.html for any path
    # it can't resolve as a file, including /favicon.ico.
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        path = os.path.join(FRONTEND_DIST, "favicon.ico")
        if os.path.exists(path):
            return FileResponse(
                path,
                media_type="image/x-icon",
                headers={"Cache-Control": "public, max-age=604800, immutable"},
            )
        return FileResponse(
            os.path.join(FRONTEND_DIST, "index.html"),
            media_type="text/html",
        )

    # Serve Vite's /assets/ directory directly (JS, CSS bundles).
    # This mount is more specific than "/" so it takes priority for asset URLs.
    _assets_dir = os.path.join(FRONTEND_DIST, "assets")
    if os.path.exists(_assets_dir):
        app.mount(
            "/assets",
            StaticFiles(directory=_assets_dir),
            name="assets",
        )

    # SPA catch-all — serves index.html for all non-API, non-asset paths.
    # html=True makes StaticFiles return index.html for unknown paths,
    # enabling client-side routing to work on hard reload.
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIST, html=True),
        name="frontend",
    )

    log.info("Serving frontend from dist", path=FRONTEND_DIST)