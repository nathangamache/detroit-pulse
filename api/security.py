"""API authentication (pure ASGI, so it also covers the WebSocket handshake).

* PULSE_API_TOKEN set  -> every route except /v1/health, /health and static UI assets needs
  `Authorization: Bearer <PULSE_API_TOKEN>` (the gateway adds it when proxying /pulse/api).
  Unset -> open, for local development only (the server still binds 127.0.0.1).
* /admin/* has its own token, PULSE_ADMIN_TOKEN, sent as `X-Pulse-Admin-Token`. Unset -> every
  admin route answers 404, as if it didn't exist. The gateway never forwards /admin.
Comparisons are constant-time.
"""
from __future__ import annotations

import hmac
import json

from fastapi import Header, HTTPException

from core.settings import get_settings

OPEN_PATHS = {"/v1/health", "/health"}
STATIC_PREFIXES = ("/assets/", "/favicon.ico", "/index.html")


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def bearer_ok(header_value: str | None) -> bool:
    tok = get_settings().pulse_api_token
    if not tok or not tok.get_secret_value():
        return True
    if not header_value or not header_value.lower().startswith("bearer "):
        return False
    return _eq(header_value[7:].strip(), tok.get_secret_value())


class ApiTokenMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        root = scope.get("root_path", "") or ""
        path = scope.get("path", "")
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        exempt = (path in OPEN_PATHS or path.startswith("/admin") or path == "/"
                  or path.startswith(STATIC_PREFIXES))
        if exempt:
            return await self.app(scope, receive, send)
        auth = None
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth = v.decode("latin-1")
                break
        if bearer_ok(auth):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = json.dumps({"error": {"code": "unauthorized", "message": "missing or invalid bearer token"}}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})


def require_admin(x_pulse_admin_token: str | None = Header(default=None)) -> None:
    tok = get_settings().pulse_admin_token
    if not tok or not tok.get_secret_value():
        raise HTTPException(status_code=404, detail="Not Found")      # admin disabled: hide it
    if not x_pulse_admin_token or not _eq(x_pulse_admin_token, tok.get_secret_value()):
        raise HTTPException(status_code=403, detail="invalid admin token")
