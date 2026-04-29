import json
import os
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from api.broadcaster import get_debug_history

log = structlog.get_logger()
router = APIRouter(tags=["websocket"])


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info("WebSocket client connected", total=len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        log.info("WebSocket client disconnected", total=len(self.active))

    async def broadcast(self, event_type: str, data: Any):
        if not self.active:
            return
        message = json.dumps({"event": event_type, "data": data})
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, event_type: str, data: Any):
        message = json.dumps({"event": event_type, "data": data})
        await ws.send_text(message)


manager = ConnectionManager()


def _get_active_incidents() -> list[dict]:
    """
    Load all active incidents from Redis, falling back to DB.
    Called on WebSocket connect to give new clients the current state.
    """
    try:
        import redis as _redis
        import json as _json
        from dotenv import load_dotenv
        load_dotenv()

        r = _redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        ids = r.smembers("index:active_incidents")
        incidents = []
        for iid in ids:
            raw = r.get(f"incident:{iid}")
            if raw:
                try:
                    inc = _json.loads(raw)
                    if inc.get("status") == "ACTIVE":
                        incidents.append(inc)
                except Exception:
                    pass

        if incidents:
            return sorted(incidents,
                          key=lambda x: x.get("opened_at", ""),
                          reverse=True)
    except Exception as e:
        log.warning("Redis incident load failed, falling back to DB", error=str(e))

    # DB fallback
    try:
        from sqlalchemy import create_engine, select, desc
        from sqlalchemy.orm import sessionmaker
        from db.models import Incident

        engine = create_engine(
            os.getenv("DATABASE_URL",
                      "postgresql://detroit:detroit@localhost:5432/detroitpulse")
        )
        Session = sessionmaker(bind=engine)
        db = Session()
        try:
            rows = db.execute(
                select(Incident)
                .where(Incident.status == "ACTIVE")
                .order_by(desc(Incident.opened_at))
            ).scalars().all()
            return [i.to_dict() for i in rows]
        finally:
            db.close()
    except Exception as e:
        log.error("DB incident load failed", error=str(e))
        return []


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # 1. Send connection confirmation
        await manager.send_to(ws, "connected", {
            "message":      "Detroit Pulse live feed connected",
            "client_count": len(manager.active),
        })

        # 3. Replay debug history
        history = get_debug_history()
        if history:
            await manager.send_to(ws, "debug:history", {"events": history})
            log.info("Debug history sent to new client", count=len(history))

        # Keep alive
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await manager.send_to(ws, "pong", {})

    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        log.error("WebSocket error", error=str(e))
        manager.disconnect(ws)


def get_manager() -> ConnectionManager:
    return manager