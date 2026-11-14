"""`local` provider: adapter for the $0 local geocoder index (OSM intersections + address points).

The index itself is built by a separate workflow (free-geocoder). This adapter only
loads it: set

    GEOCODER_LOCAL_INDEX=/path/to/index/dir
    GEOCODER_LOCAL_BACKEND=free_geocoder:FreeGeocoder     # "module:Class" on PYTHONPATH

The backend class must accept `(index_dir)` and expose
`geocode(struct: dict, bias: tuple[float, float]) -> dict | None`, where struct uses the
keys kind/street1/street2/number/name/city and the result has lat/lng/formatted/
confidence/source. Until both are configured the provider reports itself unavailable
and the chain moves on to Census/Photon/Nominatim.
"""
from __future__ import annotations

import importlib
import threading
from pathlib import Path

import structlog

from core.config_files import default_bias
from core.settings import get_settings
from geocode.base import GeoQuery, GeoResult

log = structlog.get_logger()

# our kinds -> free-geocoder kinds
_KIND = {"intersection": "intersection", "block": "block", "address": "address", "landmark": "landmark",
         "highway": "highway_segment"}


class LocalIndexProvider:
    name = "local"

    def __init__(self, index_dir: Path | None = None, backend: str | None = None):
        s = get_settings()
        self.index_dir = Path(index_dir) if index_dir else (Path(s.geocoder_local_index) if s.geocoder_local_index else None)
        self.backend_spec = backend if backend is not None else s.geocoder_local_backend
        self._impl = None
        self._lock = threading.Lock()
        self._unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self._load() is not None

    def status(self) -> dict:
        self._load()
        return {"provider": self.name, "index_dir": str(self.index_dir) if self.index_dir else None,
                "backend": self.backend_spec or None, "available": self._impl is not None,
                "reason": self._unavailable_reason}

    def _load(self):
        with self._lock:
            if self._impl is not None or self._unavailable_reason:
                return self._impl
            if not self.index_dir or not self.index_dir.exists():
                self._unavailable_reason = "GEOCODER_LOCAL_INDEX not set or missing"
            elif not self.backend_spec or ":" not in self.backend_spec:
                self._unavailable_reason = "GEOCODER_LOCAL_BACKEND not set (module:Class)"
            else:
                mod_name, cls_name = self.backend_spec.split(":", 1)
                try:
                    cls = getattr(importlib.import_module(mod_name), cls_name)
                    self._impl = cls(str(self.index_dir))
                except Exception as e:  # noqa: BLE001
                    self._unavailable_reason = f"backend failed to load: {e}"
            if self._unavailable_reason:
                log.info("local geocoder index unavailable; using fallbacks", reason=self._unavailable_reason)
            return self._impl

    def supports(self, kind: str) -> bool:
        return kind in _KIND

    def geocode(self, q: GeoQuery) -> GeoResult | None:
        impl = self._load()
        if impl is None:
            return None
        struct = {"kind": _KIND.get(q.kind, q.kind), "street1": q.street1, "street2": q.street2,
                  "number": int(q.number) if q.number and q.number.isdigit() else None, "city": q.city}
        if q.kind == "landmark":
            struct["name"] = q.street1
            struct["street1"] = q.street2
            struct["street2"] = None
        try:
            r = impl.geocode(struct, bias=q.bias or default_bias())
        except Exception as e:  # noqa: BLE001 - a backend bug must not stop the pipeline
            log.warning("local geocoder error", error=str(e))
            return None
        if not r or r.get("lat") is None:
            return None
        return GeoResult(float(r["lat"]), float(r["lng"]), r.get("formatted"), r.get("confidence", "MEDIUM"),
                         f"local:{r.get('source', 'index')}"[:30])
