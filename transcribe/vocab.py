"""Whisper `initial_prompt` builder.

faster-whisper keeps only the last ~223 tokens of the prompt
(`previous_tokens[-(max_length // 2 - 1):]`), so anything past that budget is
silently cut from the FRONT. We therefore order hints from least to most
specific and trim from the front ourselves:

    generic profile (config/vocab/<profile>.txt)
    -> coverage-area street names (config/areas(.local).yaml `vocab`)
    -> channel-specific names (config/channels(.local).yaml `vocab`)
    -> "[Previous transmission: ...]" tail for the same channel

Personal areas only ever come from the gitignored local config.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from core.config_files import Channel, load_areas
from core.settings import get_settings

# ~3.5 characters per Whisper token for comma-separated proper nouns; leave headroom.
PROMPT_CHAR_BUDGET = 700
CONTEXT_CHAR_BUDGET = 160


@lru_cache(maxsize=8)
def _profile_terms(profile: str) -> tuple[str, ...]:
    path = Path(get_settings().config_dir) / "vocab" / f"{profile}.txt"
    if not path.exists():
        return ()
    terms: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        terms.extend(t.strip() for t in line.split(",") if t.strip())
    return tuple(terms)


def _dedupe(terms: list[str]) -> list[str]:
    """Keep the LAST occurrence (the most specific position) of each term."""
    seen: set[str] = set()
    out: list[str] = []
    for t in reversed(terms):
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return list(reversed(out))


def hint_terms(channel: Channel | None) -> list[str]:
    profile = "rail" if channel is not None and channel.is_rail else "dispatch"
    terms = list(_profile_terms(profile))
    areas = load_areas()
    if channel is not None and channel.area_id:
        # this channel's area last (most specific); other areas before it
        areas = sorted(areas, key=lambda a: a.id == channel.area_id)
    for a in areas:
        terms.extend(a.vocab)
    if channel is not None:
        terms.extend(channel.vocab)
    return _dedupe(terms)


def build_prompt(channel: Channel | None, context_tail: str = "") -> str:
    terms = hint_terms(channel)
    hints = ", ".join(terms)
    if len(hints) > PROMPT_CHAR_BUDGET:
        # drop whole terms from the front (least specific) until it fits
        while terms and len(", ".join(terms)) > PROMPT_CHAR_BUDGET:
            terms.pop(0)
        hints = ", ".join(terms)
    tail = ""
    if context_tail:
        ctx = context_tail.strip()
        if len(ctx) > CONTEXT_CHAR_BUDGET:
            ctx = ctx[-CONTEXT_CHAR_BUDGET:].split(" ", 1)[-1]
        tail = f" [Previous transmission: {ctx}]"
    return (hints + "." + tail).strip() if hints else tail.strip()
