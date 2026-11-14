"""Detroit Pulse API (FastAPI). Run with `uv run python -m api` (binds 127.0.0.1:7710).

Routes
  /v1/*            the gateway contract (docs/pulse-api.md): health, incidents/near,
                   incidents/{id}, transcripts/search, rail/activity, feeds/status
  /incidents/*, /channels, /transmissions/recent, /rail/recent, /external/*, /ws
                   used by the bundled UI (served under /pulse; API via /pulse/api)
  /admin/*         token-protected maintenance; 404 unless PULSE_ADMIN_TOKEN is set
There is no /audio/stream any more: the unauthenticated Broadcastify relay is removed.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes.admin import router as admin_router
from api.routes.ui import router as ui_router
from api.routes.v1 import router as v1_router
from api.routes.ws import get_manager
from api.routes.ws import router as ws_router
from api.security import ApiTokenMiddleware
from core.bus import EVENTS_CHANNEL
from core.logging import setup_logging
from core.settings import get_settings

log = structlog.get_logger()
_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 422: "bad_request",
          503: "unavailable"}


async def redis_subscriber() -> None:
    """Fan out pipeline events to WebSocket clients; reconnects if Redis restarts."""
    while True:
        try:
            r = aioredis.from_url(get_settings().redis_dsn, decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.subscribe(EVENTS_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    await get_manager().broadcast(payload.get("event"), payload.get("data", {}))
                except (ValueError, TypeError):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("redis subscriber error; retrying", error=str(e)[:200])
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(redis_subscriber())
    yield
    task.cancel()


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app() -> FastAPI:
    s = get_settings()
    setup_logging()
    app = FastAPI(
        title="Detroit Pulse", version="0.2.0", lifespan=lifespan, root_path=s.pulse_root_path,
        docs_url="/docs" if s.pulse_docs_enabled else None, redoc_url=None,
        openapi_url="/openapi.json" if s.pulse_docs_enabled else None,
    )
    if s.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=False,
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])
    app.add_middleware(ApiTokenMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        d = exc.detail
        if isinstance(d, dict) and "code" in d:
            return _error(exc.status_code, d["code"], d.get("message", ""))
        return _error(exc.status_code, _CODES.get(exc.status_code, "internal" if exc.status_code >= 500 else "bad_request"),
                      str(d))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x not in ("query", "path", "body"))
        return _error(400, "bad_request", f"{where}: {first.get('msg', 'invalid request')}".strip(": "))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        log.error("unhandled API error", path=request.url.path, error=str(exc)[:300])
        return _error(500, "internal", "internal error")

    app.include_router(v1_router)
    app.include_router(ui_router)
    app.include_router(ws_router)
    app.include_router(admin_router)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "service": "detroit-pulse"}

    # The UI (frontend/dist, built with base /pulse/) is served by the gateway (PULSE_STATIC_DIR);
    # for local work use `npm run dev`, which proxies /pulse/api to this server.
    return app


app = create_app()
