"""Receiver heartbeats, coverage and coverage_summary (core/receivers.py). No database needed."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from core import config_files
from core.config_files import Channel, RailLine, Subdivision, load_channels, load_subdivisions, norm_line_name
from core.receivers import (ReceiverView, coverage_for, coverage_summary, on_subdivision, parse_airband_stats,
                            read_heartbeat, state_from)
from core.settings import get_settings, reset_settings_cache

NOW = datetime.now(timezone.utc)

# what rtl_airband v7 writes to stats_filepath (src/output.cpp write_stats_file), trimmed
STATS = """# HELP channel_activity_counter Loops of output_thread with frequency active.
# TYPE channel_activity_counter counter
channel_activity_counter{freq="160.215",label="csx_road"}\t12
channel_activity_counter{freq="161.250",label="csx_disp"}\t0

# HELP channel_noise_level Raw squelch noise_level.
# TYPE channel_noise_level gauge
channel_noise_level{freq="160.215",label="csx_road"}\t0.012
channel_noise_level{freq="161.250",label="csx_disp"}\t0.010
channel_noise_level{freq="160.500"}\t0.010
"""


@pytest.fixture()
def status_dir(tmp_path, monkeypatch):
    d = tmp_path / "receivers"
    d.mkdir()
    monkeypatch.setenv("RECEIVER_STATUS_DIR", str(d))
    monkeypatch.setenv("TRAINTAP_CSV", str(tmp_path / "traintap" / "trains.csv"))
    reset_settings_cache()
    yield d
    reset_settings_cache()


def _touch(path, age_s: float, text: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    t = time.time() - age_s
    os.utime(path, (t, t))


def test_parse_airband_stats_lists_demodulated_channels():
    labels, freqs = parse_airband_stats(STATS)
    assert labels == {"csx_road", "csx_disp"}
    assert freqs == (160.215, 160.5, 161.25)
    assert parse_airband_stats("") == (frozenset(), ())


def test_state_thresholds():
    assert state_from(None, NOW, 300) == "not_installed"
    assert state_from(NOW - timedelta(seconds=299), NOW, 300) == "online"
    assert state_from(NOW - timedelta(seconds=301), NOW, 300) == "offline"


def test_no_heartbeat_and_no_output_is_not_installed(status_dir):
    rv = ReceiverView()
    ch = Channel(id="csx_road", label="road", kind="rail_road", freq_mhz=160.215)
    assert rv.status(ch) == {"state": "not_installed", "device": "rail-airband: RTL-SDR serial RAIL160 (rtl_airband)"}
    assert rv.status(Channel(id="pd", label="pd", kind="police")) == {"state": "not_installed"}   # no receiver assigned


def test_fresh_airband_stats_means_online_even_with_no_transmissions(status_dir):
    _touch(status_dir / "rail-airband.prom", 20, STATS)
    rv = ReceiverView()
    road = rv.status(Channel(id="csx_road", label="road", kind="rail_road"))
    assert road["state"] == "online" and road["last_seen_at"].endswith("Z")
    # not in rtl_airband's channel list (by label or frequency): this receiver isn't listening to it
    other = Channel(id="csx_yard", label="yard", kind="rail_yard", freq_mhz=160.9)
    assert rv.status(other)["state"] == "not_installed"
    # matched by frequency when the label differs (an unlabelled or renamed channel)
    assert rv.status(Channel(id="x", label="x", kind="rail_voice", freq_mhz=160.5001))["state"] == "online"


def test_stale_heartbeat_is_offline_and_recent_output_wins(status_dir):
    _touch(status_dir / "rail-airband.prom", 3600, STATS)
    rv = ReceiverView()
    ch = Channel(id="csx_road", label="road", kind="rail_road")
    assert rv.status(ch)["state"] == "offline"
    assert rv.status(ch, NOW - timedelta(seconds=30))["state"] == "online"        # a recording 30 s ago


def test_unparseable_stats_file_still_counts_for_every_channel(status_dir):
    _touch(status_dir / "rail-airband.prom", 5, "")             # read while rtl_airband was rewriting it
    assert ReceiverView().status(Channel(id="any", label="a", kind="rail_road"))["state"] == "online"


def test_traintap_heartbeat_new_location_and_csv_fallback(status_dir, tmp_path):
    eot = Channel(id="eot_hot", label="EOT", kind="eot")
    _touch(tmp_path / "traintap" / "heartbeat", 400)             # traintap's default, next to the CSV
    assert ReceiverView().status(eot)["state"] == "offline"
    _touch(status_dir / "traintap-eot.heartbeat", 10)            # the container stack's --heartbeat
    hb = read_heartbeat("traintap-eot", get_settings())
    assert hb.path.name == "traintap-eot.heartbeat"
    rv = ReceiverView()
    assert rv.status(eot)["state"] == "online" and 0 <= rv.heartbeat_age_s("traintap-eot") < 60


def test_explicit_receiver_name_and_device(cfg_dir, status_dir):
    (cfg_dir / "channels.local.yaml").write_text(yaml.safe_dump({
        "receivers": {"ps-airband": {"device": "ps-airband: RTL-SDR serial PS460"}},
        "channels": [{"id": "fire", "label": "Fire", "kind": "fire", "receiver": "ps-airband"}]}))
    config_files.clear_cache()
    ch = load_channels()[0]
    assert ch.receiver_name == "ps-airband"
    _touch(status_dir / "ps-airband.heartbeat", 5)
    st = ReceiverView().status(ch)
    assert st["state"] == "online" and st["device"] == "ps-airband: RTL-SDR serial PS460" and st["last_seen_at"]
    (cfg_dir / "channels.local.yaml").write_text(yaml.safe_dump({"channels": [{"id": "x", "receiver": "../etc"}]}))
    config_files.clear_cache()
    with pytest.raises(ValueError):
        load_channels()


def test_example_feeds_flagged_and_excluded_from_summary():
    ex = Channel(id="example_rail_road", label="ex", kind="rail_road")
    flagged = Channel(id="demo", label="demo", kind="fire", example=True)
    assert ex.is_example and flagged.is_example
    assert not Channel(id="eot_hot", label="EOT", kind="eot").is_example
    real = Channel(id="csx", label="csx", kind="rail_voice")
    off = Channel(id="eot2", label="eot", kind="eot", enabled=False)
    assert coverage_summary([(ex, "online"), (flagged, "online")]) == {
        "rail_radio": "not_configured", "eot": "not_configured", "public_safety": "not_configured"}
    assert coverage_summary([(ex, "online"), (real, "not_installed"), (off, "online")]) == {
        "rail_radio": "no_receiver", "eot": "not_configured", "public_safety": "not_configured"}
    two = Channel(id="csx2", label="csx2", kind="rail_dispatcher")
    assert coverage_summary([(real, "offline"), (two, "not_installed")])["rail_radio"] == "receiver_offline"
    assert coverage_summary([(real, "offline"), (two, "online")])["rail_radio"] == "listening"


def test_committed_config_is_examples_plus_the_generic_eot_feed():
    chans = {c.id: c for c in load_channels()}
    assert {cid for cid, c in chans.items() if not c.is_example} == {"eot_hot"}
    summary = coverage_summary([(c, "not_installed") for c in chans.values()])
    assert summary == {"rail_radio": "not_configured", "eot": "no_receiver", "public_safety": "not_configured"}


def test_line_names_match_across_datasets():
    assert norm_line_name("EXAMPLE (CSXT)") == norm_line_name("Example Sub") == "example"
    assert norm_line_name("Third Subdivision") == "third"
    sd = Subdivision(railroad="CSX", name="Test Sub", aliases=["OTHERNAME (CSXT)"])
    assert sd.matches("othername") and sd.matches("Test") and sd.matches("OTHERNAME (CSXT)")
    assert not sd.matches("sub") and not sd.matches("") and not sd.matches("Holly")
    ch = Channel(id="c", label="c", kind="rail_voice", railroad="CSX",
                 coverage_lines=[RailLine("CSX", "Third Sub", ["OTHERNAME"])])
    assert on_subdivision(ch, sd)                                     # via the line alias
    assert not on_subdivision(Channel(id="d", label="d", kind="rail_voice", railroad="CN",
                                      coverage_lines=[RailLine("CN", "OTHERNAME", [])]), sd)  # other railroad


def test_channel_hint_is_exact_and_coverage_needs_a_place():
    """A hint names a channel's line only exactly (normalised), and a channel covers a point only
    through a place on record: its bias point or area with a radius, or a nearby subdivision."""
    from core.config_files import Area
    from core.receivers import channel_covers_point, channel_matches_hint
    ch = Channel(id="c", label="c", kind="rail_voice", railroad="CSX", subdivision="Test Sub",
                 coverage_lines=[RailLine("CSX", "Test Sub", ["OTHERNAME (CSXT)"])])
    assert channel_matches_hint(ch, "test") and channel_matches_hint(ch, "othername")
    assert channel_matches_hint(ch, "Test Subdivision", railroad="csx")
    assert not channel_matches_hint(ch, "est") and not channel_matches_hint(ch, "other")       # substrings
    assert not channel_matches_hint(ch, "Test Sub", railroad="NS")
    no_rr = Channel(id="n", label="n", kind="rail_voice", subdivision="Test Sub")
    assert not channel_matches_hint(no_rr, "Test Sub", railroad="CSX")                       # unknown isn't CSX
    sd = Subdivision(railroad="CSX", name="Test Sub", center=(42.25, -83.70), radius_km=8)
    assert channel_covers_point(ch, 42.26, -83.71, [sd], []) and not channel_covers_point(ch, 42.9, -83.0, [sd], [])
    assert not channel_covers_point(ch, 42.26, -83.71, [], [])                                # no place on record
    biased = Channel(id="b", label="b", kind="rail_voice", bias_lat=42.9, bias_lon=-83.0, coverage_radius_km=10)
    assert channel_covers_point(biased, 42.9, -83.0, [], []) and not channel_covers_point(biased, 42.26, -83.71, [], [])
    bias_only = Channel(id="x", label="x", kind="rail_voice", bias_lat=42.9, bias_lon=-83.0)
    assert not channel_covers_point(bias_only, 42.9, -83.0, [], [])                           # no radius: no reach
    area = Area(id="a1", name="Test area", lat=42.9, lon=-83.0, radius_km=10)
    in_area = Channel(id="a", label="a", kind="rail_voice", area_id="a1")
    assert channel_covers_point(in_area, 42.9, -83.0, [], [area])
    assert not channel_covers_point(in_area, 42.26, -83.71, [], [area])


def test_coverage_object_from_config_and_derived(cfg_dir):
    (cfg_dir / "channels.local.yaml").write_text(yaml.safe_dump({"channels": [
        {"id": "road", "label": "road", "kind": "rail_voice", "railroad": "CSX", "subdivision": "Test Sub",
         "coverage": {"area": "Test area", "radius_km": 4,
                      "lines": [{"name": "Test Sub", "aliases": ["TESTNAME (CSXT)"]}]}},
        {"id": "derived", "label": "d", "kind": "rail_road", "railroad": "CSX", "subdivision": "Test Sub"},
        {"id": "fire", "label": "f", "kind": "fire", "area_id": "ann_arbor"},
    ]}))
    (cfg_dir / "rail_channels.local.yaml").write_text(yaml.safe_dump({"railroads": {"CSX": {"subdivisions": {
        "Test Sub": {"aliases": ["TESTNAME"], "center": {"lat": 42.2, "lon": -83.6}, "radius_km": 5}}}}}))
    config_files.clear_cache()
    chans = {c.id: c for c in load_channels()}
    subs, areas = load_subdivisions(), config_files.load_areas()
    assert subs[0].aliases == ["TESTNAME"]
    assert coverage_for(chans["road"], subs, areas) == {
        "area": "Test area", "radius_km": 4.0,
        "lines": [{"railroad": "CSX", "name": "Test Sub", "aliases": ["TESTNAME (CSXT)"]}]}   # railroad inherited
    assert coverage_for(chans["derived"], subs, areas) == {
        "lines": [{"railroad": "CSX", "name": "Test Sub", "aliases": ["TESTNAME"]}]}           # from the rail plan
    fire = coverage_for(chans["fire"], subs, areas)
    assert "lines" not in fire and fire.get("area") and fire.get("radius_km")                 # from areas.yaml


def test_a_label_on_another_frequency_is_not_this_channel(status_dir):
    """rtl_airband's stats file still names the channel, but on another frequency than the one
    configured (channels.local.yaml changed, rail.local.conf not re-rendered): the receiver is
    not listening to it. A channel it doesn't label still matches by frequency."""
    from core.receivers import pairs_cover, parse_airband_pairs, reported_freqs
    stale = ('channel_activity_counter{freq="161.100",label="csx_a_road"}\t0\n'
             'channel_activity_counter{freq="161.130",label="csx_a_disp"}\t0\n'
             'channel_activity_counter{freq="160.500"}\t0\n')
    _touch(status_dir / "rail-airband.prom", 5, stale)
    rv = ReceiverView()
    road = Channel(id="csx_a_road", label="road", kind="rail_voice", freq_mhz=160.215)
    assert rv.status(road)["state"] == "not_installed"                  # used to be "online" by label alone
    assert reported_freqs(rv.heartbeat("rail-airband").pairs, road) == [161.1]
    assert rv.status(Channel(id="csx_a_road", label="road", kind="rail_voice", freq_mhz=161.1015))["state"] == "online"
    assert rv.status(Channel(id="csx_a_road", label="road", kind="rail_voice"))["state"] == "online"   # no freq set
    assert rv.status(Channel(id="new", label="n", kind="rail_voice", freq_mhz=160.5))["state"] == "online"
    pairs = parse_airband_pairs(stale)
    assert pairs == ((None, 160.5), ("csx_a_road", 161.1), ("csx_a_disp", 161.13))      # by frequency
    # a detector on the road channel's frequency: the receiver hears it through the road channel
    assert pairs_cover(pairs, Channel(id="det", label="d", kind="rail_detector", freq_mhz=161.1))
    assert not pairs_cover(pairs, Channel(id="csx_a_disp", label="d", kind="rail_voice", freq_mhz=161.1))
    assert reported_freqs(pairs, Channel(id="other", label="o", kind="rail_voice", freq_mhz=1.0)) == []


def test_a_frequency_is_heard_as_the_channel_its_recordings_are_filed_under():
    """A stats line vouches for a channel only through the channel its recordings are filed under
    (sdr_files): its own label, or a shared frequency demodulated once under another enabled
    channel. A label that is a disabled channel (renamed, the old entry kept disabled, rail.local.conf
    not re-rendered) or a channel configured on another frequency means the receiver isn't
    listening to the channel on that frequency (it used to match by frequency alone)."""
    from core.receivers import heard_via, pairs_cover, pairs_problem, parse_airband_pairs
    ch = {c.id: c for c in (
        Channel(id="t_road", label="r", kind="rail_road", freq_mhz=160.98),
        Channel(id="t_road_old", label="o", kind="rail_road", freq_mhz=160.98, enabled=False),
        Channel(id="t_disp", label="d", kind="rail_dispatcher", freq_mhz=161.07),
        Channel(id="t_det", label="x", kind="rail_detector", freq_mhz=161.07),
        Channel(id="moved", label="m", kind="rail_road", freq_mhz=160.5),
        Channel(id="t_yard", label="y", kind="rail_yard", freq_mhz=160.62, templates=["yard_tmpl"]),
    )}
    stale = parse_airband_pairs('channel_activity_counter{freq="160.980",label="t_road_old"}\t0\n'
                                'channel_activity_counter{freq="161.070",label="t_disp"}\t0\n'
                                'channel_activity_counter{freq="161.900",label="moved"}\t0\n')
    assert heard_via(stale, ch["t_road"], ch) == [] and not pairs_cover(stale, ch["t_road"], ch)
    assert pairs_problem(stale, ch["t_road"], ch) == (
        "the receiver files 160.98 MHz under t_road_old, a disabled channel, so those recordings are dropped "
        "(re-render rail.local.conf)")
    assert heard_via(stale, ch["t_disp"], ch) == ["t_disp"] and pairs_problem(stale, ch["t_disp"], ch) is None
    assert heard_via(stale, ch["t_det"], ch) == ["t_disp"]          # a detector on the dispatcher frequency
    assert heard_via(stale, ch["moved"], ch) == []                   # its own label, on another frequency
    # a label that is no channel id resolves like sdr_files' files: by template, then frequency
    other = parse_airband_pairs('channel_activity_counter{freq="160.980",label="gone"}\t0\n'
                                'channel_activity_counter{freq="160.620",label="yard_tmpl"}\t0\n'
                                'channel_activity_counter{freq="161.070"}\t0\n')
    assert heard_via(other, ch["t_road"], ch) == ["t_road"]          # first on 160.98 is t_road (enabled)
    assert heard_via(other, ch["t_yard"], ch) == ["t_yard"]
    assert heard_via(other, ch["t_disp"], ch) == ["t_disp"] and heard_via(other, ch["t_det"], ch) == ["t_disp"]
    first_disabled = {"t_road_old": ch["t_road_old"], **ch}          # the disabled one listed first
    assert heard_via(other, ch["t_road"], first_disabled) == []
    moved = parse_airband_pairs('channel_activity_counter{freq="160.980",label="moved"}\t0\n')
    assert heard_via(moved, ch["t_road"], ch) == []
    assert pairs_problem(moved, ch["t_road"], ch) == (
        "the receiver demodulates 160.98 MHz as moved, which is configured on 160.5 MHz (re-render rail.local.conf)")


def test_coverage_is_the_channels_own_area_first():
    """A channel's own area (bias or area with a radius) decides and a subdivision never stretches
    it; with only a radius it reaches that far from a subdivision it is on; an eot channel is
    the same (no fixed 25 km), and a railroad hint keeps that railroad's channels."""
    from core.config_files import Area
    from core.receivers import channel_covers_point, on_railroad, own_places
    sd = Subdivision(railroad="CSX", name="Test Sub", center=(42.25, -83.70), radius_km=8)
    four_km = (42.25 + 0.036, -83.70)
    short = Channel(id="s", label="s", kind="rail_road", railroad="CSX", subdivision="Test Sub",
                    bias_lat=42.25, bias_lon=-83.70, coverage_radius_km=2)
    assert own_places(short, []) == [(42.25, -83.70, 2)]
    assert not channel_covers_point(short, *four_km, [sd], [])           # on the sub, but it hears 2 km
    assert channel_covers_point(short, 42.255, -83.70, [sd], [])
    capped = Channel(id="c", label="c", kind="rail_road", railroad="CSX", subdivision="Test Sub", coverage_radius_km=3)
    assert own_places(capped, []) == [] and not channel_covers_point(capped, *four_km, [sd], [])
    assert channel_covers_point(capped, 42.27, -83.70, [sd], [])
    plain = Channel(id="p", label="p", kind="rail_road", railroad="CSX", subdivision="Test Sub")
    assert channel_covers_point(plain, *four_km, [sd], []) and not channel_covers_point(plain, 42.35, -83.70, [sd], [])
    eot = Channel(id="eot_hot", label="EOT", kind="eot", bias_lat=42.25, bias_lon=-83.70, coverage_radius_km=3)
    assert channel_covers_point(eot, 42.27, -83.70, [sd], [])
    assert not channel_covers_point(eot, 42.34, -83.70, [sd], [])      # 10 km
    assert not channel_covers_point(eot, 42.43, -83.70, [sd], [])      # 20 km
    assert not channel_covers_point(Channel(id="e2", label="e", kind="eot", bias_lat=42.25, bias_lon=-83.70),
                                    42.25, -83.70, [sd], [])               # a bias with no radius reaches nothing
    area = Area(id="a1", name="A", lat=42.25, lon=-83.70, radius_km=4)
    in_area = Channel(id="ea", label="e", kind="eot", area_id="a1")
    assert channel_covers_point(in_area, 42.27, -83.70, [], [area])
    assert not channel_covers_point(in_area, *four_km, [], [area])
    assert on_railroad(plain, "csx", [sd]) and not on_railroad(plain, "NS", [sd]) and on_railroad(plain, None, [])
    linked = Channel(id="l", label="l", kind="rail_road")                   # linked only from the rail plan
    sd.channels.append(config_files.RailChannel("CSX", "Test Sub", 160.2, None, "road", False, None, "l"))
    assert on_railroad(linked, "CSX", [sd]) and not on_railroad(linked, "CN", [sd])
