from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import create_engine, desc, select, text, func
from sqlalchemy.orm import Session, sessionmaker

from db.models import Incident, TranscriptChunk
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://detroit:detroit@localhost:5432/detroitpulse"
)
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine)
router       = APIRouter(prefix="/incidents", tags=["incidents"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _chunk_count_subquery():
    """
    Fix #38 — reusable subquery that counts chunks per incident.
    Replaces the lazy-loaded len(self.chunks) call in to_dict().
    """
    return (
        select(
            TranscriptChunk.incident_id,
            func.count(TranscriptChunk.chunk_id).label("chunk_count"),
        )
        .group_by(TranscriptChunk.incident_id)
        .subquery()
    )


def _rows_to_dicts(rows) -> list[dict]:
    """Convert (Incident, chunk_count) rows to dicts with injected chunk_count."""
    result = []
    for incident, chunk_count in rows:
        d = incident.to_dict()
        d["chunk_count"] = int(chunk_count or 0)
        result.append(d)
    return result


@router.get("/active")
def list_active_incidents(
    county:  Optional[str] = Query(None),
    feed_id: Optional[str] = Query(None),
    db:      Session       = Depends(get_db),
):
    """
    Returns all currently active incidents with chunk counts.
    Single query — no N+1.
    """
    sq = _chunk_count_subquery()
    q  = (
        select(Incident, func.coalesce(sq.c.chunk_count, 0))
        .outerjoin(sq, Incident.incident_id == sq.c.incident_id)
        .where(Incident.status == "ACTIVE")
        .order_by(desc(Incident.opened_at))
    )
    if county:
        q = q.where(Incident.county == county.lower())
    if feed_id:
        q = q.where(Incident.feed_id == feed_id)

    return _rows_to_dicts(db.execute(q).all())


@router.get("/")
def list_incidents(
    status:        Optional[str] = Query(None),
    county:        Optional[str] = Query(None),
    incident_type: Optional[str] = Query(None),
    feed_id:       Optional[str] = Query(None),
    limit:         int           = Query(50, le=200),
    offset:        int           = Query(0),
    db:            Session       = Depends(get_db),
):
    sq = _chunk_count_subquery()
    q  = (
        select(Incident, func.coalesce(sq.c.chunk_count, 0))
        .outerjoin(sq, Incident.incident_id == sq.c.incident_id)
        .order_by(desc(Incident.opened_at))
    )
    if status:
        q = q.where(Incident.status == status.upper())
    if county:
        q = q.where(Incident.county == county.lower())
    if incident_type:
        q = q.where(Incident.incident_type == incident_type.upper())
    if feed_id:
        q = q.where(Incident.feed_id == feed_id)

    return _rows_to_dicts(db.execute(q.limit(limit).offset(offset)).all())


@router.get("/stats/summary")
def get_stats(db: Session = Depends(get_db)):
    active_count = db.execute(
        text("SELECT COUNT(*) FROM incidents WHERE status='ACTIVE'")
    ).scalar()
    today_count = db.execute(
        text("SELECT COUNT(*) FROM incidents "
             "WHERE opened_at >= NOW() - INTERVAL '24 hours'")
    ).scalar()
    by_type = db.execute(text("""
        SELECT incident_type, COUNT(*) as count FROM incidents
        WHERE opened_at >= NOW() - INTERVAL '24 hours'
        GROUP BY incident_type ORDER BY count DESC
    """)).fetchall()
    by_county = db.execute(text("""
        SELECT county, COUNT(*) as count FROM incidents
        WHERE status='ACTIVE' GROUP BY county ORDER BY count DESC
    """)).fetchall()
    return {
        "active_incidents": active_count,
        "incidents_24h":    today_count,
        "by_type_24h":      {r[0]: r[1] for r in by_type},
        "active_by_county": {r[0]: r[1] for r in by_county},
    }


@router.get("/{incident_id}")
def get_incident(incident_id: UUID, db: Session = Depends(get_db)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident.to_dict()


@router.get("/{incident_id}/chunks")
def get_incident_chunks(incident_id: UUID, db: Session = Depends(get_db)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    chunks = db.execute(
        select(TranscriptChunk)
        .where(TranscriptChunk.incident_id == incident_id)
        .order_by(TranscriptChunk.timestamp)
    ).scalars().all()
    return {
        "incident_id": str(incident_id),
        "chunk_count": len(chunks),
        "chunks":      [c.to_dict() for c in chunks],
    }


@router.get("/feed/{feed_id}/recent")
def get_recent_by_feed(
    feed_id: str,
    limit:   int     = Query(20, le=100),
    db:      Session = Depends(get_db),
):
    incidents = db.execute(
        select(Incident)
        .where(Incident.feed_id == feed_id)
        .order_by(desc(Incident.opened_at))
        .limit(limit)
    ).scalars().all()
    return [i.to_dict() for i in incidents]