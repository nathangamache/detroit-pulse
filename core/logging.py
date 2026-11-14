"""structlog setup shared by every process."""
from __future__ import annotations

import logging
import re
import sys

import structlog

from core.settings import get_settings

_SECRETISH = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+")


def _scrub(_, __, event_dict):
    """Last line of defence: mask anything that looks like an Anthropic key."""
    for k, v in list(event_dict.items()):
        if isinstance(v, str) and "sk-ant-" in v:
            event_dict[k] = _SECRETISH.sub(r"\1…", v)
    return event_dict


def setup_logging() -> None:
    s = get_settings()
    level = getattr(logging, s.log_level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")
    for noisy in ("httpx", "httpcore", "faster_whisper", "anthropic"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    renderer = structlog.processors.JSONRenderer() if s.log_json else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
