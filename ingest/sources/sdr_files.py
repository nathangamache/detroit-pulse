"""`sdr_files` source: watch a directory for one-file-per-transmission SDR recordings.

Primary producer: RTLSDR-Airband (rtl_airband) multichannel NFM with
`split_on_transmission = true`. Its file naming (src/output.cpp
build_output_filename, checked against RTLSDR-Airband master 2026-11-02):

    <directory>[/YYYY/MM/DD]/<filename_template>[_<tx start>]_<file open>[_<freq Hz>].mp3

  * timestamps are `%Y%m%d_%H%M%S`, in UTC unless the global `localtime = true`
  * `_<tx start>` appears only for the 2nd+ part of a transmission longer than
    split_max_file_time when split_include_transmission_start = true
  * `_<freq Hz>` appears only with `include_freq = true` (needed in scan mode)
  * while recording, the file is `<name>.mp3.tmp`; it is renamed on close
  * `rawfile` outputs (.cf32 I/Q) are ignored here

Secondary producer (for P25 trunked systems, which rtl_airband cannot decode):
trunk-recorder writes `<talkgroup>-<epoch>_<freq>[...].wav|m4a` plus a `.json`
sidecar with talkgroup/freq/start_time. If a sidecar exists it wins.

The channel (label, kind, agency, rail subdivision ...) is looked up in
config/channels(.local).yaml by template name, then talkgroup, then frequency.
Recordings of a disabled channel are not ingested; on the rail band they are counted in the
heartbeat's `dropped` (per channel: how many, and the newest), because a receiver still
recording a channel Pulse doesn't ingest means its traffic is not in the database: then
/v1/rail/activity can't call the window quiet.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from core import bus
from core.config_files import RAIL_VOICE_KINDS, match_channel
from core.settings import get_settings
from core.workqueue import enqueue_transmission
from ingest.sources.base import Source

log = structlog.get_logger()

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
# A `.tmp` file rtl_airband is still writing (a transmission in progress) has a fresh mtime; one
# left behind by a crash doesn't, and is not counted as recording now.
TMP_ACTIVE_S = 120.0
# the AAR railroad pool (as /v1/rail/activity's unmapped count): a disabled channel's recording
# here, or one of a rail kind, is rail traffic Pulse dropped
RAIL_BAND_HZ = (160_100_000, 161_580_000)

_AIRBAND_RE = re.compile(
    r"^(?P<template>.+?)"
    r"(?:_(?P<txd>\d{8})_(?P<txt>\d{6}))?"
    r"_(?P<d>\d{8})_(?P<t>\d{6})"
    r"(?:_(?P<freq>\d{5,}))?"
    r"\.(?P<ext>mp3|wav)$"
)
_TRUNK_RE = re.compile(r"^(?P<tg>\d+)-(?P<epoch>\d{9,10})_(?P<freq>\d+(?:\.\d+)?)")


@dataclass
class ParsedName:
    template: str | None
    opened_at: datetime            # when this file was opened (= transmission start for part 1)
    tx_start: datetime             # start of the whole transmission
    freq_hz: int | None
    talkgroup: int | None = None
    part: int = 1                  # 1 for the first file of a transmission, 2+ for continuations
    producer: str = "rtl_airband"


def parse_airband_name(name: str, tz: str = "UTC") -> ParsedName | None:
    m = _AIRBAND_RE.match(name)
    if not m:
        return None
    zone = ZoneInfo(tz)

    def ts(d: str, t: str) -> datetime:
        return datetime.strptime(d + t, "%Y%m%d%H%M%S").replace(tzinfo=zone).astimezone(timezone.utc)

    opened = ts(m["d"], m["t"])
    tx = ts(m["txd"], m["txt"]) if m["txd"] else opened
    return ParsedName(
        template=m["template"], opened_at=opened, tx_start=tx,
        freq_hz=int(m["freq"]) if m["freq"] else None,
        part=1 if tx == opened else 2,
    )


def parse_trunk_recorder(path: Path) -> ParsedName | None:
    side = path.with_suffix(".json")
    meta: dict = {}
    if side.exists():
        try:
            meta = json.loads(side.read_text())
        except (OSError, ValueError):
            meta = {}
    m = _TRUNK_RE.match(path.name)
    if not meta and not m:
        return None
    epoch = meta.get("start_time") or (int(m["epoch"]) if m else None)
    if epoch is None:
        return None
    freq = meta.get("freq") or (float(m["freq"]) if m else None)
    tg = meta.get("talkgroup") or (int(m["tg"]) if m else None)
    at = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    return ParsedName(template=None, opened_at=at, tx_start=at,
                      freq_hz=int(float(freq)) if freq else None,
                      talkgroup=int(tg) if tg is not None else None, producer="trunk-recorder")


def parse_name(path: Path, tz: str = "UTC") -> ParsedName | None:
    if path.with_suffix(".json").exists() or _TRUNK_RE.match(path.name):
        p = parse_trunk_recorder(path)
        if p:
            return p
    return parse_airband_name(path.name, tz)


class SdrFileSource(Source):
    name = "sdr_files"

    def __init__(self, watch_dir: Path | None = None, min_age_s: float | None = None,
                 tz: str | None = None):
        s = get_settings()
        self.watch_dir = Path(watch_dir or s.sdr_watch_dir)
        self.min_age_s = s.sdr_min_file_age_s if min_age_s is None else min_age_s
        self.tz = tz or s.sdr_filename_tz
        self._seen: set[str] = set()
        self._warned: set[str] = set()
        self.last_ingest: datetime | None = None
        self.ingested = 0
        self.last_by_channel: dict[str, datetime] = {}
        self.recording_now: dict[str, int] = {}          # channel id -> files still being written
        # disabled rail channel id -> {"n": recordings not ingested, "last": newest one's start, ISO}
        self.dropped: dict[str, dict] = {}
        self._dropped_seen: set[str] = set()

    def _candidates(self):
        """(path, in_progress) for every audio file; in_progress for `<name>.<ext>.tmp`."""
        if not self.watch_dir.exists():
            return
        for root, _dirs, files in os.walk(self.watch_dir):
            for f in files:
                p = Path(root) / f
                if p.suffix.lower() in AUDIO_EXTS:
                    yield p, False
                elif p.suffix.lower() == ".tmp" and Path(p.stem).suffix.lower() in AUDIO_EXTS:
                    yield p, True

    def _channel_for(self, p: Path) -> str | None:
        """The channel id a recording would be ingested as (None: skipped, e.g. disabled)."""
        parsed = parse_name(p, self.tz)
        if parsed is None:
            return None
        ch = match_channel(parsed.template, parsed.freq_hz, parsed.talkgroup)
        if ch is not None:
            if not ch.enabled:
                self._drop(ch, parsed, p)
            return ch.id if ch.enabled else None
        label = parsed.template or (f"tg{parsed.talkgroup}" if parsed.talkgroup else f"{parsed.freq_hz}")
        return f"unmapped:{label}"

    def _drop(self, ch, parsed: ParsedName, p: Path) -> None:
        """A recording `p` (its final name) of disabled channel `ch` that won't be ingested: counted
        once, from when it is first seen (even still being written), when it is rail traffic."""
        freq = parsed.freq_hz or (int(ch.freq_mhz * 1e6) if ch.freq_mhz else None)
        if not (ch.kind in RAIL_VOICE_KINDS or (freq and RAIL_BAND_HZ[0] <= freq <= RAIL_BAND_HZ[1])):
            return
        key = str(p.resolve())
        if key in self._dropped_seen:
            return
        self._dropped_seen.add(key)
        d = self.dropped.setdefault(ch.id, {"n": 0, "last": None})
        d["n"] += 1
        at = parsed.opened_at.isoformat().replace("+00:00", "Z")
        if d["last"] is None or at > d["last"]:
            d["last"] = at

    def poll_once(self) -> int:
        self.mark_checked()
        n = 0
        now = datetime.now(timezone.utc).timestamp()
        recording: dict[str, int] = {}
        for p, in_progress in self._candidates():
            key = str(p.resolve())
            if key in self._seen:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if in_progress or st.st_size == 0 or now - st.st_mtime < self.min_age_s:
                # still being written: radio traffic now, ingested once the file is closed
                if not in_progress or now - st.st_mtime <= TMP_ACTIVE_S:
                    cid = self._channel_for(p.with_name(p.stem) if in_progress else p)
                    if cid:
                        recording[cid] = recording.get(cid, 0) + 1
                continue
            if self._ingest(p, key):
                n += 1
            self._seen.add(key)
        self.recording_now = recording
        return n

    # ── heartbeat (core/liveness.py) ────────────────────────────────────────
    def blind_reason(self) -> str | None:
        return None if self.watch_dir.is_dir() else "no_watch_dir"

    def beat_detail(self) -> dict:
        out = {"recording_now": dict(self.recording_now), "ingested": self.ingested}
        if self.dropped:
            out["dropped"] = {k: dict(v) for k, v in self.dropped.items()}
        return out

    def urgent(self) -> object:
        """Beat at once when a transmission starts or ends, or a recording is dropped."""
        return (tuple(sorted(self.recording_now.items())),
                tuple(sorted((k, v["n"]) for k, v in self.dropped.items())))

    def receiver_names(self) -> set[str]:
        from core.config_files import load_channels
        return {c.receiver_name for c in load_channels() if c.enabled and c.kind != "eot" and c.receiver_name}

    def _warn_once(self, k: str, msg: str, **kw) -> None:
        if k not in self._warned:
            self._warned.add(k)
            log.warning(msg, **kw)

    def _ingest(self, p: Path, key: str) -> bool:
        parsed = parse_name(p, self.tz)
        if parsed is None:
            self._warn_once("name:" + p.name, "sdr file name not recognised; skipped", file=p.name)
            return False
        ch = match_channel(parsed.template, parsed.freq_hz, parsed.talkgroup)
        if ch is not None and not ch.enabled:
            self._drop(ch, parsed, p)
            return False
        if ch is None:
            label = parsed.template or (f"tg{parsed.talkgroup}" if parsed.talkgroup else f"{parsed.freq_hz}")
            channel_id = f"unmapped:{label}"
            self._warn_once("ch:" + channel_id, "no channel in config/channels.yaml for SDR file; "
                            "ingesting as unmapped", template=parsed.template, freq_hz=parsed.freq_hz)
        else:
            channel_id = ch.id
        freq = parsed.freq_hz or (int(ch.freq_mhz * 1e6) if ch and ch.freq_mhz else None)
        cid = enqueue_transmission(source=self.name, source_key=key, channel_id=channel_id,
                                   captured_at=parsed.opened_at, audio_path=key, freq_hz=freq)
        if cid:
            self.ingested += 1
            self.last_ingest = datetime.now(timezone.utc)
            self.last_by_channel[channel_id] = parsed.opened_at
            bus.ring("transcribe")
            log.info("sdr transmission queued", channel=channel_id, file=p.name, part=parsed.part)
        return bool(cid)

    def status(self) -> dict:
        return {"source": self.name, "watch_dir": str(self.watch_dir), "exists": self.watch_dir.exists(),
                "ingested": self.ingested,
                "last_ingest": self.last_ingest.isoformat() if self.last_ingest else None}
