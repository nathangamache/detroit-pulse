"""Ingest: rtl_airband/trunk-recorder file watcher, traintap CSV tailer, fixtures, legacy stub."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from ingest.sources.broadcastify_legacy import BroadcastifyLegacySource
from ingest.sources.fixture import FixtureSource
from ingest.sources.sdr_files import SdrFileSource, parse_airband_name, parse_name
from ingest.sources.traintap import TraintapSource

CHANNELS = """
channels:
  - id: aa_fire
    label: Test fire dispatch
    kind: fire
    templates: [aa_fire]
    county: Washtenaw
  - id: test_rail
    label: Test rail road channel
    kind: rail_road
    freq_mhz: 160.215
    railroad: TESTRR
    subdivision: Test Sub
  - id: off_channel
    label: Disabled
    templates: ["off"]
    enabled: false
  - id: tg_ch
    label: Trunked talkgroup
    talkgroups: [9101]
"""


def _old(p: Path, age_s: float = 10) -> None:
    t = time.time() - age_s
    os.utime(p, (t, t))


# ── filename parsing (pure) ────────────────────────────────────────────────

def test_parse_airband_basic_and_parts():
    p = parse_airband_name("aa_fire_20261114_153012.mp3")
    assert p.template == "aa_fire" and p.part == 1 and p.freq_hz is None
    assert p.opened_at == datetime(2026, 11, 14, 15, 30, 12, tzinfo=timezone.utc)

    p2 = parse_airband_name("TWR_20260922_101500_20260922_101601.mp3")
    assert p2.template == "TWR" and p2.part == 2
    assert p2.tx_start < p2.opened_at

    p3 = parse_airband_name("rail_road_20261114_153012_160215000.mp3")
    assert p3.template == "rail_road" and p3.freq_hz == 160215000

    assert parse_airband_name("garbage.mp3") is None
    assert parse_airband_name("aa_fire_20261114_153012.mp3.tmp") is None


def test_parse_airband_localtime():
    p = parse_airband_name("aa_fire_20260701_103000.mp3", tz="America/Detroit")
    assert p.opened_at == datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)   # EDT = UTC-4


def test_parse_trunk_recorder_sidecar(tmp_path):
    f = tmp_path / "9101-1763130612_852412500.0-call_7.wav"
    f.write_bytes(b"RIFF")
    (tmp_path / "9101-1763130612_852412500.0-call_7.json").write_text(
        json.dumps({"talkgroup": 9101, "freq": 852412500, "start_time": 1763130612}))
    p = parse_name(f)
    assert p.producer == "trunk-recorder" and p.talkgroup == 9101 and p.freq_hz == 852412500


# ── watcher (DB) ───────────────────────────────────────────────────────────

@pytest.fixture()
def channels(cfg_dir):
    (cfg_dir / "channels.local.yaml").write_text(CHANNELS)
    return cfg_dir


def _rows(db):
    with db.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT channel_id, status, source, audio_path, captured_at, freq_hz "
            "FROM transcript_chunks ORDER BY captured_at")).mappings()]


def test_watcher_ingests_closed_files_once(db, channels, tmp_path):
    watch = tmp_path / "sdr" / "2026" / "11" / "14"           # dated_subdirectories layout
    watch.mkdir(parents=True)
    done = watch / "aa_fire_20261114_153012.mp3"
    done.write_bytes(b"ID3fake")
    _old(done)
    (watch / "aa_fire_20261114_153100.mp3.tmp").write_bytes(b"ID3partial")   # still recording
    fresh = watch / "aa_fire_20261114_153200.mp3"
    fresh.write_bytes(b"ID3fresh")                                            # mtime = now
    (watch / "rail_road_20261114_153300_160215000.mp3").write_bytes(b"ID3")
    _old(watch / "rail_road_20261114_153300_160215000.mp3")
    (watch / "off_20261114_153400.mp3").write_bytes(b"ID3")
    _old(watch / "off_20261114_153400.mp3")
    (watch / "mystery_20261114_153500.mp3").write_bytes(b"ID3")
    _old(watch / "mystery_20261114_153500.mp3")

    src = SdrFileSource(watch_dir=tmp_path / "sdr", min_age_s=1.5)
    assert src.poll_once() == 3            # aa_fire (closed), rail by frequency, unmapped
    rows = _rows(db)
    chans = [r["channel_id"] for r in rows]
    assert chans == ["aa_fire", "test_rail", "unmapped:mystery"]
    assert all(r["status"] == "queued" and r["source"] == "sdr_files" for r in rows)
    assert rows[0]["captured_at"] == datetime(2026, 11, 14, 15, 30, 12, tzinfo=timezone.utc)
    assert rows[1]["freq_hz"] == 160215000

    assert src.poll_once() == 0            # dedupe: nothing new
    _old(fresh)
    assert src.poll_once() == 1            # the fresh file once it stopped changing
    # a restart (new watcher, same files) must not double-queue anything
    assert SdrFileSource(watch_dir=tmp_path / "sdr", min_age_s=1.5).poll_once() == 0
    assert len(_rows(db)) == 4


# ── traintap tailer (DB) ───────────────────────────────────────────────────

HEADER = ("timestamp,epoch,source,freq_hz,valid,corrected,unit_addr,pressure_psig,batt_charge_pct,"
          "batt_cond,message_type,arm_status,motion,marker_light,turbine,valve_ckt,data_block,checkbits_rx\n")


def _pkt(epoch, unit, psig, motion, valid=1, source="EOT", db_bits="1" * 45):
    return (f"2026-11-14 10:00:00.000,{epoch:.3f},{source},457937500,{valid},0,{unit},{psig},94,OK,"
            f"000,Normal,{motion},1,0,0,{db_bits},{'0' * 18}\n")


def test_traintap_tail_and_interpret(db, tmp_path):
    csvf = tmp_path / "trains.csv"
    t0 = 1763114400.0
    csvf.write_text(HEADER + _pkt(t0, 45678, 90, 1) + _pkt(t0 + 60, 45678, 88, 0)
                    + _pkt(t0 + 70, 11111, 0, 0, valid=0))
    src = TraintapSource(csv_path=csvf, channel="eot_hot")
    assert src.poll_once() == 2                      # invalid (BCH-failed) row skipped
    # emergency: the same unit's pipe collapses from 88 to 2 psig; half-written line waits
    with open(csvf, "a") as f:
        f.write(_pkt(t0 + 120, 45678, 2, 0))
        f.write("2026-11-14 10:03:00.000,17631146")   # partial line, no newline yet
    assert src.poll_once() == 1
    with open(csvf, "a") as f:
        f.write("00.000,EOT,457937500,1,0,22222,0,90,OK,000,Normal,0,0,0,0," + "0" * 45 + "," + "0" * 18 + "\n")
    assert src.poll_once() == 1
    with db.connect() as c:
        rows = c.execute(text("SELECT unit_id, brake_pipe_psig, motion, status, emergency "
                              "FROM rail_telemetry ORDER BY ts")).all()
    assert [(r[0], r[3], r[4]) for r in rows] == [
        (45678, "moving", False),
        (45678, "stopped_released", False),
        (45678, "emergency_stopped", True),
        (22222, "no_air", False),                     # cold EOT at 0 psig is not an emergency
    ]
    assert src.poll_once() == 0
    # traintap rotates the file when its header changes: follow the new inode from the top
    os.rename(csvf, tmp_path / "trains.csv.1")
    csvf.write_text(HEADER + _pkt(t0 + 600, 33333, 91, 1))
    assert src.poll_once() == 1


def test_fixture_text_goes_straight_to_extraction(db, tmp_path):
    f = tmp_path / "fx.jsonl"
    f.write_text('{"channel_id": "aa_fire", "text": "Engine 5 respond to Packard and State", "offset_s": 0}\n'
                 '{"channel_id": "aa_fire", "text": "Engine 5 copy", "offset_s": 4}\n')
    src = FixtureSource(f, run_id="t1")
    assert src.poll_once() == 2
    assert src.poll_once() == 0
    with db.connect() as c:
        st = c.execute(text("SELECT status, raw_transcript FROM transcript_chunks ORDER BY captured_at")).all()
    assert [s[0] for s in st] == ["transcribed", "transcribed"]


def test_broadcastify_legacy_never_runs(monkeypatch):
    monkeypatch.setenv("BROADCASTIFY_LEGACY_ENABLED", "true")
    monkeypatch.setenv("BROADCASTIFY_TOS_ACKNOWLEDGED", "true")
    from core.settings import reset_settings_cache
    reset_settings_cache()
    src = BroadcastifyLegacySource()
    assert src.poll_once() == 0
    assert "Terms" in src.status()["warning"] and src.status()["enabled"] is False
