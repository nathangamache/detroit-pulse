"""Liveness history (core/liveness.py) and the verdict builder (api/rail_verdicts.py)."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from api import rail_verdicts as V
from core import liveness
from core.config_files import Channel
from core.settings import reset_settings_cache

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _runs(db, component: str) -> list[tuple]:
    with db.connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT up_since, last_seen, fingerprint FROM liveness WHERE component = :c ORDER BY up_since"),
            {"c": component}).all()]


def _beat(db, component: str, at: datetime, fp: str = "", detail: dict | None = None) -> bool:
    with db.begin() as c:
        return liveness.beat(c, component, at, fp, detail, max_gap_s=150)


def test_beats_extend_a_run_until_a_gap_or_a_change(db):
    comp = "receiver:rail-airband"
    assert _beat(db, comp, T0) is True
    for s in (15, 30, 45, 160):                                      # 115 s between 45 and 160: still one run
        assert _beat(db, comp, T0 + timedelta(seconds=s)) is False
    assert _runs(db, comp) == [(T0, T0 + timedelta(seconds=160), "")]
    assert _beat(db, comp, T0 + timedelta(seconds=160)) is False       # the same heartbeat seen again
    assert _beat(db, comp, T0 + timedelta(seconds=100)) is False       # an older one: nothing new
    assert _beat(db, comp, T0 + timedelta(seconds=400)) is True        # 240 s silent: a new run
    assert _beat(db, comp, T0 + timedelta(seconds=415), "csx@160.2150") is True   # another channel list
    assert [r[0] for r in _runs(db, comp)] == [T0, T0 + timedelta(seconds=400), T0 + timedelta(seconds=415)]
    with db.connect() as c:
        runs = liveness.runs_since(c, [comp, "ingest:none"], T0 + timedelta(seconds=300))
    assert [r.up_since for r in runs[comp]] == [T0 + timedelta(seconds=415), T0 + timedelta(seconds=400)]
    assert runs["ingest:none"] == []
    cur = liveness.current(runs[comp], T0 + timedelta(seconds=500), 150)
    assert cur is not None and cur.fingerprint == "csx@160.2150"
    assert liveness.current(runs[comp], T0 + timedelta(seconds=600), 150) is None


def test_run_covers_only_the_channels_it_listed():
    run = liveness.Run("receiver:x", T0, T0, "csx@160.2150", {"pairs": [["csx", 160.215]]})
    assert run.covers(Channel(id="csx", label="c", kind="rail_voice", freq_mhz=160.215))
    assert not run.covers(Channel(id="csx", label="c", kind="rail_voice", freq_mhz=160.95))
    assert not run.covers(Channel(id="yard", label="y", kind="rail_voice", freq_mhz=160.95))
    assert liveness.Run("receiver:t", T0, T0, "", {}).covers(Channel(id="eot", label="e", kind="eot"))


@pytest.fixture()
def status_dir(tmp_path, monkeypatch):
    d = tmp_path / "receivers"
    d.mkdir()
    monkeypatch.setenv("RECEIVER_STATUS_DIR", str(d))
    monkeypatch.setenv("TRAINTAP_CSV", str(tmp_path / "traintap" / "trains.csv"))
    reset_settings_cache()
    yield d
    reset_settings_cache()


def test_observe_receiver_records_the_file_mtime_and_channel_list(db, status_dir):
    prom = status_dir / "rail-airband.prom"
    prom.write_text('channel_activity_counter{freq="160.215",label="csx_road"}\t0\n')
    t = time.time() - 20
    os.utime(prom, (t, t))
    with db.begin() as c:
        assert liveness.observe_receiver(c, "rail-airband") is True
        assert liveness.observe_receiver(c, "traintap-eot") is False       # never wrote a heartbeat
    (up, last, fp), = _runs(db, "receiver:rail-airband")
    assert abs(up.timestamp() - t) < 0.01 and up == last and fp == "csx_road@160.2150"
    prom.write_text("")                                                    # read mid-rewrite: skipped
    with db.begin() as c:
        assert liveness.observe_receiver(c, "rail-airband") is False
    assert len(_runs(db, "receiver:rail-airband")) == 1
    (status_dir / "traintap-eot.heartbeat").write_text("")
    with db.begin() as c:
        assert liveness.observe_receiver(c, "traintap-eot") is True
    assert _runs(db, "receiver:traintap-eot")[0][2] == ""


def test_record_ingest_beats_and_observes_its_receivers(db, status_dir):
    (status_dir / "traintap-eot.heartbeat").write_text("")
    liveness.record_ingest("traintap", receivers={"traintap-eot", "nothing-here"}, detail={"ingested": 3})
    liveness.record_ingest("sdr_files", blind="no_watch_dir")
    (_, _, fp), = _runs(db, "ingest:traintap")
    assert fp == "" and len(_runs(db, "receiver:traintap-eot")) == 1 and _runs(db, "receiver:nothing-here") == []
    assert _runs(db, "ingest:sdr_files")[0][2] == "blind:no_watch_dir"
    with db.connect() as c:
        detail = c.execute(text("SELECT detail FROM liveness WHERE component = 'ingest:traintap'")).scalar()
    assert detail == {"ingested": 3}


# ── api/rail_verdicts.py (no database) ───────────────────────────────────────

W0, W1 = T0, T0 + timedelta(minutes=30)
ROAD = Channel(id="road", label="Road", kind="rail_voice", freq_mhz=160.215)
YARD = Channel(id="yard", label="Yard", kind="rail_voice", freq_mhz=160.95)
OK = V.IngestProof("sdr_files", up_since=W0 - timedelta(hours=1))


def _p(ch, covers=True, state="online", since=W0 - timedelta(hours=1), **kw) -> V.ChannelProof:
    return V.ChannelProof(ch=ch, covers=covers, receiver_state=state, listening_since=since,
                          listened_in_window=state == "online", **kw)


def test_verdict_states():
    quiet = V.build("rail_voice", [_p(ROAD), _p(YARD, covers=False, since=None, state="not_installed")], {}, OK,
                    W0, W1)
    assert quiet["state"] == "quiet" and quiet["listening_since"] == "2026-11-14T11:00:00Z"
    assert quiet["window_start"] == "2026-11-14T12:00:00Z" and quiet["window_end"] == "2026-11-14T12:30:00Z"
    assert quiet["channels"] == [
        {"feed_id": "road", "receiver_state": "online", "covers_point": True,
         "listening_since": "2026-11-14T11:00:00Z"},
        {"feed_id": "yard", "receiver_state": "not_installed", "covers_point": False}]
    # any message from a covering channel, whatever else is missing
    act = V.build("rail_voice", [_p(ROAD, state="not_installed", since=None)], {"recordings_without_text": 2},
                  V.IngestProof("sdr_files"), W0, W1)
    assert act["state"] == "activity" and act["reasons"] == [
        "heard on channels that cover this point: 2 recordings without text"]
    assert V.build("rail_voice", [], {}, OK, W0, W1)["state"] == "not_covered"
    assert V.build("rail_voice", [_p(ROAD, covers=False)], {}, OK, W0, W1)["state"] == "not_covered"
    off = V.build("rail_voice", [_p(ROAD, state="offline", since=None, last_seen_at="2026-11-14T10:00:00Z")], {}, OK,
                  W0, W1)
    assert off["state"] == "not_covered"
    assert "road: receiver offline (last heard 2026-11-14T10:00:00Z)" in off["reasons"]
    late = V.build("rail_voice", [_p(ROAD, since=W0 + timedelta(minutes=5))], {}, OK, W0, W1)
    assert late["state"] == "unknown" and late["listening_since"] == "2026-11-14T12:05:00Z"
    # every covering channel must be proven, not just one
    part = V.build("rail_voice", [_p(ROAD), _p(YARD, state="not_installed", since=None)], {}, OK, W0, W1)
    assert part["state"] == "unknown" and part["reasons"] == ["yard: no receiver has ever reported"]
    assert "listening_since" not in part
    assert V.build("rail_voice", [_p(ROAD)], {}, V.IngestProof("sdr_files", up_since=W0 + timedelta(seconds=1)),
                   W0, W1)["state"] == "unknown"
    assert V.build("rail_voice", [_p(ROAD)], {}, V.IngestProof("sdr_files", blind="no_watch_dir"), W0, W1)[
        "reasons"] == ["ingest (sdr_files) can't see its input (no_watch_dir)"]
    assert V.build("rail_voice", [_p(ROAD)], {}, OK, W0, W1, unattributed=["3 stray"])["state"] == "unknown"
    mism = V.build("rail_voice", [_p(ROAD, state="not_installed", since=None, reported_freqs=[161.1])], {}, OK, W0, W1)
    assert mism["state"] == "not_covered"
    assert "road: the receiver reports this channel on 161.1 MHz, not the configured 160.215 MHz" in mism["reasons"]
    many = V.build("rail_voice", [_p(Channel(id=f"c{i}", label="c", kind="rail_voice"), since=None) for i in range(3)],
                   {}, OK, W0, W1)
    assert many["reasons"] == ["c0, c1, c2: no heartbeat history shows the receiver listening for the whole window"]
    assert set(V.STATES) == {"activity", "quiet", "not_covered", "unknown"}
