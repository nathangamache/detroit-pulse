"""`verdicts` for /v1/rail/activity (contract v1.2, additive): per kind of rail radio, may the
point be called quiet?

    verdicts: {"rail_voice": V, "eot": V}
    V = {"state": "activity" | "quiet" | "not_covered" | "unknown",
         "reasons": [str], "window_start": ISO, "window_end": ISO, "listening_since"?: ISO,
         "counts": {"transcripts", "detector_readouts", "recordings_without_text", "eot_packets"},
         "channels": [{"feed_id", "receiver_state", "listening_since"?, "covers_point"}]}

`activity`    something was heard in the window on a channel that covers the point: a
              transcript, a detector readout, a recording with no text yet (queued,
              transcribing, failed, no speech, or still being written) or an EOT/HOT packet.
              Channels that don't cover the point never count for it.
`not_covered` no channel of that kind covers the point (by its own radius / area / subdivision
              area, never by name or a fixed distance), or none that does was listened to at
              any time in the window.
`unknown`     nothing was heard, but Pulse can't prove nothing was said: see `reasons`.
`quiet`       nothing was heard, and for the WHOLE window every channel that covers the point
              was listened to: its receiver's heartbeat history shows it up since before
              `window_start` with the configured frequency, the ingest stage's heartbeat
              history shows it up since before `window_start` and able to see its input, and
              nothing in the window is left unattributed (unmapped recordings or packets).

Pulse never says `quiet` on less than that. The inputs are built by api/routes/v1.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.config_files import Channel

STATES = ("activity", "quiet", "not_covered", "unknown")
COUNT_KEYS = ("transcripts", "detector_readouts", "recordings_without_text", "eot_packets")
_NOUN = {"rail_voice": "rail voice", "eot": "EOT/HOT"}


def iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    return t.isoformat().replace("+00:00", "Z")


@dataclass
class ChannelProof:
    ch: Channel
    covers: bool
    receiver_state: str                         # not_installed | offline | online (now)
    last_seen_at: str | None = None
    listening_since: datetime | None = None     # the receiver's current run, if it listens to ch
    listened_in_window: bool = False            # some run in the window listened to ch, or online now
    reported_freqs: list[float] = field(default_factory=list)   # its label, on another frequency


@dataclass
class IngestProof:
    source: str                                 # sdr_files | traintap
    up_since: datetime | None = None            # the current run, while it can see its input
    blind: str | None = None                    # why the current run can't see its input


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _grouped(items: list[tuple[str, str]]) -> list[str]:
    """[(feed, reason)] -> ["feed1, feed2: reason"], in first-seen order."""
    by: dict[str, list[str]] = {}
    for feed, why in items:
        by.setdefault(why, []).append(feed)
    return [f"{', '.join(feeds)}: {why}" for why, feeds in by.items()]


def _channel_gap(p: ChannelProof, window_start: datetime) -> str | None:
    """Why `p` (a covering channel) isn't proven for the whole window, or None if it is."""
    if p.listening_since is not None and p.listening_since <= window_start:
        return None
    if p.reported_freqs:
        reported = ", ".join(f"{f:g}" for f in p.reported_freqs)
        return f"the receiver reports this channel on {reported} MHz, not the configured {p.ch.freq_mhz:g} MHz"
    if p.listening_since is not None:
        return f"receiver listening only since {iso(p.listening_since)}, after the window start"
    if p.receiver_state == "not_installed":
        return "no receiver has ever reported"
    if p.receiver_state == "offline":
        return f"receiver offline (last heard {p.last_seen_at})" if p.last_seen_at else "receiver offline"
    if p.listened_in_window:
        return "no heartbeat history shows the receiver listening for the whole window"
    return "no heartbeat history shows the receiver listening"


def build(kind: str, proofs: list[ChannelProof], counts: dict[str, int], ingest: IngestProof,
          window_start: datetime, window_end: datetime, unattributed: list[str] | None = None,
          no_place: list[Channel] | None = None) -> dict:
    """One verdict V. `proofs`: every enabled, non-example channel of this kind that was
    considered (covering or not). `counts`: messages in the window from the covering ones.
    `unattributed`: reasons something in the window can't be tied to a channel (blocks quiet).
    `no_place`: channels of this kind with no receiver area on record."""
    noun = _NOUN[kind]
    counts = {k: int(counts.get(k, 0)) for k in COUNT_KEYS}
    covering = [p for p in proofs if p.covers]
    reasons: list[str] = []
    listening_since = None
    if sum(counts.values()):
        state = "activity"
        heard = [(counts["transcripts"], "transcript"), (counts["detector_readouts"], "detector readout"),
                 (counts["recordings_without_text"], "recording without text", "recordings without text"),
                 (counts["eot_packets"], "EOT/HOT packet")]
        reasons.append("heard on channels that cover this point: "
                       + ", ".join(_n(*h) for h in heard if h[0]))
    elif not covering:
        state = "not_covered"
        reasons.append(f"no {noun} channel covers this point")
        if no_place:
            reasons.append(f"{', '.join(c.id for c in no_place)}: no receiver area on record "
                           "(a bias point or area_id with a radius, or a subdivision area)")
    else:
        gaps = [(p.ch.id, g) for p in covering if (g := _channel_gap(p, window_start))]
        if not any(p.listened_in_window or p.listening_since for p in covering):
            state = "not_covered"
            reasons.append(f"no receiver listened to the {noun} channels that cover this point in the window")
            reasons += _grouped(gaps)
        else:
            problems = _grouped(gaps)
            if ingest.blind:
                problems.append(f"ingest ({ingest.source}) can't see its input ({ingest.blind})")
            elif ingest.up_since is None:
                problems.append(f"ingest ({ingest.source}) has no current heartbeat")
            elif ingest.up_since > window_start:
                problems.append(f"ingest ({ingest.source}) heartbeat only since {iso(ingest.up_since)}, "
                                "after the window start")
            problems += unattributed or []
            if problems:
                state = "unknown"
                reasons += problems
            else:
                state = "quiet"
                reasons.append(f"every {noun} channel that covers this point was listened to for the whole "
                               "window (receiver and ingest heartbeats) and nothing was heard")
    if covering and ingest.up_since and not ingest.blind and all(p.listening_since for p in covering):
        listening_since = max([ingest.up_since] + [p.listening_since for p in covering])
    out = {"state": state, "reasons": reasons, "window_start": iso(window_start), "window_end": iso(window_end),
           "counts": counts, "channels": []}
    if listening_since is not None:
        out["listening_since"] = iso(listening_since)
    for p in proofs:
        c = {"feed_id": p.ch.id, "receiver_state": p.receiver_state, "covers_point": p.covers}
        if p.listening_since is not None:
            c["listening_since"] = iso(p.listening_since)
        out["channels"].append(c)
    return out
