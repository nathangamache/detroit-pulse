"""WebSocket /ws: relays Redis `pulse:events` (incident:new|update|resolve, rail:*, pipeline:debug)."""
from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.bus import DEBUG_HISTORY_KEY, get_redis

log = structlog.get_logger()
router = APIRouter(tags=["websocket"])


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, event_type: str, data: Any):
        if not self.active:
            return
        message = json.dumps({"event": event_type, "data": data}, default=str)
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:  # noqa: BLE001
                self.disconnect(ws)


manager = ConnectionManager()


def get_manager() -> ConnectionManager:
    return manager


def _debug_history(limit: int = 200) -> list[dict]:
    try:
        raw = get_redis().lrange(DEBUG_HISTORY_KEY, 0, limit - 1)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in reversed(raw):
        try:
            out.append(json.loads(item))
        except ValueError:
            continue
    return out


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps({"event": "connected", "data": {"client_count": len(manager.active)}}))
        hist = _debug_history()
        if hist:
            await ws.send_text(json.dumps({"event": "debug:history", "data": {"events": hist}}, default=str))
        while True:
            msg = await ws.receive_text()
            try:
                if json.loads(msg).get("type") == "ping":
                    await ws.send_text(json.dumps({"event": "pong", "data": {}}))
            except ValueError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:  # noqa: BLE001
        log.info("websocket closed", error=str(e)[:100])
        manager.disconnect(ws)
