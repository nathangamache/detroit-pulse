"""`local` provider: the $0 local geocoder index (geocode/local), first in the chain.

Build the index with `scripts/build_geocoder_index.py`; it lands in GEOCODER_LOCAL_INDEX
(default <GEOCODER_DATA_DIR>/index = ~/.local/share/detroit-pulse/geocoder/index). In Docker the
index directory is mounted read-only and GEOCODER_LOCAL_INDEX points at it.

GEOCODER_LOCAL_BACKEND ("module:Class", default geocode.local:LocalGeocoder) is the class that
takes `(index_dir)` and exposes `geocode(struct, bias) -> dict | None`; struct uses the keys
kind/street1/street2/number/name/city and the result has lat/lng/formatted/confidence/source.

The index is loaded on the first lookup (about 1 GB of RAM, a few seconds), so only the process
that geocodes (the extract worker) pays for it. While it is missing the provider reports itself
unavailable, retries every 10 minutes, and the chain moves on to Census/Photon/Nominatim.
"""
from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path

import structlog

from core.config_files import default_bias
from core.settings import get_settings
from geocode.base import GeoQuery, GeoResult

log = structlog.get_logger()

# our kinds -> local-index kinds
_KIND = {"intersection": "intersection", "block": "block", "address": "address", "landmark": "landmark",
         "highway": "highway_segment"}
RETRY_S = 600


class LocalIndexProvider:
    name = "local"

    def __init__(self, index_dir: Path | None = None, backend: str | None = None):
        s = get_settings()
        self.index_dir = Path(index_dir) if index_dir else s.geocoder_index_dir
        self.backend_spec = backend if backend is not None else s.geocoder_local_backend
        self._impl = None
        self._lock = threading.Lock()
        self._unavailable_reason: str | None = None
        self._failed_at = 0.0

    @property
    def available(self) -> bool:
        return self._load() is not None

    def status(self) -> dict:
        self._load()
        st = {"provider": self.name, "index_dir": str(self.index_dir) if self.index_dir else None,
              "backend": self.backend_spec or None, "available": self._impl is not None,
              "reason": self._unavailable_reason}
        info = getattr(self._impl, "info", None)
        if callable(info):
            st["index"] = info()
        return st

    def _load(self):
        with self._lock:
            if self._impl is not None:
                return self._impl
            if self._unavailable_reason and time.monotonic() - self._failed_at < RETRY_S:
                return None
            self._unavailable_reason = None
            if not self.index_dir or not Path(self.index_dir).exists():
                self._unavailable_reason = f"no index at {self.index_dir} (run scripts/build_geocoder_index.py)"
            elif not self.backend_spec or ":" not in self.backend_spec:
                self._unavailable_reason = "GEOCODER_LOCAL_BACKEND not set (module:Class)"
            else:
                mod_name, cls_name = self.backend_spec.split(":", 1)
                try:
                    t0 = time.monotonic()
                    cls = getattr(importlib.import_module(mod_name), cls_name)
                    self._impl = cls(str(self.index_dir))
                    log.info("local geocoder index loaded", index_dir=str(self.index_dir),
                             seconds=round(time.monotonic() - t0, 1))
                except Exception as e:  # noqa: BLE001
                    self._unavailable_reason = f"backend failed to load: {e}"
            if self._unavailable_reason:
                self._failed_at = time.monotonic()
                log.info("local geocoder index unavailable; using fallbacks", reason=self._unavailable_reason)
            return self._impl

    def supports(self, kind: str) -> bool:
        return kind in _KIND

    def geocode(self, q: GeoQuery) -> GeoResult | None:
        impl = self._load()
        if impl is None:
            return None
        number = str(q.number).strip() if q.number is not None else ""
        struct = {"kind": _KIND.get(q.kind, q.kind), "street1": q.street1, "street2": q.street2,
                  "number": int(number) if number.isdigit() else None, "city": q.city}
        if q.kind == "landmark":
            struct["name"] = q.street1
            struct["street1"] = q.street2
            struct["street2"] = None
        try:
            r = impl.geocode(struct, bias=q.bias or default_bias())
        except Exception as e:  # noqa: BLE001 - a backend bug must not stop the pipeline
            log.warning("local geocoder error", error=str(e)[:200])
            return None
        if not r or r.get("lat") is None:
            return None
        return GeoResult(float(r["lat"]), float(r["lng"]), r.get("formatted"), r.get("confidence", "MEDIUM"),
                         f"local:{r.get('source', 'index')}"[:30])
