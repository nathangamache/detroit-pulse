"""Index files: gzip'd pickles read with a restricted unpickler, plus manifest.json.

The index is built on this box by scripts/build_geocoder_index.py. Even so, loading uses an
allowlist unpickler: only plain containers and numpy arrays can come back, never arbitrary
classes or callables, so a tampered index file can't run code in the worker.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import pickle
from pathlib import Path
from typing import Any

INDEX_FORMAT = 2            # bump when the pickled structures change
OSM_FILE = "osm.pkl.gz"
ADDR_FILE = "addr.pkl.gz"
MANIFEST = "manifest.json"

_ALLOWED = {
    ("numpy", "dtype"),
    ("numpy", "ndarray"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy._core.numeric", "_frombuffer"),
    ("numpy.core.multiarray", "_reconstruct"),     # numpy 1.x spelling
    ("numpy.core.multiarray", "scalar"),
    ("numpy.core.numeric", "_frombuffer"),
}


class _SafeUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if (module, name) in _ALLOWED:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"geocoder index: refusing to load {module}.{name}")


def save(obj: Any, path: Path, level: int = 5) -> int:
    """Write atomically (tmp file + rename); returns the size in bytes."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=level) as f:
        pickle.dump(obj, f, protocol=4)
    os.replace(tmp, path)
    return path.stat().st_size


def load(path: Path) -> Any:
    with gzip.open(path, "rb") as f:
        return _SafeUnpickler(io.BufferedReader(f)).load()


def write_manifest(index_dir: Path, manifest: dict) -> None:
    p = Path(index_dir) / MANIFEST
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, p)


def read_manifest(index_dir: Path) -> dict:
    p = Path(index_dir) / MANIFEST
    return json.loads(p.read_text()) if p.exists() else {}
