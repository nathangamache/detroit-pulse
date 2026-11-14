"""Rail: defect-detector parser, EOT interpretation, reason rules, FCC ULS import, channel plan."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from extract.rules import rail_reason
from rail.detector import looks_like_detector, parse
from rail.eot import EotReading, interpret
from rail.numbers import parse_number

# Synthetic readouts in the formats real detector voices use (digits, spelled digits, hyphens,
# "point", repeats, alarm then axle/side, integrity failures).
DETECTOR_CASES = [
    ("CSX equipment detector, milepost 123.4, track 1. No defects. No defects. Total axles 412. "
     "Train speed 47 miles per hour. Temperature 38 degrees. Detector out.",
     dict(railroad="CSX", milepost=123.4, track="1", result="no_defects", axle_count=412, speed_mph=47,
          temperature_f=38, alarms=[])),
    ("NS detector mile post two eight five point three, south track, hot box, axle 46, left side. "
     "Repeat, hot box axle 46 left side. Speed 42. Axle count 312.",
     dict(railroad="NS", milepost=285.3, track="south", result="alarm", axle_count=312, speed_mph=42,
          alarms=[("hot_box", 46, "left")])),
    ("CN defect detector, mile 21.4, northbound, dragging equipment, dragging equipment, axle 88. "
     "Total axle count 220, temperature minus 5 degrees, detector out",
     dict(railroad="CN", milepost=21.4, direction="northbound", result="alarm", axle_count=220,
          temperature_f=-5, alarms=[("dragging_equipment", 88, None)])),
    ("Equipment detector milepost 61.3, integrity failure, integrity failure.",
     dict(milepost=61.3, result="integrity_failure", integrity_failure=True, alarms=[])),
    ("Conrail detector milepost 1-2-point-7, main track 2, hot wheel axle 1 0 4 right side, high and wide load, "
     "total axles 96",
     dict(railroad="CSAO", milepost=12.7, track="2", result="alarm", axle_count=96,
          alarms=[("hot_wheel", 104, "right"), ("high_wide", None, None)])),
    ("Amtrak detector mile post one hundred two point five, no defects, train speed seventy nine, axles 40",
     dict(railroad="AMTK", milepost=102.5, result="no_defects", speed_mph=79, axle_count=40)),
    ("detector milepost forty seven point one, track two, no defects found, too slow for temperature, "
     "detector malfunction",
     dict(milepost=47.1, track="2", result="integrity_failure", integrity_failure=True)),
]


@pytest.mark.parametrize("text,expect", DETECTOR_CASES)
def test_detector_parser(text, expect):
    assert looks_like_detector(text)
    r = parse(text)
    for k, v in expect.items():
        if k == "alarms":
            got = [(a.type, a.axle, a.side) for a in r.alarms]
            for want in v:
                assert want in got, (got, want)
            if not v:
                assert got == []
        else:
            assert getattr(r, k) == v, (k, getattr(r, k), v)
    assert r.complete


def test_not_a_detector():
    for t in ["Engine 5 respond to Packard and State", "Q326 holding at the east end for the westbound",
              "Medic 9 on scene, 2 patients"]:
        assert not looks_like_detector(t)
        assert not parse(t).complete


@pytest.mark.parametrize("s,v", [("123.4", 123.4), ("1 2 3 point 4", 123.4), ("1-2-3-point-4", 123.4),
                                 ("one two three point four", 123.4), ("one hundred twenty three point four", 123.4),
                                 ("forty-seven", 47.0), ("twelve", 12.0), ("oh nine", 9.0)])
def test_spoken_numbers(s, v):
    assert parse_number(s) == v


T0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def _r(psig, moving, dt=0, source="EOT", unit=45678):
    return EotReading(ts=T0 + timedelta(seconds=dt), unit_id=unit, brake_pipe_psig=psig, motion=moving, source=source)


def test_eot_interpretation():
    assert interpret(_r(90, True)).status == "moving"
    assert interpret(_r(70, True)).status == "moving_braking"
    assert interpret(_r(88, False)).status == "stopped_released"
    assert interpret(_r(64, False)).status == "stopped_service"          # normal stop, full service
    assert interpret(_r(30, False)).status == "stopped_heavy"
    s = interpret(_r(3, True))
    assert s.status == "emergency_moving" and s.emergency
    s = interpret(_r(2, False, dt=60), previous=_r(88, True))
    assert s.status == "emergency_stopped" and s.emergency
    # 0 psig without a recent charged reading is an uncharged EOT, not an emergency
    assert interpret(_r(0, False)).status == "no_air"
    assert not interpret(_r(0, False, dt=3600), previous=_r(90, True)).emergency   # old history doesn't count
    assert interpret(_r(None, False)).status == "unknown"
    assert interpret(_r(None, None, source="HOT")).status == "hot"


@pytest.mark.parametrize("text,label", [
    ("Q326 you'll be holding at the east end for the westbound", "meet_or_hold"),
    ("dispatcher, we've got a dark signal at CP Wayne, restricting", "signal_problem"),
    ("we're going to have a crew change at the yard, we're about to outlaw on hours of service", "crew_change_hos"),
    ("we have a broken knuckle on the 40th car, going to walk it", "mechanical"),
    ("car on the crossing at Main Street, we struck a vehicle at the crossing", "crossing_incident"),
    ("there's some kids on the bridge, trespassers on the tracks", "trespasser"),
    ("shove 10 car lengths and couple to the cut in track 3", "switching_yard"),
    ("CSX detector milepost 12 no defects", "defect_detector"),
    ("good morning dispatcher", "unknown"),
])
def test_rail_reason_rules(text, label):
    assert rail_reason(text) == label


def _uls(tmp_path):
    d = tmp_path / "uls"
    d.mkdir()

    def w(name, rows):
        (d / name).write_text("\n".join("|".join(r) for r in rows) + "\n", encoding="latin-1")

    def pad(r, n):
        return r + [""] * (n - len(r))

    w("HD.dat", [pad(["HD", "1001", "", "", "WQAA001", "A", "IG"], 10), pad(["HD", "1002", "", "", "WQAA002", "A", "IG"], 10),
                 pad(["HD", "1003", "", "", "WQAA003", "E", "IG"], 10)])
    w("EN.dat", [pad(["EN", "1001", "", "", "", "L", "", "CSX TRANSPORTATION, INC."], 10),
                 pad(["EN", "1002", "", "", "", "L", "", "ACME CONCRETE LLC"], 10),
                 pad(["EN", "1003", "", "", "", "L", "", "NORFOLK SOUTHERN RAILWAY"], 10)])
    lo = pad(["LO", "1001", "", "", "", "", "F", "", "1"], 27)
    lo[12], lo[13], lo[14] = "YPSILANTI", "WASHTENAW", "MI"
    lo[19:27] = ["42", "14", "24", "N", "83", "36", "36", "W"]
    w("LO.dat", [lo])
    fr = lambda usi, f, cls: pad(["FR", usi, "", "", "", "", "1", "1", cls, "", f], 17)  # noqa: E731
    w("FR.dat", [fr("1001", "160.2150", "FB2"), fr("1001", "160.2225", "FB"), fr("1001", "452.9375", "FB"),
                 fr("1002", "160.2150", "FB")])
    return d


def test_fcc_uls_import(tmp_path):
    from scripts.import_fcc_uls import aar_channel, build_plan
    assert aar_channel(160.215) == "07" and aar_channel(161.565) == "97" and aar_channel(160.2225) == "07.5"
    assert aar_channel(452.9375) is None
    plan = build_plan(_uls(tmp_path), (42.02, -84.15, 42.90, -82.85), "MI")
    assert list(plan["railroads"]) == ["CSX"]                              # concrete co. and expired NS ignored
    sub = plan["railroads"]["CSX"]["subdivisions"]["Washtenaw County (ULS)"]
    chans = {(c["freq_mhz"], c["aar_channel"]) for c in sub["channels"]}
    assert chans == {(160.215, "07"), (160.2225, "07.5")}                  # UHF EOT freq outside the pool
    assert all(c["verified"] is False and c["source"].startswith("FCC ULS WQAA001") for c in sub["channels"])
    assert sub["center"] == {"lat": 42.24, "lon": -83.61}


def test_rail_channel_plan_loader(cfg_dir):
    from core.config_files import clear_cache, load_subdivisions
    (cfg_dir / "rail_channels.local.yaml").write_text(yaml.safe_dump({"railroads": {"CSX": {"subdivisions": {
        "Test Sub": {"center": {"lat": 42.28, "lon": -83.74}, "radius_km": 5,
                     "channels": [{"freq_mhz": 160.215, "aar_channel": "07", "use": "road", "verified": False,
                                   "source": "test", "channel_id": "test_rail"}]}}}}}))
    clear_cache()
    subs = load_subdivisions()
    assert len(subs) == 1 and subs[0].near(42.281, -83.741) and not subs[0].near(42.5, -83.0)
    assert subs[0].channels[0].channel_id == "test_rail" and subs[0].channels[0].use == "road"
