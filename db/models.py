import uuid
from datetime import datetime
from typing import Optional

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Index, Integer, String, Text, JSON,
    create_engine, text,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"

    incident_id  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    feed_id      = Column(String(100), nullable=False, index=True)
    county       = Column(String(50),  nullable=False, index=True)
    status       = Column(String(20),  nullable=False, default="ACTIVE", index=True)

    opened_at    = Column(DateTime(timezone=True), nullable=False, default=func.now())
    last_updated = Column(DateTime(timezone=True), nullable=False, default=func.now(),
                          onupdate=func.now())
    resolved_at  = Column(DateTime(timezone=True), nullable=True)

    incident_type = Column(String(50), nullable=False, default="UNKNOWN")
    priority      = Column(String(20), nullable=False, default="UNKNOWN")

    address_raw  = Column(Text, nullable=True)
    address_full = Column(Text, nullable=True)
    city         = Column(String(100), nullable=True)

    lat          = Column(Float, nullable=True)
    lng          = Column(Float, nullable=True)
    location     = Column(Geometry("POINT", srid=4326), nullable=True)

    units         = Column(ARRAY(String), nullable=False, default=list)
    units_cleared = Column(ARRAY(String), nullable=False, default=list)

    summary      = Column(Text, nullable=True)

    # Geographic enrichment
    precinct     = Column(String(10), nullable=True)
    battalion    = Column(String(10), nullable=True)
    nearest_stations = Column(JSON, nullable=True)

    # Relationships
    chunks = relationship(
        "TranscriptChunk",
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="TranscriptChunk.timestamp",
    )

    __table_args__ = (
        Index("ix_incidents_opened_at",     "opened_at"),
        Index("ix_incidents_incident_type", "incident_type"),
        Index("ix_incidents_status_county", "status", "county"),
    )

    def to_dict(self) -> dict:
        return {
            "incident_id":       str(self.incident_id),
            "feed_id":           self.feed_id,
            "county":            self.county,
            "status":            self.status,
            "opened_at":         self.opened_at.isoformat() if self.opened_at else None,
            "last_updated":      self.last_updated.isoformat() if self.last_updated else None,
            "resolved_at":       self.resolved_at.isoformat() if self.resolved_at else None,
            "incident_type":     self.incident_type,
            "priority":          self.priority,
            "address_raw":       self.address_raw,
            "address_full":      self.address_full,
            "city":              self.city,
            "lat":               self.lat,
            "lng":               self.lng,
            "units":             self.units or [],
            "units_cleared":     self.units_cleared or [],
            "summary":           self.summary,
            "precinct":          self.precinct,
            "battalion":         self.battalion,
            "nearest_stations":  self.nearest_stations,
            "chunk_count":       len(self.chunks) if self.chunks else 0,
        }


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"

    chunk_id    = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(
        UUID(as_uuid=True),
        ForeignKey("incidents.incident_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    feed_id     = Column(String(100), nullable=False, index=True)
    timestamp   = Column(DateTime(timezone=True), nullable=False, default=func.now())

    raw_transcript      = Column(Text, nullable=True)
    normalized_address  = Column(Text, nullable=True)

    correlation_action     = Column(String(20), nullable=True)
    correlation_method     = Column(String(30), nullable=True)
    correlation_confidence = Column(String(20), nullable=True)

    geocode_source      = Column(String(30), nullable=True)
    geocode_confidence  = Column(String(20), nullable=True)

    whisper_model       = Column(String(50), nullable=True)
    lora_version        = Column(String(50), nullable=True)
    processing_ms       = Column(Integer, nullable=True)

    # Relationship
    incident = relationship("Incident", back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_timestamp", "timestamp"),
        Index("ix_chunks_feed_id",   "feed_id"),
    )

    def to_dict(self) -> dict:
        return {
            "chunk_id":               str(self.chunk_id),
            "incident_id":            str(self.incident_id) if self.incident_id else None,
            "feed_id":                self.feed_id,
            "timestamp":              self.timestamp.isoformat() if self.timestamp else None,
            "raw_transcript":         self.raw_transcript,
            "normalized_address":     self.normalized_address,
            "correlation_action":     self.correlation_action,
            "correlation_method":     self.correlation_method,
            "correlation_confidence": self.correlation_confidence,
            "geocode_source":         self.geocode_source,
            "geocode_confidence":     self.geocode_confidence,
            "whisper_model":          self.whisper_model,
            "lora_version":           self.lora_version,
            "processing_ms":          self.processing_ms,
        }