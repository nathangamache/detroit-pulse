"""SQLAlchemy engine/session helpers (one engine per process)."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.settings import get_settings

_lock = threading.Lock()
_engine: Engine | None = None
_sessionmaker: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine, _sessionmaker
    with _lock:
        if _engine is None:
            _engine = create_engine(
                get_settings().db_url,
                pool_size=5,
                max_overflow=5,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
            _sessionmaker = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Dispose the engine (tests switch databases between modules)."""
    global _engine, _sessionmaker
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _sessionmaker = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on error, always close."""
    get_engine()
    assert _sessionmaker is not None
    s = _sessionmaker()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
