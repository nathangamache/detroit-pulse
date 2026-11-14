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
  - id: off_rail
    label: Retired rail channel
    kind: rail_road
    templates: [off_rail]
    freq_mhz: 160.95
    enabled: false
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


# ── ingest heartbeat (core/liveness.py) ────────────────────────────────────

def _liveness(db) -> dict[str, tuple]:
    with db.connect() as c:
        return {r[0]: (r[1], r[2]) for r in c.execute(text(
            "SELECT component, fingerprint, detail FROM liveness ORDER BY component, up_since")).all()}


def test_sdr_watcher_beats_with_its_receivers_and_what_is_recording_now(db, channels, tmp_path, monkeypatch):
    """After each completed poll the watcher beats (about every INGEST_HEARTBEAT_S): it says
    what is still being written (radio traffic right now) and records its receivers' heartbeat."""
    from core.settings import reset_settings_cache
    status = tmp_path / "receivers"
    status.mkdir()
    (status / "rail-airband.prom").write_text('channel_activity_counter{freq="160.215",label="test_rail"}\t0\n')
    monkeypatch.setenv("RECEIVER_STATUS_DIR", str(status))
    reset_settings_cache()
    missing = SdrFileSource(watch_dir=tmp_path / "nope", min_age_s=1.5)
    missing.poll_once()
    assert missing.maybe_beat() is True
    assert _liveness(db)["ingest:sdr_files"][0] == "blind:no_watch_dir"     # can't see its input

    watch = tmp_path / "sdr" / "2026" / "11" / "14"
    watch.mkdir(parents=True)
    (watch / "aa_fire_20261114_153100.mp3.tmp").write_bytes(b"ID3partial")   # a transmission right now
    stale = watch / "aa_fire_20261114_120000.mp3.tmp"                         # left behind by a crash
    stale.write_bytes(b"ID3")
    _old(stale, 3600)
    (watch / "rail_road_20261114_153300_160215000.mp3").write_bytes(b"ID3")  # closed a moment ago
    (watch / "off_20261114_153400.mp3.tmp").write_bytes(b"ID3")             # a disabled channel
    src = SdrFileSource(watch_dir=tmp_path / "sdr", min_age_s=1.5)
    assert src.poll_once() == 0
    assert src.recording_now == {"aa_fire": 1, "test_rail": 1}
    assert src.maybe_beat() is True and src.maybe_beat() is False            # once per INGEST_HEARTBEAT_S
    rows = _liveness(db)
    assert rows["ingest:sdr_files"] == ("", {"recording_now": {"aa_fire": 1, "test_rail": 1}, "ingested": 0})
    assert rows["receiver:rail-airband"][0] == "test_rail@160.2150"
    _old(watch / "rail_road_20261114_153300_160215000.mp3")
    os.rename(watch / "aa_fire_20261114_153100.mp3.tmp", watch / "aa_fire_20261114_153100.mp3")
    _old(watch / "aa_fire_20261114_153100.mp3")
    assert src.poll_once() == 2 and src.recording_now == {}
    assert src.maybe_beat(force=True) is True
    assert _liveness(db)["ingest:sdr_files"][1]["recording_now"] == {}


def test_sdr_watcher_beats_at_once_when_a_transmission_starts(db, channels, tmp_path, monkeypatch):
    """The watcher polls every second but beats every INGEST_HEARTBEAT_S (15 s): a transmission
    that starts in between is reported by the next poll's beat, not up to 15 s later. Each beat is
    stamped with when its poll began (how far the ingest stage has looked), and a dropped rail
    recording (a disabled rail channel) is reported at once too."""
    import threading

    from core.settings import reset_settings_cache
    monkeypatch.setenv("RECEIVER_STATUS_DIR", str(tmp_path / "receivers"))
    reset_settings_cache()
    watch = tmp_path / "sdr"
    watch.mkdir()
    src = SdrFileSource(watch_dir=watch, min_age_s=1.5)
    stop = threading.Event()
    t = threading.Thread(target=src.run, args=(stop, 0.2), daemon=True)
    t.start()

    def detail_within(seconds: float, pred) -> dict | None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            row = _liveness(db).get("ingest:sdr_files")
            if row and pred(row[1]):
                return row[1]
            time.sleep(0.1)
        return None

    try:
        assert detail_within(5, lambda d: d.get("recording_now") == {}) is not None
        (watch / "rail_road_20261114_153300_160215000.mp3.tmp").write_bytes(b"ID3partial")
        assert detail_within(4, lambda d: d.get("recording_now") == {"test_rail": 1}) is not None
        (watch / "off_rail_20261114_153400_160950000.mp3").write_bytes(b"ID3" + b"\0" * 50)
        _old(watch / "off_rail_20261114_153400_160950000.mp3")
        d = detail_within(4, lambda d: "dropped" in d)
        assert d is not None and d["dropped"] == {"off_rail": {"n": 1, "last": "2026-11-14T15:34:00Z"}}
    finally:
        stop.set()
        t.join(5)
    with db.connect() as c:
        last = c.execute(text("SELECT last_seen FROM liveness WHERE component = 'ingest:sdr_files'")).scalar()
    assert last <= src.checked_at                                     # a poll's start, never later


def test_traintap_beats_only_while_it_can_see_the_csv(db, tmp_path, monkeypatch):
    from core.settings import reset_settings_cache
    monkeypatch.setenv("RECEIVER_STATUS_DIR", str(tmp_path / "receivers"))
    reset_settings_cache()
    csvf = tmp_path / "trains.csv"
    src = TraintapSource(csv_path=csvf, channel="eot_hot")
    assert src.poll_once() == 0 and src.maybe_beat() is True
    assert _liveness(db)["ingest:traintap"][0] == "blind:no_csv"
    assert src.receiver_names() == {"traintap-eot"}
    csvf.write_text(HEADER)
    assert src.poll_once() == 0 and src.maybe_beat(force=True) is True
    with db.connect() as c:
        fps = [r[0] for r in c.execute(text("SELECT fingerprint FROM liveness WHERE component = 'ingest:traintap' "
                                            "ORDER BY up_since")).all()]
    assert fps == ["blind:no_csv", ""]                                       # a new run once it can see it


def test_a_failed_heartbeat_never_stops_ingestion(tmp_path, monkeypatch):
    src = SdrFileSource(watch_dir=tmp_path, min_age_s=1.5)

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(src, "beat", boom)
    assert src.maybe_beat() is False
