"""/v1/rail/activity `verdicts` (contract v1.2): "quiet" only on positive proof for the whole window.

One test per rule (api/rail_verdicts.py), end to end against detroitpulse_test:
  1. a channel covers the point only by its own area (radius / area / subdivision area)
  2. its receiver was listening for the whole window (heartbeat history, not just "online now")
  3. the receiver demodulates the configured frequency (a label on another frequency doesn't count)
  4. the ingest stage beat for the whole window and could see its input
  5. nothing at all was heard on the covering channels (and nothing in the window is unattributed)
Fictional geography only: the test points used across tests/test_api.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_api import BOTH_TUNED, NEAR, QUIET, QUIET_RAIL, _beats, _listening, _recording, _states

CENTER = (42.25, -83.70)          # the Test Sub center, and the EOT receiver's bias point


def _client(cfg_dir, tmp_path, monkeypatch, channels: dict, rail: dict = QUIET_RAIL):
    from core import config_files
    from core.settings import reset_settings_cache
    (cfg_dir / "channels.local.yaml").write_text(yaml.safe_dump(channels))
    (cfg_dir / "rail_channels.local.yaml").write_text(yaml.safe_dump(rail))
    config_files.clear_cache()
    status = tmp_path / "receivers"
    status.mkdir(parents=True)
    monkeypatch.setenv("RECEIVER_STATUS_DIR", str(status))
    monkeypatch.setenv("TRAINTAP_CSV", str(tmp_path / "traintap" / "trains.csv"))
    reset_settings_cache()
    from api.main import create_app
    return TestClient(create_app()), status


@pytest.fixture()
def quiet(db, cfg_dir, tmp_path, monkeypatch):
    client, status = _client(cfg_dir, tmp_path, monkeypatch, QUIET)
    return client, status, db


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _get(client, **params) -> dict:
    return client.get("/v1/rail/activity", params={**NEAR, **params}).json()


def _packet(c, minutes_ago: float, key: str, channel: str = "eot_hot", source: str = "EOT") -> None:
    c.execute(text("""INSERT INTO rail_telemetry (ts, channel, source, unit_id, brake_pipe_psig, motion, status,
                      dedupe_key) VALUES (:t, :ch, :src, 12345, 88, true, 'moving', :k)"""),
              {"t": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), "ch": channel, "src": source,
               "k": key})


def test_verdict_shape_and_the_quiet_case(quiet):
    """With 40 minutes of receiver and ingest history and nothing heard, both kinds are quiet,
    and the verdict says over which window and since when Pulse has been listening."""
    client, status, db = quiet
    _listening(db, status, BOTH_TUNED)
    a = _get(client)
    v = a["verdicts"]["rail_voice"]
    assert set(v) == {"state", "reasons", "window_start", "window_end", "listening_since", "counts", "channels"}
    assert v["state"] == "quiet" and len(v["reasons"]) == 1 and "whole window" in v["reasons"][0]
    assert v["counts"] == {"transcripts": 0, "detector_readouts": 0, "recordings_without_text": 0, "eot_packets": 0}
    assert _t(v["listening_since"]) < _t(v["window_start"]) < _t(v["window_end"])
    assert [c["feed_id"] for c in v["channels"]] == ["csx_road", "csx_yard"]
    assert all(c["covers_point"] and c["receiver_state"] == "online" and c["listening_since"] for c in v["channels"])
    assert a["verdicts"]["eot"]["state"] == "quiet"
    assert a["verdicts"]["eot"]["channels"][0]["feed_id"] == "eot_hot"
    # since_minutes=30, and the quiet claim ends at Pulse's newest heartbeat (here the ingest
    # stage's, 15 s before the request), never at the request time
    assert 0 < 30 * 60 - (_t(v["window_end"]) - _t(v["window_start"])).total_seconds() < 40
    assert _t(v["window_end"]) < _t(a["generated_at"]) and " s before this answer" in v["reasons"][0]


# ── rule 1: coverage by the channel's own area, never by name or a fixed 25 km ─────────────

EOT3 = {"channels": [
    {"id": "eot_hot", "label": "EOT/HOT telemetry", "kind": "eot", "freq_mhz": 457.9375,
     "bias": {"lat": CENTER[0], "lon": CENTER[1]}, "coverage": {"radius_km": 3}}]}


def test_eot_reaches_only_its_own_radius(db, cfg_dir, tmp_path, monkeypatch):
    """An EOT/HOT receiver that hears 3 km is not listed, quiet or "activity" 10 or 20 km away
    (the old rule listed it for any point within 25 km of its bias)."""
    client, status = _client(cfg_dir, tmp_path, monkeypatch, EOT3)
    _listening(db, status, prom=None)
    with db.begin() as c:
        _packet(c, 2, "p1")
        _packet(c, 3, "p2")
    for dlat in (0.09, 0.18):                                                # about 10 km and 20 km
        far = _get(client, lat=CENTER[0] + dlat, lon=CENTER[1])
        assert far["coverage"]["channels"] == [] and far["eot_hot"] == [] and far["coverage"]["covered"] is False
        assert "outside every eot channel's coverage radius" in far["coverage"]["note"]
        v = far["verdicts"]["eot"]
        assert v["state"] == "not_covered" and v["counts"]["eot_packets"] == 0
        assert v["channels"] == [{"feed_id": "eot_hot", "receiver_state": "online", "covers_point": False,
                                  "listening_since": v["channels"][0]["listening_since"]}]
        assert "no EOT/HOT channel covers this point" in v["reasons"]
    one_km = _get(client, lat=CENTER[0] + 0.009, lon=CENTER[1])
    assert [x["kind"] for x in one_km["coverage"]["channels"]] == ["eot"] and len(one_km["eot_hot"]) == 2
    assert one_km["verdicts"]["eot"]["state"] == "activity" and one_km["verdicts"]["eot"]["counts"]["eot_packets"] == 2


def test_eot_without_a_radius_covers_no_point(db, cfg_dir, tmp_path, monkeypatch):
    """A bias point alone (or no place at all) is no reach: never "shown unfiltered"."""
    for eot in ({"bias": {"lat": CENTER[0], "lon": CENTER[1]}}, {}):
        chans = {"channels": [{"id": "eot_hot", "label": "EOT", "kind": "eot", "freq_mhz": 457.9375, **eot}]}
        client, status = _client(cfg_dir, tmp_path / str(len(eot)), monkeypatch, chans)
        _listening(db, status, prom=None)
        with db.begin() as c:
            _packet(c, 1, f"p-{len(eot)}")
        a = _get(client, lat=CENTER[0], lon=CENTER[1])
        assert a["eot_hot"] == [] and a["coverage"]["channels"] == [], eot
        assert a["verdicts"]["eot"]["state"] == "not_covered", eot
        assert any("eot_hot: no receiver area on record" in x for x in a["verdicts"]["eot"]["reasons"]), eot
        assert "needs a receiver area on record" in a["coverage"]["note"], eot
        with db.begin() as c:
            c.execute(text("TRUNCATE liveness, rail_telemetry"))


OWN_AREA = {"channels": QUIET["channels"] + [
    # on the Test Sub (linked by name), but its own receiver hears only 2 km around the center
    {"id": "short_road", "label": "CSX Test short-range road", "kind": "rail_road", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.47, "bias": {"lat": CENTER[0], "lon": CENTER[1]},
     "coverage": {"radius_km": 2}},
    # on the Test Sub, no place of its own, but says it hears 3 km
    {"id": "capped_road", "label": "CSX Test capped road", "kind": "rail_road", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.53, "coverage": {"radius_km": 3}}]}
ALL_FOUR = BOTH_TUNED + ('channel_activity_counter{freq="160.470",label="short_road"}\t0\n'
                         'channel_activity_counter{freq="160.530",label="capped_road"}\t0\n')


def test_voice_coverage_is_the_channels_own_area(db, cfg_dir, tmp_path, monkeypatch):
    """A channel's own area decides, and a subdivision never stretches it; a channel with only a
    radius reaches that far from the subdivision's center. Traffic on a channel that doesn't
    cover the point never counts for it (it used to make the point "activity")."""
    client, status = _client(cfg_dir, tmp_path, monkeypatch, OWN_AREA)
    _listening(db, status, ALL_FOUR)
    with db.begin() as c:
        _recording(c, "short_road", "done", "Q100 by the signal", 2, "short-1")
        _recording(c, "capped_road", "queued", None, 2, "capped-1")
    four_km = _get(client, lat=CENTER[0] + 0.036, lon=CENTER[1])          # inside the 8 km subdivision
    # csx_road says 5 km and csx_yard has only the subdivision's 8 km: both reach 4 km
    assert {c["channel"] for c in four_km["coverage"]["channels"] if c["kind"] != "eot"} == {
        "CSX Test Sub road", "CSX Test yard"}
    assert four_km["transcripts"] == [] and "recordings_without_text" not in four_km["coverage"]
    covers = {c["feed_id"]: c["covers_point"] for c in four_km["verdicts"]["rail_voice"]["channels"]}
    assert covers == {"csx_road": True, "csx_yard": True, "short_road": False, "capped_road": False}
    assert four_km["verdicts"]["rail_voice"]["state"] == "quiet"
    one_km = _get(client, lat=CENTER[0] + 0.009, lon=CENTER[1])
    v = one_km["verdicts"]["rail_voice"]
    assert all(c["covers_point"] for c in v["channels"])
    assert v["state"] == "activity" and v["counts"]["transcripts"] == 1 and v["counts"]["recordings_without_text"] == 1
    assert [t["feed_id"] for t in one_km["transcripts"]] == ["short_road"]


# ── rule 2: listening for the whole window ───────────────────────────────────────────────

def test_a_receiver_that_came_back_mid_window_is_never_quiet(quiet):
    """The receiver was last heard 40 minutes ago, then came back a minute ago: the 30 minute
    (or 12 hour) window is not quiet, though the receiver is "online" now."""
    import os
    import time
    client, status, db = quiet
    (status / "rail-airband.prom").write_text(BOTH_TUNED)
    (status / "traintap-eot.heartbeat").write_text("")
    old = time.time() - 40 * 60
    for p in status.iterdir():
        os.utime(p, (old, old))
    from core.liveness import pairs_fingerprint
    from core.receivers import parse_airband_pairs
    pairs = parse_airband_pairs(BOTH_TUNED)
    fp, detail = pairs_fingerprint(pairs), {"pairs": [list(p) for p in pairs]}
    _beats(db, "receiver:rail-airband", 120, fp, detail, until_s=40 * 60)    # up from 2 h ago to 40 min ago
    _beats(db, "receiver:traintap-eot", 120, until_s=40 * 60)
    _beats(db, "ingest:sdr_files", 120)
    _beats(db, "ingest:traintap", 120)
    a = _get(client)
    assert {c["receiver_state"] for c in a["coverage"]["channels"]} == {"offline"}
    assert _states(a) == {"rail_voice": "not_covered", "eot": "not_covered"}
    # it comes back: fresh files and a new run that started two minutes ago
    (status / "rail-airband.prom").write_text(BOTH_TUNED)
    (status / "traintap-eot.heartbeat").write_text("")
    _beats(db, "receiver:rail-airband", 2, fp, detail)
    _beats(db, "receiver:traintap-eot", 2)
    for minutes in (30, 720):
        a = _get(client, since_minutes=minutes)
        assert {c["receiver_state"] for c in a["coverage"]["channels"]} == {"online"}, minutes
        assert "they were quiet" not in a["coverage"]["note"], minutes   # it used to say so
        assert _states(a) == {"rail_voice": "unknown", "eot": "unknown"}, minutes
        v = a["verdicts"]["rail_voice"]
        assert any("receiver listening only since" in x for x in v["reasons"]), minutes
        assert _t(v["listening_since"]) > _t(v["window_start"]), minutes
    # a window that starts after it came back can be quiet
    assert _states(_get(client, since_minutes=1)) == {"rail_voice": "quiet", "eot": "quiet"}


def test_a_gap_in_the_history_ends_the_run(quiet):
    """Two runs with a 10 minute hole between them: the receiver was not proven listening in the
    hole, so a window over it is unknown; a window after it is quiet."""
    client, status, db = quiet
    _listening(db, status, BOTH_TUNED, minutes=60)
    with db.begin() as c:                                    # punch a hole 20-10 minutes ago
        c.execute(text("DELETE FROM liveness WHERE component = 'receiver:rail-airband'"))
    from core.liveness import pairs_fingerprint
    from core.receivers import parse_airband_pairs
    pairs = parse_airband_pairs(BOTH_TUNED)
    fp, detail = pairs_fingerprint(pairs), {"pairs": [list(p) for p in pairs]}
    _beats(db, "receiver:rail-airband", 60, fp, detail, until_s=20 * 60)
    _beats(db, "receiver:rail-airband", 10, fp, detail)
    a = _get(client)
    assert "they were quiet" not in a["coverage"]["note"] and _states(a)["rail_voice"] == "unknown"
    assert _states(_get(client, since_minutes=9))["rail_voice"] == "quiet"


def _sample(db, path, name: str, agos) -> None:
    """The ingest stage's sampling of receiver `name` (core/liveness.observe_receiver, the real
    path): its heartbeat file last written each of `agos` seconds ago, in turn."""
    import os
    import time

    from core import liveness
    for ago in agos:
        t = time.time() - ago
        os.utime(path, (t, t))
        with db.begin() as c:
            liveness.observe_receiver(c, name)


def test_a_two_minute_outage_inside_the_window_is_never_quiet(quiet):
    """rtl_airband's stats file is rewritten every 15 s and traintap's heartbeat once a minute.
    A 130 s hole in the one and a 120 s hole in the other, 15 minutes ago: each receiver was deaf
    for about two minutes inside the window, so neither kind is quiet (one 150 s max gap for
    every component used to bridge both holes: "listening for the whole window")."""
    client, status, db = quiet
    prom, hb = status / "rail-airband.prom", status / "traintap-eot.heartbeat"
    prom.write_text(BOTH_TUNED)
    hb.write_text("")
    _sample(db, prom, "rail-airband", list(range(2400, 999, -15)) + list(range(870, -1, -15)))
    _sample(db, hb, "traintap-eot", list(range(2400, 999, -60)) + list(range(880, -1, -60)))
    _beats(db, "ingest:sdr_files", 40)
    _beats(db, "ingest:traintap", 40)
    a = _get(client)
    assert {c["receiver_state"] for c in a["coverage"]["channels"]} == {"online"}
    assert _states(a) == {"rail_voice": "unknown", "eot": "unknown"}
    assert "they were quiet" not in a["coverage"]["note"]
    for kind, back in (("rail_voice", 870), ("eot", 880)):
        v = a["verdicts"][kind]
        assert any("receiver listening only since" in x for x in v["reasons"]), kind
        assert _t(v["listening_since"]) > _t(v["window_start"]), kind
        assert abs((_t(a["generated_at"]) - _t(v["listening_since"])).total_seconds() - back) < 5, kind
    # the same history without the holes is quiet
    with db.begin() as c:
        c.execute(text("DELETE FROM liveness WHERE component LIKE 'receiver:%'"))
    _sample(db, prom, "rail-airband", range(2400, -1, -15))
    _sample(db, hb, "traintap-eot", range(2400, -1, -60))
    assert _states(_get(client)) == {"rail_voice": "quiet", "eot": "quiet"}


def test_an_rtl_airband_restart_inside_the_window_is_never_quiet(quiet):
    """rtl_airband restarted ten minutes ago and was back before its next 15 s rewrite: no gap in
    the stats file's mtimes, but its counters started again at 0. The receiver is listening only
    since then. A restart the ingest stage hasn't recorded yet (the file read now) counts too."""
    import os
    import time
    client, status, db = quiet
    prom = status / "rail-airband.prom"

    def stats(n: int) -> str:
        return "".join(f'channel_activity_counter{{freq="{f}",label="{cid}"}}\t{n}\n'
                       f'channel_squelch_counter{{freq="{f}",label="{cid}"}}\t{n // 10}\n'
                       for f, cid in (("160.215", "csx_road"), ("160.950", "csx_yard")))
    _listening(db, status, prom=None, ingest=True)
    for i, ago in enumerate(range(2400, -1, -15)):
        prom.write_text(stats(5000 + i if ago > 600 else i))         # back to 0 ten minutes ago
        _sample(db, prom, "rail-airband", [ago])
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and any("receiver listening only since" in x for x in v["reasons"])
    assert abs((datetime.now(timezone.utc) - _t(v["listening_since"])).total_seconds() - 600) < 20
    assert _states(_get(client, since_minutes=9))["rail_voice"] == "quiet"
    # it restarts again now: the ingest stage hasn't sampled the file since
    prom.write_text(stats(0))
    t = time.time()
    os.utime(prom, (t, t))
    v = _get(client, since_minutes=9)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and any("receiver listening only since" in x for x in v["reasons"])


# ── rule 3: the configured frequency ─────────────────────────────────────────────────────

STALE_RENDER = ('channel_activity_counter{freq="161.100",label="csx_road"}\t0\n'
                'channel_activity_counter{freq="161.130",label="csx_yard"}\t0\n')


def test_a_label_on_another_frequency_is_not_listening(quiet):
    """rtl_airband still runs the old render: its stats file names csx_road and csx_yard, but on
    other frequencies than channels.local.yaml. They are not being listened to (it used to read
    as online and quiet)."""
    client, status, db = quiet
    _listening(db, status, STALE_RENDER)
    st = {f["feed_id"]: f["receiver"]["state"] for f in client.get("/v1/feeds/status").json()["feeds"]}
    assert st["csx_road"] == "not_installed" and st["csx_yard"] == "not_installed"
    a = _get(client)
    assert "they were quiet" not in a["coverage"]["note"]
    assert {c["receiver_state"] for c in a["coverage"]["channels"] if c["kind"] != "eot"} == {"not_installed"}
    v = a["verdicts"]["rail_voice"]
    assert v["state"] == "not_covered"
    assert "csx_road: the receiver reports this channel on 161.1 MHz, not the configured 160.215 MHz" in v["reasons"]
    assert all("listening_since" not in c for c in v["channels"])
    # one right, one wrong: still not quiet
    mixed = 'channel_activity_counter{freq="160.215",label="csx_road"}\t0\n' + STALE_RENDER.splitlines()[1] + "\n"
    with db.begin() as c:
        c.execute(text("DELETE FROM liveness WHERE component = 'receiver:rail-airband'"))
    _listening(db, status, mixed, eot=False, ingest=False)
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown"
    assert "csx_yard: the receiver reports this channel on 161.13 MHz, not the configured 160.95 MHz" in v["reasons"]


def test_a_render_change_since_the_window_start_is_not_quiet(quiet):
    """The history was recorded with another channel list (rail.local.conf re-rendered ten
    minutes ago, which restarts rtl_airband): the receiver has listened to csx_yard only since."""
    client, status, db = quiet
    from core.liveness import pairs_fingerprint
    from core.receivers import parse_airband_pairs
    road = parse_airband_pairs('channel_activity_counter{freq="160.215",label="csx_road"}\t0\n')
    _beats(db, "receiver:rail-airband", 60, pairs_fingerprint(road), {"pairs": [list(p) for p in road]},
           until_s=10 * 60)
    _listening(db, status, BOTH_TUNED, minutes=10, eot=True, ingest=False)
    _beats(db, "ingest:sdr_files", 60)
    _beats(db, "ingest:traintap", 60)
    a = _get(client)
    assert "they were quiet" not in a["coverage"]["note"]
    v = a["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and any("listening only since" in x for x in v["reasons"])


RENAMED = {"channels": [
    # csx_road was csx_road_old: the old entry is kept disabled, and rail.local.conf still has it
    {"id": "csx_road", "label": "CSX Test Sub road", "kind": "rail_road", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.215, "coverage": {"radius_km": 5}},
    {"id": "csx_road_old", "label": "CSX Test Sub road (old)", "kind": "rail_road", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.215, "enabled": False},
    QUIET["channels"][1], QUIET["channels"][3]]}
OLD_RENDER = ('channel_activity_counter{freq="160.215",label="csx_road_old"}\t0\n'
              'channel_activity_counter{freq="160.950",label="csx_yard"}\t0\n')


def test_a_frequency_filed_under_a_disabled_channel_is_not_listening(db, cfg_dir, tmp_path, monkeypatch):
    """A channel renamed in channels.local.yaml, its old id kept `enabled: false`, rail.local.conf
    not re-rendered: rtl_airband still demodulates 160.215 MHz, but labels it (and names its
    files) csx_road_old, whose recordings sdr_files drops. csx_road is not being listened to
    (its frequency used to vouch for it: "quiet"), and a dropped recording in the window is
    traffic Pulse didn't ingest."""
    import time

    from ingest.sources.sdr_files import SdrFileSource
    client, status = _client(cfg_dir, tmp_path, monkeypatch, RENAMED)
    _listening(db, status, OLD_RENDER)
    st = {f["feed_id"]: f["receiver"]["state"] for f in client.get("/v1/feeds/status").json()["feeds"]}
    assert st["csx_road"] == "not_installed" and st["csx_yard"] == "online"
    a = _get(client)
    v = a["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and "they were quiet" not in a["coverage"]["note"]
    assert ("csx_road: the receiver files 160.215 MHz under csx_road_old, a disabled channel, so those "
            "recordings are dropped (re-render rail.local.conf)") in v["reasons"]
    road = next(c for c in v["channels"] if c["feed_id"] == "csx_road")
    assert road["covers_point"] and "listening_since" not in road
    # a recording from inside the window under the old name: dropped, and counted as such
    watch = tmp_path / "sdr"
    watch.mkdir()
    name = datetime.fromtimestamp(time.time() - 300, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    (watch / f"csx_road_old_{name}_160215000.mp3").write_bytes(b"ID3" + b"\0" * 100)
    src = SdrFileSource(watch_dir=watch, min_age_s=0)
    assert src.poll_once() == 0 and src.dropped["csx_road_old"]["n"] == 1
    assert src.maybe_beat(force=True) is True
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown"
    assert any(x.startswith("recordings of the disabled channel csx_road_old in the window") for x in v["reasons"])
    # re-rendered: csx_road is listened to from then on, and the dropped recording still blocks quiet
    with db.begin() as c:
        c.execute(text("DELETE FROM liveness WHERE component = 'receiver:rail-airband'"))
    _listening(db, status, BOTH_TUNED, eot=False, ingest=False)
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and not any("csx_road: " in x for x in v["reasons"])
    assert _get(client, since_minutes=4)["verdicts"]["rail_voice"]["state"] == "quiet"    # before it


SHARED = {"channels": [
    # the road channel's own receiver area is 2 km around the subdivision center ...
    {"id": "csx_road", "label": "CSX Test Sub road", "kind": "rail_road", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.215, "coverage": {"radius_km": 2}},
    # ... while the detector on the same frequency has the whole 8 km subdivision
    {"id": "csx_det", "label": "CSX Test Sub detectors", "kind": "rail_detector", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.215},
    QUIET["channels"][1], QUIET["channels"][3]]}


def test_a_channel_heard_through_another_counts_only_where_that_one_does(db, cfg_dir, tmp_path, monkeypatch):
    """rtl_airband demodulates a shared frequency once, under the first channel (csx_road): a
    detector on it is heard through csx_road and its traffic is filed as csx_road's. At a point
    the detector covers but csx_road doesn't, csx_road's recordings never count, so the detector
    can't be called quiet there (its frequency used to vouch for it: "quiet" with traffic on
    csx_road)."""
    client, status = _client(cfg_dir, tmp_path, monkeypatch, SHARED)
    _listening(db, status, BOTH_TUNED)
    with db.begin() as c:
        _recording(c, "csx_road", "done", "detector milepost 4 no defects", 3, "sh-1")
    four_km = _get(client, lat=CENTER[0] + 0.036, lon=CENTER[1])
    v = four_km["verdicts"]["rail_voice"]
    covers = {c["feed_id"]: c["covers_point"] for c in v["channels"]}
    assert covers == {"csx_road": False, "csx_det": True, "csx_yard": True}
    assert v["state"] == "unknown" and v["counts"]["transcripts"] == 0
    assert ("csx_det: the receiver records this channel's frequency as csx_road, which doesn't cover this "
            "point, so those recordings don't count here") in v["reasons"]
    # where both cover the point, the detector is heard through csx_road and its traffic counts
    one_km = _get(client, lat=CENTER[0] + 0.009, lon=CENTER[1])["verdicts"]["rail_voice"]
    assert one_km["state"] == "activity" and one_km["counts"]["transcripts"] == 1


# ── rule 4: the ingest stage ─────────────────────────────────────────────────────────────

def test_quiet_needs_the_ingest_stage_for_the_whole_window(quiet):
    """Receivers proven, but the stage that turns their files and packets into rows (pulse-
    transcribe's sdr_files and traintap sources) isn't: never quiet."""
    client, status, db = quiet
    _listening(db, status, BOTH_TUNED, ingest=False)
    a = _get(client)
    assert "they were quiet" not in a["coverage"]["note"]                   # it used to say so
    assert _states(a) == {"rail_voice": "unknown", "eot": "unknown"}
    assert "ingest (sdr_files) has no current heartbeat" in a["verdicts"]["rail_voice"]["reasons"]
    assert "ingest (traintap) has no current heartbeat" in a["verdicts"]["eot"]["reasons"]
    assert "listening_since" not in a["verdicts"]["rail_voice"]
    # it stopped beating 10 minutes ago (pulse-transcribe down): no current heartbeat either
    _beats(db, "ingest:sdr_files", 60, until_s=10 * 60)
    assert any(x.startswith("ingest (sdr_files) has no current heartbeat (its last beat was at ")
               for x in _get(client)["verdicts"]["rail_voice"]["reasons"])
    # it is beating again, but only for 3 minutes
    _beats(db, "ingest:sdr_files", 3)
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown"
    assert any(x.startswith("ingest (sdr_files) heartbeat only since") for x in v["reasons"])
    # beating, but it can't see the watch directory (the volume isn't mounted)
    with db.begin() as c:
        c.execute(text("TRUNCATE liveness"))
    _listening(db, status, BOTH_TUNED, ingest=False)
    _beats(db, "ingest:sdr_files", 60, "blind:no_watch_dir", until_s=5)
    _beats(db, "ingest:traintap", 40)
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and "ingest (sdr_files) can't see its input (no_watch_dir)" in v["reasons"]
    # it can see it again: a new run, which doesn't reach back over the window
    _beats(db, "ingest:sdr_files", 0, until_s=0)
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "unknown"
    assert any(x.startswith("ingest (sdr_files) heartbeat only since") for x in v["reasons"])
    # and once it has beaten for the whole window
    with db.begin() as c:
        c.execute(text("DELETE FROM liveness WHERE component = 'ingest:sdr_files'"))
    _beats(db, "ingest:sdr_files", 40)
    assert _states(_get(client)) == {"rail_voice": "quiet", "eot": "quiet"}


def _last_beat(db, component: str) -> datetime:
    with db.connect() as c:
        return c.execute(text("SELECT max(last_seen) FROM liveness WHERE component = :c"), {"c": component}).scalar()


def test_quiet_ends_at_the_ingest_stages_last_check(quiet, tmp_path):
    """The ingest stage last looked at the recordings 15 s ago (nothing being written then); a
    transmission began 8 s ago. "Quiet" may cover the window only up to that last look, never
    up to the request: `window_end` is the ingest stage's last beat and the reason says so. The
    real sdr_files source's next poll sees the transmission and beats at once: activity."""
    import time

    from ingest.sources.sdr_files import SdrFileSource
    client, status, db = quiet
    _listening(db, status, BOTH_TUNED, ingest=False)
    _beats(db, "ingest:sdr_files", 40, "", {"recording_now": {}}, until_s=12)
    _beats(db, "ingest:traintap", 40)
    a = _get(client)
    v = a["verdicts"]["rail_voice"]
    last = _last_beat(db, "ingest:sdr_files")
    assert v["state"] == "quiet" and _t(v["window_end"]) == last      # it used to be the request time
    assert (_t(a["generated_at"]) - _t(v["window_end"])).total_seconds() >= 12
    assert f"up to {v['window_end']}" in v["reasons"][0] and "s before this answer" in v["reasons"][0]
    watch = tmp_path / "sdr"
    watch.mkdir()
    started = datetime.fromtimestamp(time.time() - 8, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    (watch / f"csx_road_{started}_160215000.mp3.tmp").write_bytes(b"ID3partial")
    src = SdrFileSource(watch_dir=watch, min_age_s=1.5)
    assert src.poll_once() == 0 and src.recording_now == {"csx_road": 1}
    assert src.maybe_beat(force=src.urgent() != getattr(src, "_beat_urgent", None)) is True
    assert _last_beat(db, "ingest:sdr_files") == src.checked_at        # the poll's start, not the beat's
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "activity" and v["counts"]["recordings_without_text"] == 1


def test_a_stalled_ingest_stage_is_not_current(quiet, tmp_path):
    """The ingest stage last beat 120 s ago and has been hung since; two transmissions finished
    100 s and 60 s ago are on disk but not in the database. Its heartbeat is not current (two
    missed beats end it, not 150 s), so this is unknown, not quiet. Once it resumes it ingests
    both: activity."""
    import os
    import time

    from ingest.sources.sdr_files import SdrFileSource
    client, status, db = quiet
    _listening(db, status, BOTH_TUNED, ingest=False)
    _beats(db, "ingest:sdr_files", 40, "", {"recording_now": {}}, until_s=120)
    _beats(db, "ingest:traintap", 40)
    watch = tmp_path / "sdr"
    watch.mkdir()
    for ago in (100, 60):
        name = datetime.fromtimestamp(time.time() - ago, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        f = watch / f"csx_road_{name}_160215000.mp3"
        f.write_bytes(b"ID3" + b"\0" * 100)
        t = time.time() - ago + 5
        os.utime(f, (t, t))
    a = _get(client)
    v = a["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and "they were quiet" not in a["coverage"]["note"]
    assert any(x.startswith("ingest (sdr_files) has no current heartbeat (its last beat was at ") for x in v["reasons"])
    assert _states(a)["eot"] == "quiet"                                 # traintap is still beating
    src = SdrFileSource(watch_dir=watch, min_age_s=1.5)
    assert src.poll_once() == 2 and src.maybe_beat() is True
    v = _get(client)["verdicts"]["rail_voice"]
    assert v["state"] == "activity" and v["counts"]["recordings_without_text"] == 2


# ── rule 5: nothing heard at all ─────────────────────────────────────────────────────────

FAR_CHANNEL = {"channels": QUIET["channels"] + [
    {"id": "far_road", "label": "NS Far Sub road", "kind": "rail_road", "railroad": "NS", "subdivision": "Far Sub",
     "freq_mhz": 160.8, "bias": {"lat": 42.9, "lon": -83.0}, "coverage": {"radius_km": 10}}]}


def test_any_message_on_a_covering_channel_is_activity(db, cfg_dir, tmp_path, monkeypatch):
    """With full proof, each kind of message alone makes the point "activity"; messages on a
    channel that doesn't cover it never do, and traffic no channel claims blocks "quiet"."""
    client, status = _client(cfg_dir, tmp_path, monkeypatch, FAR_CHANNEL)
    _listening(db, status, BOTH_TUNED + 'channel_activity_counter{freq="160.800",label="far_road"}\t0\n')

    def voice() -> dict:
        return _get(client)["verdicts"]["rail_voice"]

    with db.begin() as c:                                    # far_road doesn't cover NEAR
        _recording(c, "far_road", "done", "far line traffic", 2, "far-1")
        _recording(c, "far_road", "queued", None, 2, "far-2")
        _packet(c, 1, "far-eot", channel="eot_hot", source="EOT")
    assert voice()["state"] == "quiet"
    cases = [
        ("transcripts", lambda c: _recording(c, "csx_road", "done", "Q326 copy", 3, "m1")),
        ("recordings_without_text", lambda c: _recording(c, "csx_yard", "queued", None, 3, "m2")),
        ("recordings_without_text", lambda c: _recording(c, "csx_yard", "transcribing", None, 3, "m3")),
        ("recordings_without_text", lambda c: _recording(c, "csx_road", "error", None, 3, "m4")),
        ("recordings_without_text", lambda c: _recording(c, "csx_road", "done", "", 3, "m5")),
        ("detector_readouts", lambda c: c.execute(text(
            """INSERT INTO rail_detector_events (channel_id, ts, railroad, subdivision, detector_name, result,
               alarms, parse_source, raw_text) VALUES ('csx_road', now() - interval '3 minutes', 'CSX', 'Test Sub',
               'detector', 'no_defects', '[]', 'rules', 'no defects')"""))),
    ]
    for key, seed in cases:
        with db.begin() as c:
            c.execute(text("DELETE FROM rail_detector_events"))
            c.execute(text("DELETE FROM transcript_chunks WHERE channel_id <> 'far_road'"))
            seed(c)
        v = voice()
        assert v["state"] == "activity" and v["counts"][key] == 1, key
        assert sum(v["counts"].values()) == 1, key                         # far_road's traffic never counts
    with db.begin() as c:
        c.execute(text("DELETE FROM rail_detector_events"))
        c.execute(text("DELETE FROM transcript_chunks WHERE channel_id <> 'far_road'"))
    # a recording still being written at the ingest stage's last beat (rtl_airband's .tmp file)
    _beats(db, "ingest:sdr_files", 0, "", {"recording_now": {"csx_road": 1, "far_road": 3}}, until_s=0)
    v = voice()
    assert v["state"] == "activity" and v["counts"]["recordings_without_text"] == 1
    a = _get(client)
    assert a["coverage"]["recordings_without_text"] == {"pending": 1, "error": 0, "no_speech": 0}
    _beats(db, "ingest:sdr_files", 0, "", {"recording_now": {}}, until_s=0)
    assert voice()["state"] == "quiet"
    # a rail-band recording no configured channel claims: Pulse can't place it, so never quiet
    with db.begin() as c:
        c.execute(text("""INSERT INTO transcript_chunks (chunk_id, feed_id, channel_id, "timestamp", captured_at,
                          status, source, source_key, freq_hz)
                          VALUES (gen_random_uuid(), 'unmapped:old_tmpl', 'unmapped:old_tmpl', now(), now(),
                          'queued', 'sdr_files', 'u1', 160635000)"""))
        c.execute(text("""INSERT INTO transcript_chunks (chunk_id, feed_id, channel_id, "timestamp", captured_at,
                          status, source, source_key, freq_hz)
                          VALUES (gen_random_uuid(), 'unmapped:tg9101', 'unmapped:tg9101', now(), now(),
                          'queued', 'sdr_files', 'u2', 852412500)"""))
    v = voice()
    assert v["state"] == "unknown" and v["counts"]["recordings_without_text"] == 0
    assert any("1 recording on rail frequencies" in x and "unmapped" in x for x in v["reasons"])   # not the 852 MHz one
    # EOT: an attributed packet is activity; one no eot channel claims blocks quiet
    with db.begin() as c:
        c.execute(text("TRUNCATE rail_telemetry"))
    assert _get(client)["verdicts"]["eot"]["state"] == "quiet"
    with db.begin() as c:
        _packet(c, 1, "dpu-1", source="DPU")
    e = _get(client)
    assert e["verdicts"]["eot"]["state"] == "activity" and e["verdicts"]["eot"]["counts"]["eot_packets"] == 1
    assert e["eot_hot"] == []                                                # DPU isn't listed, but it is traffic
    with db.begin() as c:
        c.execute(text("TRUNCATE rail_telemetry"))
        _packet(c, 1, "stray-1", channel="eot_other")
    e = _get(client)["verdicts"]["eot"]
    assert e["state"] == "unknown" and any("not attributed to a configured eot channel" in x for x in e["reasons"])


def test_recordings_stamped_before_the_window_they_arrived_in_are_not_quiet(quiet):
    """rtl_airband writes local time (`localtime = true`) but SDR_FILENAME_TZ says UTC: a
    transmission ingested a minute ago is stamped five hours ago, outside every window. Pulse
    can't place it, so the window isn't quiet (it used to be)."""
    client, status, db = quiet
    _listening(db, status, BOTH_TUNED)
    with db.begin() as c:
        _recording(c, "csx_road", "done", "Q326 copy", 5 * 60, "tz-1", ingested_minutes_ago=1)
    a = _get(client)
    assert a["transcripts"] == [] and "they were quiet" not in a["coverage"]["note"]
    v = a["verdicts"]["rail_voice"]
    assert v["state"] == "unknown" and v["counts"]["transcripts"] == 0
    assert any("ingested in the window but stamped before it" in x for x in v["reasons"])


def test_detector_readouts_on_another_channel_never_count(db, cfg_dir, tmp_path, monkeypatch):
    """A readout heard on a channel that doesn't cover the point isn't this point's, even when it
    names a nearby subdivision (it used to be listed by subdivision name)."""
    client, status = _client(cfg_dir, tmp_path, monkeypatch, FAR_CHANNEL)
    _listening(db, status, BOTH_TUNED)
    with db.begin() as c:
        cid = _recording(c, "far_road", "done", "detector milepost 12", 3, "fd-1")
        c.execute(text("""INSERT INTO rail_detector_events (chunk_id, channel_id, ts, railroad, subdivision,
                          detector_name, result, alarms, parse_source, raw_text)
                          VALUES (:cid, 'far_road', now() - interval '3 minutes', 'CSX', 'Test Sub', 'detector',
                          'no_defects', '[]', 'rules', 'no defects')"""), {"cid": cid})
    a = _get(client)
    assert a["detector_readouts"] == [] and a["verdicts"]["rail_voice"]["state"] == "quiet"
