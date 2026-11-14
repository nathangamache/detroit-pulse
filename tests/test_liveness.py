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


def _observe(db, path, name: str, ago_s: float) -> None:
    """The ingest stage samples receiver `name` whose heartbeat file was last written `ago_s` ago."""
    t = time.time() - ago_s
    os.utime(path, (t, t))
    with db.begin() as c:
        liveness.observe_receiver(c, name)


def test_each_component_has_its_own_max_gap(cfg_dir):
    """A run may bridge only a missed beat or two of that component, not a fixed 150 s: about 10
    of rtl_airband's 15 s stats rewrites (a receiver deaf for two minutes) used to stay one run."""
    from core import config_files
    assert liveness.receiver_max_gap_s("rail-airband", stats=True) == 45          # 15 + 15 + 15
    assert liveness.receiver_max_gap_s("traintap-eot", stats=False) == 105        # 60 + 15 + 30
    assert liveness.ingest_max_gap_s() == 35                                      # 2 x 15 + 5
    assert liveness.max_gap_for("receiver:rail-airband", "csx@160.2150") == 45
    assert liveness.max_gap_for("receiver:traintap-eot", "") == 105
    assert liveness.max_gap_for("ingest:sdr_files", "") == 35
    # a receiver that rewrites its file every 5 s says so in channels.local.yaml
    (cfg_dir / "channels.local.yaml").write_text("receivers:\n  rail-airband: {heartbeat_s: 5}\nchannels: []\n")
    config_files.clear_cache()
    assert liveness.receiver_max_gap_s("rail-airband", stats=True) == 35          # 5 + 15 + 15
    (cfg_dir / "channels.local.yaml").write_text("receivers:\n  rail-airband: {heartbeat_s: -1}\nchannels: []\n")
    config_files.clear_cache()
    with pytest.raises(ValueError):
        liveness.receiver_max_gap_s("rail-airband", stats=True)


def test_a_short_receiver_outage_ends_the_run(db, status_dir):
    """rtl_airband's stats file every 15 s with a 130 s hole, traintap's heartbeat every minute
    with a 120 s hole: each hole ends the run, so the receiver is listening only since it came
    back. Normal jitter (a beat 65 s after the last for traintap, 30 s for a skipped stats
    rewrite) doesn't."""
    prom = status_dir / "rail-airband.prom"
    prom.write_text('channel_activity_counter{freq="160.215",label="csx_road"}\t0\n')
    for ago in list(range(2400, 999, -15)) + [970] + list(range(870, -1, -15)):     # 970: one rewrite skipped
        _observe(db, prom, "rail-airband", ago)
    runs = _runs(db, "receiver:rail-airband")
    assert len(runs) == 2
    assert abs(runs[1][0].timestamp() - (time.time() - 870)) < 2
    hb = status_dir / "traintap-eot.heartbeat"
    hb.write_text("")
    for ago in list(range(2400, 999, -60)) + [935] + list(range(815, -1, -60)):     # 65 s, then a 120 s hole
        _observe(db, hb, "traintap-eot", ago)
    runs = _runs(db, "receiver:traintap-eot")
    assert len(runs) == 2 and abs(runs[1][0].timestamp() - (time.time() - 815)) < 2


def test_rtl_airband_counters_that_went_down_are_a_restart(db, status_dir):
    """rtl_airband restarted between two 15 s rewrites (no gap to see): its counters started again
    at 0, so the run ends there. A read mid-rewrite (lines missing, the last one cut off) proves
    nothing either way."""
    prom = status_dir / "rail-airband.prom"

    def stats(activity: int, squelch: int, tail: str = "") -> str:
        return (f'channel_activity_counter{{freq="160.215",label="csx_road"}}\t{activity}\n'
                f'channel_noise_level{{freq="160.215",label="csx_road"}}\t3.2\n'
                f'channel_squelch_counter{{freq="160.215",label="csx_road"}}\t{squelch}\n'
                f'buffer_overflow_count{{device="0"}}\t0\n' + tail)

    prom.write_text(stats(900, 40))
    _observe(db, prom, "rail-airband", 120)
    prom.write_text(stats(950, 41))
    _observe(db, prom, "rail-airband", 105)
    # mid-rewrite: only the first line, then a counter cut off at "4" (it was 41): no restart
    cut = 'channel_squelch_counter{freq="160.215",label="csx_road"}\t4'
    prom.write_text(stats(960, 41).split("\n")[0] + "\n" + cut)
    _observe(db, prom, "rail-airband", 90)
    assert len(_runs(db, "receiver:rail-airband")) == 1
    with db.connect() as c:
        detail = c.execute(text("SELECT detail FROM liveness WHERE component = 'receiver:rail-airband'")).scalar()
    assert detail["counters"] == {'channel_activity_counter{freq="160.215",label="csx_road"}': 960,   # the
                                  'channel_squelch_counter{freq="160.215",label="csx_road"}': 41}    # run's highest
    prom.write_text(stats(970, 42))
    _observe(db, prom, "rail-airband", 75)
    prom.write_text(stats(3, 0))                          # 15 s later: counters back near 0
    _observe(db, prom, "rail-airband", 60)
    runs = _runs(db, "receiver:rail-airband")
    assert len(runs) == 2 and abs(runs[1][0].timestamp() - (time.time() - 60)) < 2
    # the API reads the file itself too: a reset the ingest stage hasn't recorded yet breaks the
    # current run as well
    from core.receivers import read_heartbeat
    with db.connect() as c:
        run = liveness.runs_since(c, ["receiver:rail-airband"],
                                  datetime.now(timezone.utc) - timedelta(hours=1))["receiver:rail-airband"][0]
    t = time.time() - 50
    for body, continues in ((stats(5, 1), True), (stats(1, 0), False)):
        prom.write_text(body)
        os.utime(prom, (t, t))
        assert liveness.file_continues(run, read_heartbeat("rail-airband"), 45) is continues
    os.utime(prom, (t - 30, t - 30))                                   # not newer than the run's last beat
    assert liveness.file_continues(run, read_heartbeat("rail-airband"), 45) is None
    assert liveness.file_continues(run, None, 45) is None


def test_a_traintap_restart_its_heartbeat_cant_show_ends_the_run(db, status_dir):
    """traintap's heartbeat is a bare touch with no counters, and it touches as soon as it starts
    (upstream Heartbeat._last starts at 0): a restart 10 minutes ago left touches 600 s and 510 s
    ago, 90 s apart, inside the 105 s max gap, and was bridged as one run. The start marker its
    entrypoint writes (a new boot id at every start) ends the run there. A touch by the old
    process that the ingest stage samples only after the new marker appeared (540 s ago, the
    marker 515 s ago) is from before the restart and records nothing."""
    from tests.test_api import _mark
    hb = status_dir / "traintap-eot.heartbeat"
    hb.write_text("")
    _mark(status_dir, "traintap-eot", "boot-1", 3 * 3600)
    for ago in range(7200, 599, -60):
        _observe(db, hb, "traintap-eot", ago)
    _mark(status_dir, "traintap-eot", "boot-2", 515)
    _observe(db, hb, "traintap-eot", 540)
    assert len(_runs(db, "receiver:traintap-eot")) == 1
    assert abs(_runs(db, "receiver:traintap-eot")[0][1].timestamp() - (time.time() - 600)) < 2
    for ago in range(510, -1, -60):
        _observe(db, hb, "traintap-eot", ago)
    runs = _runs(db, "receiver:traintap-eot")
    assert len(runs) == 2 and abs(runs[1][0].timestamp() - (time.time() - 510)) < 2
    with db.connect() as c:
        detail = c.execute(text("SELECT detail FROM liveness WHERE component = 'receiver:traintap-eot' "
                                "ORDER BY up_since DESC LIMIT 1")).scalar()
    assert detail["started"] == "boot-2"


def test_an_rtl_airband_restart_with_every_counter_at_zero_ends_the_run(db, status_dir):
    """Until its first squelch opening since it last started (a crash loop, two quick redeploys)
    every rtl_airband counter is 0, and 0 can't go down: a restart 10 minutes ago (rewrites 615 s
    and 585 s ago, 30 s apart) was one unbroken run. The start marker ends it."""
    from tests.test_api import ROAD_ONLY, _mark
    prom = status_dir / "rail-airband.prom"
    prom.write_text(ROAD_ONLY)                                   # every counter 0
    _mark(status_dir, "rail-airband", "boot-1", 3 * 3600)
    for ago in range(7200, 614, -15):
        _observe(db, prom, "rail-airband", ago)
    _mark(status_dir, "rail-airband", "boot-2", 600)
    for ago in range(585, -1, -15):
        _observe(db, prom, "rail-airband", ago)
    runs = _runs(db, "receiver:rail-airband")
    assert len(runs) == 2 and abs(runs[1][0].timestamp() - (time.time() - 585)) < 2
    # the API reads the files itself too: a marker newer than the run's, not yet sampled, is a
    # restart; the same marker continues the run
    from core.receivers import read_heartbeat
    with db.connect() as c:
        run = liveness.runs_since(c, ["receiver:rail-airband"],
                                  datetime.now(timezone.utc) - timedelta(hours=1))["receiver:rail-airband"][0]
    t = time.time()
    os.utime(prom, (t, t))
    assert liveness.file_continues(run, read_heartbeat("rail-airband"), 45) is True
    _mark(status_dir, "rail-airband", "boot-3", 1)
    assert liveness.file_continues(run, read_heartbeat("rail-airband"), 45) is False


def test_squelch_openings_are_recorded_per_sample(db, status_dir):
    """Every sample of rtl_airband's stats file records how many times each channel's squelch
    opened since the last one (channel_squelch_counter), how many transmissions it dropped as too
    short (channel_split_tx_dropped_counter) and how long the squelch was open
    (channel_activity_counter): the run itself keeps only the newest counters. After a restart
    (a new start marker) the counters count from 0 again."""
    from tests.test_api import _counters, _mark
    prom = status_dir / "rail-airband.prom"
    _mark(status_dir, "rail-airband", "boot-1", 3 * 3600)
    for ago, act, sq, dr in ((120, 900, 40, 2), (105, 900, 40, 2), (90, 960, 43, 3), (75, 970, 44, 3)):
        prom.write_text(_counters("160.215", "csx_road", act, sq, dr) + _counters("160.950", "csx_yard", 10, 1, 0))
        _observe(db, prom, "rail-airband", ago)
    _mark(status_dir, "rail-airband", "boot-2", 70)
    prom.write_text(_counters("160.215", "csx_road", 5, 2, 0) + _counters("160.950", "csx_yard", 0, 0, 0))
    _observe(db, prom, "rail-airband", 60)
    with db.connect() as c:
        rows = [tuple(r) for r in c.execute(text(
            "SELECT at, label, freq_mhz, opens, dropped, active FROM receiver_squelch "
            "WHERE component = 'receiver:rail-airband' ORDER BY at, label")).all()]
    # the first sample has nothing to compare with; 90 s ago: 3 openings, 1 dropped, 60 loops with
    # the squelch open; after the restart: what it counted since it started
    assert [r[1:] for r in rows] == [("csx_road", 160.215, 3, 1, 60), ("csx_road", 160.215, 1, 0, 10),
                                     ("csx_road", 160.215, 2, 0, 5)]
    for (at, *_), ago in zip(rows, (90, 75, 60), strict=True):
        assert abs(at.timestamp() - (time.time() - ago)) < 3


# ── api/rail_verdicts.py (no database) ───────────────────────────────────────

W0, W1 = T0, T0 + timedelta(minutes=30)
ROAD = Channel(id="road", label="Road", kind="rail_voice", freq_mhz=160.215)
YARD = Channel(id="yard", label="Yard", kind="rail_voice", freq_mhz=160.95)
OK = V.IngestProof("sdr_files", up_since=W0 - timedelta(hours=1), checked_at=W1 - timedelta(seconds=4))


def _p(ch, covers=True, state="online", since=W0 - timedelta(hours=1), **kw) -> V.ChannelProof:
    kw.setdefault("heard_until", W1 - timedelta(seconds=9) if since else None)
    return V.ChannelProof(ch=ch, covers=covers, receiver_state=state, listening_since=since,
                          listened_in_window=state == "online", **kw)


def test_verdict_states():
    quiet = V.build("rail_voice", [_p(ROAD), _p(YARD, covers=False, since=None, state="not_installed")], {}, OK,
                    W0, W1)
    assert quiet["state"] == "quiet" and quiet["listening_since"] == "2026-11-14T11:00:00Z"
    # the quiet claim ends at the earliest of the receiver's newest heartbeat and the ingest's last check
    assert quiet["window_start"] == "2026-11-14T12:00:00Z" and quiet["window_end"] == "2026-11-14T12:29:51Z"
    assert "up to 2026-11-14T12:29:51Z (9 s before this answer" in quiet["reasons"][0]
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
    # heard only through a channel that doesn't cover the point; a frequency filed under a disabled one
    via = V.build("rail_voice", [_p(ROAD, via_uncovered=["far_road"])], {}, OK, W0, W1)
    assert via["state"] == "unknown" and via["reasons"] == [
        "road: the receiver records this channel's frequency as far_road, which doesn't cover this point, so those "
        "recordings don't count here"]
    dropped = V.build("rail_voice", [_p(ROAD, since=None, state="not_installed", problem="files it under old")], {},
                      OK, W0, W1)
    assert dropped["state"] == "not_covered" and "road: files it under old" in dropped["reasons"]
    # no record of where the proof ends: never quiet
    assert V.build("rail_voice", [_p(ROAD, heard_until=None)], {}, OK, W0, W1)["state"] == "unknown"
    assert V.build("rail_voice", [_p(ROAD)], {}, V.IngestProof("sdr_files", up_since=W0 - timedelta(hours=1)),
                   W0, W1)["state"] == "unknown"
    stale = V.build("rail_voice", [_p(ROAD)], {}, V.IngestProof("sdr_files", last_beat=W1 - timedelta(minutes=2)),
                    W0, W1)
    assert stale["reasons"] == ["ingest (sdr_files) has no current heartbeat (its last beat was at "
                                "2026-11-14T12:28:00Z)"]
