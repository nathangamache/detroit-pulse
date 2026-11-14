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
