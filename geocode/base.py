"""Pluggable geocoder interface ($0 providers only).

A query is the STRUCTURED location the extractor produced (kind, street1, street2,
number, city), never a free-text blob, so a local intersection/address index can
resolve it deterministically. Providers return a GeoResult or None.

Confidence: HIGH (exact intersection / address point), MEDIUM (interpolated address,
named place), LOW (street or area centroid), FAILED.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

CONF_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "FAILED": 0}


@dataclass
class GeoQuery:
    kind: str                       # intersection | block | address | landmark | highway
    street1: str | None = None
    street2: str | None = None
    number: str | None = None
    city: str | None = None
    raw: str | None = None
    bias: tuple[float, float] | None = None     # (lat, lon) of the channel's area
    county: str | None = None

    def key(self) -> str:
        norm = {k: (str(v).strip().lower() if v is not None else None)
                for k, v in asdict(self).items() if k not in ("raw", "bias")}
        # bias is rounded so nearby channels share cache entries
        norm["bias"] = [round(self.bias[0], 2), round(self.bias[1], 2)] if self.bias else None
        return hashlib.sha256(json.dumps(norm, sort_keys=True).encode()).hexdigest()[:40]

    def to_json(self) -> dict:
        d = asdict(self)
        d["bias"] = list(self.bias) if self.bias else None
        return d

    def oneline(self) -> str:
        """Free-text rendering for providers that only take a query string."""
        if self.kind in ("intersection", "highway") and self.street1 and self.street2:
            head = f"{self.street1} & {self.street2}"
        elif self.kind in ("address", "block") and self.number and self.street1:
            head = f"{self.number} {self.street1}"
        elif self.kind == "landmark" and self.street1 and self.street2:
            head = f"{self.street1}, {self.street2}"
        else:
            head = self.street1 or self.raw or ""
        parts = [head]
        if self.city:
            parts.append(self.city)
        parts.append("Michigan")
        return ", ".join(p for p in parts if p)


@dataclass
class GeoResult:
    lat: float | None
    lng: float | None
    formatted: str | None
    confidence: str
    source: str
    extra: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return self.lat is not None and self.lng is not None and self.confidence != "FAILED"

    @classmethod
    def failed(cls, source: str = "none") -> "GeoResult":
        return cls(None, None, None, "FAILED", source)

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lng": self.lng, "formatted": self.formatted,
                "confidence": self.confidence, "source": self.source}


class Provider(Protocol):
    name: str

    def supports(self, kind: str) -> bool: ...

    def geocode(self, q: GeoQuery) -> GeoResult | None: ...
