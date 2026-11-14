"""API: the gateway contract (/v1), UI routes, bearer auth, admin lockdown. Runs against detroitpulse_test."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import text

CHANNELS = {"channels": [
    {"id": "aa_fire", "label": "Test fire dispatch", "kind": "fire", "county": "Washtenaw", "city": "Ann Arbor"},
    {"id": "csx_road", "label": "CSX Test Sub road", "kind": "rail_road", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.215},
    {"id": "eot_hot", "label": "EOT/HOT telemetry", "kind": "eot", "freq_mhz": 457.9375,
     "bias": {"lat": 42.25, "lon": -83.70}, "coverage": {"radius_km": 10}},
]}
RAIL = {"railroads": {"CSX": {"subdivisions": {"Test Sub": {
    "center": {"lat": 42.25, "lon": -83.70}, "radius_km": 8,
    "channels": [{"freq_mhz": 160.215, "aar_channel": "07", "use": "road", "verified": False, "source": "test",
                  "channel_id": "csx_road"}]}}}}}
NOW = datetime.now(timezone.utc)
INC = str(uuid.uuid4())
FAR = str(uuid.uuid4())


@pytest.fixture()
def seeded(db, cfg_dir):
    (cfg_dir / "channels.local.yaml").write_text(yaml.safe_dump(CHANNELS))
    (cfg_dir / "rail_channels.local.yaml").write_text(yaml.safe_dump(RAIL))
    with db.begin() as c:
        for iid, lat, lng, mins in ((INC, 42.2650, -83.7310, 5), (FAR, 42.4500, -83.1000, 5)):
            c.execute(text("""INSERT INTO incidents (incident_id, feed_id, channel_id, county, status, opened_at,
                              last_updated, incident_type, priority, address_full, lat, lng, location, units,
                              geocode_confidence, short_summary)
                              VALUES (:id, 'aa_fire', 'aa_fire', 'Washtenaw', 'ACTIVE', :t, :t, 'VEHICLE_FIRE', 'HIGH',
                              'Packard St & Stadium Blvd', :lat, :lng, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
                              ARRAY['AAFD:Engine 4'], 'HIGH', 'Car fully involved')"""),
                      {"id": iid, "t": NOW - timedelta(minutes=mins), "lat": lat, "lng": lng})
        rows = [
            ("aa_fire", "Engine 4 respond to Packard and Stadium, vehicle fire", INC, None, None, 3),
            ("aa_fire", "Engine 4 on scene, car fully involved", INC, None, None, 2),
            ("csx_road", "Q326 you'll be holding at the east end for the westbound", None, "meet_or_hold", "claude", 4),
            ("csx_road", "dispatcher, dark signal at the control point, we're stopped", None, "signal_problem", "claude", 3),
            ("csx_road", "CSX equipment detector milepost 123.4 track 1 hot box axle 46 total axles 412", None,
             "defect_detector", "prefilter", 2),
        ]
        for i, (ch, tx, iid, reason, src, mins) in enumerate(rows):
            cid = str(uuid.uuid4())
            c.execute(text("""INSERT INTO transcript_chunks (chunk_id, feed_id, channel_id, "timestamp", captured_at,
                              status, raw_transcript, incident_id, rail, extract_source, priority, source, source_key)
                              VALUES (:cid, :ch, :ch, :t, :t, 'done', :tx, :iid, CAST(:rail AS jsonb), :src, 'LOW',
                              'sdr_files', :key)"""),
                      {"cid": cid, "ch": ch, "t": NOW - timedelta(minutes=mins), "tx": tx, "iid": iid,
                       "rail": json.dumps({"reason_label": reason}) if reason else None, "src": src,
                       "key": f"k{i}"})
            if reason == "defect_detector":
                c.execute(text("""INSERT INTO rail_detector_events (chunk_id, channel_id, ts, railroad, subdivision,
                                  detector_name, milepost, track, axle_count, result, alarms, parse_source, raw_text)
                                  VALUES (:cid, 'csx_road', :t, 'CSX', 'Test Sub', 'csx equipment detector', 123.4, '1',
                                  412, 'alarm', CAST(:al AS jsonb), 'rules', :tx)"""),
                          {"cid": cid, "t": NOW - timedelta(minutes=mins), "tx": tx,
                           "al": json.dumps([{"type": "hot_box", "axle": 46, "side": None}])})
        c.execute(text("""INSERT INTO transcript_chunks (chunk_id, feed_id, channel_id, status, created_at, source_key)
                          VALUES (gen_random_uuid(), 'aa_fire', 'aa_fire', 'queued', now() - interval '30 seconds', 'q1')"""))
        c.execute(text("""INSERT INTO rail_telemetry (ts, channel, source, unit_id, brake_pipe_psig, motion, marker_light,
                          emergency, status, batt_cond, arm_status, dedupe_key)
                          VALUES (:t, 'eot_hot', 'EOT', 45678, 2, false, true, true, 'emergency_stopped', 'OK', 'Normal', 'd1')"""),
                  {"t": NOW - timedelta(minutes=1)})
    from api.main import create_app
    return TestClient(create_app())


def _err(r, status, code):
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


def test_health_and_generated_at(seeded):
    r = seeded.get("/v1/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["generated_at"].endswith("Z")


def test_incidents_near(seeded):
    r = seeded.get("/v1/incidents/near", params={"lat": 42.2808, "lon": -83.7430, "radius_km": 5})
    body = r.json()
    assert r.status_code == 200 and body["radius_km"] == 5 and body["since_minutes"] == 60
    assert [i["incident_id"] for i in body["incidents"]] == [INC]           # the far one is excluded
    i = body["incidents"][0]
    assert i["type"] == "vehicle_fire" and i["priority"] == "high" and i["status"] == "active"
    assert i["location_confidence"] == "high" and 1500 < i["distance_m"] < 2500 and i["chunk_count"] == 2
    assert i["summary_untrusted"] == "Car fully involved" and i["feed_id"] == "aa_fire" and i["units"] == ["AAFD:Engine 4"]
    assert seeded.get("/v1/incidents/near", params={"lat": 42.2808, "lon": -83.7430, "since_minutes": 1}).json()["incidents"] == []
    _err(seeded.get("/v1/incidents/near", params={"lat": 42.28, "lon": -83.74, "radius_km": 80}), 400, "bad_request")
    _err(seeded.get("/v1/incidents/near", params={"lat": 42.28}), 400, "bad_request")


def test_incident_detail(seeded):
    r = seeded.get(f"/v1/incidents/{INC}").json()
    assert r["incident"]["incident_id"] == INC
    assert [t["text_untrusted"][:8] for t in r["transcripts"]] == ["Engine 4", "Engine 4"]      # chronological
    assert r["transcripts"][0]["channel"] == "Test fire dispatch" and r["transcripts"][0]["incident_id"] == INC
    _err(seeded.get(f"/v1/incidents/{uuid.uuid4()}"), 404, "not_found")
    _err(seeded.get("/v1/incidents/not-a-uuid"), 404, "not_found")


def test_search_transcripts(seeded):
    r = seeded.get("/v1/transcripts/search", params={"q": "fully involved"}).json()
    assert r["query"] == "fully involved" and len(r["results"]) == 1
    r2 = seeded.get("/v1/transcripts/search", params={"q": "Q326", "channel": "csx_road"}).json()
    assert len(r2["results"]) == 1 and r2["results"][0]["feed_id"] == "csx_road"
    assert seeded.get("/v1/transcripts/search", params={"q": "Q326", "channel": "aa_fire"}).json()["results"] == []
    assert seeded.get("/v1/transcripts/search", params={"q": "100%_"}).json()["results"] == []   # LIKE escaping
    _err(seeded.get("/v1/transcripts/search", params={"q": ""}), 400, "bad_request")


def test_rail_activity(seeded):
    r = seeded.get("/v1/rail/activity", params={"lat": 42.26, "lon": -83.71}).json()
    assert r["subdivision"] == "Test Sub" and r["railroad"] == "CSX" and r["coverage"]["covered"] is True
    kinds = {c["kind"] for c in r["coverage"]["channels"]}
    assert kinds == {"road", "eot"}
    labels = [t.get("reason_label") for t in r["transcripts"]]
    assert labels == ["defect_detector_stop", "signal_stop", "meet_pass"]                         # newest first
    ro = r["detector_readouts"][0]
    assert ro["milepost"] == "123.4" and ro["defects"] == ["hot_box"] and ro["axles"] == 412 and ro["raw_untrusted"]
    e = r["eot_hot"][0]
    assert e["device"] == "EOT" and e["unit_id"] == "45678" and e["brake_pipe_psi"] == 2 and e["battery"] == "ok"
    assert e["armed"] is False and e["emergency"] is True
    rl = {x["label"]: x for x in r["reason_labels"]}
    assert {"emergency", "signal_stop", "meet_pass", "defect_detector_stop"} <= set(rl)
    assert all(0 <= x["confidence"] <= 1 for x in r["reason_labels"])
    assert {c["receiver_state"] for c in r["coverage"]["channels"]} == {"online"}   # recordings / packets just now
    v = r["verdicts"]
    assert v["rail_voice"]["state"] == "activity" and v["eot"]["state"] == "activity"
    assert v["rail_voice"]["counts"] == {"transcripts": 3, "detector_readouts": 1, "recordings_without_text": 0,
                                         "eot_packets": 0}
    assert v["eot"]["counts"]["eot_packets"] == 1
    assert v["rail_voice"]["channels"] == [{"feed_id": "csx_road", "receiver_state": "online", "covers_point": True}]
    # 75 km from the EOT receiver (10 km radius) and outside the line: nothing covers it
    far = seeded.get("/v1/rail/activity", params={"lat": 42.9, "lon": -83.0}).json()
    assert far["coverage"]["channels"] == [] and far["eot_hot"] == [] and far["coverage"]["covered"] is False
    assert "No monitored rail voice channel" in far["coverage"]["note"]
    assert "No EOT/HOT receiver covers this point (it is outside every eot channel's coverage radius)" in \
        far["coverage"]["note"]
    assert {k: x["state"] for k, x in far["verdicts"].items()} == {"rail_voice": "not_covered", "eot": "not_covered"}
    assert far["verdicts"]["eot"]["channels"] == [{"feed_id": "eot_hot", "receiver_state": "online",
                                                   "covers_point": False}]
    # naming the line doesn't bring its traffic to a point outside its area
    hinted = seeded.get("/v1/rail/activity", params={"lat": 42.9, "lon": -83.0, "subdivision": "test"}).json()
    assert hinted["coverage"]["channels"] == []
    assert hinted["transcripts"] == [] and hinted["detector_readouts"] == []
    assert ("Pulse monitors the CSX Test Sub line, but no receiver for it covers this point"
            in hinted["coverage"]["note"])
    _err(seeded.get("/v1/rail/activity", params={"lat": 42.26, "lon": -83.71, "since_minutes": 5000}), 400, "bad_request")


def test_feeds_status(seeded):
    r = seeded.get("/v1/feeds/status").json()
    feeds = {f["feed_id"]: f for f in r["feeds"]}
    assert feeds["aa_fire"]["state"] == "up" and feeds["aa_fire"]["category"] == "public_safety"
    assert feeds["aa_fire"]["source"] == "sdr:rtl_airband" and feeds["aa_fire"]["chunks_last_hour"] >= 2
    assert feeds["aa_fire"]["backlog"] == 1
    assert feeds["csx_road"]["category"] == "rail"
    assert feeds["eot_hot"]["source"] == "sdr:traintap" and feeds["eot_hot"]["state"] == "down"   # no heartbeat file
    assert r["backlog"]["queued"] == 1 and r["backlog"]["oldest_waiting_s"] >= 0
    # receiver: recordings / packets in the last minutes prove a receiver even without a heartbeat file
    assert feeds["aa_fire"]["receiver"]["state"] == "online" and "device" not in feeds["aa_fire"]["receiver"]
    assert feeds["csx_road"]["receiver"]["state"] == "online"
    assert feeds["csx_road"]["receiver"]["device"].startswith("rail-airband")
    assert feeds["eot_hot"]["receiver"]["state"] == "online" and feeds["eot_hot"]["receiver"]["last_seen_at"].endswith("Z")
    assert feeds["csx_road"]["coverage"] == {"lines": [{"railroad": "CSX", "name": "Test Sub", "aliases": []}]}
    assert feeds["aa_fire"]["coverage"] == {"area": "Ann Arbor"} and feeds["eot_hot"]["coverage"] == {"radius_km": 10.0}
    assert not any(f["example"] for f in r["feeds"])
    assert r["coverage_summary"] == {"rail_radio": "listening", "eot": "listening", "public_safety": "listening"}


QUIET = {"channels": [
    {"id": "csx_road", "label": "CSX Test Sub road", "kind": "rail_voice", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.215,
     "coverage": {"area": "Test area", "radius_km": 5,
                  "lines": [{"name": "Test Sub", "aliases": ["TESTNAME (CSXT)", "Other Sub"]}]}},
    {"id": "csx_yard", "label": "CSX Test yard", "kind": "rail_yard", "railroad": "CSX", "subdivision": "Test Sub",
     "freq_mhz": 160.95},
    {"id": "example_fire_dispatch", "label": "Example fire", "kind": "fire"},
    {"id": "eot_hot", "label": "EOT/HOT telemetry", "kind": "eot", "freq_mhz": 457.9375,
     "bias": {"lat": 42.25, "lon": -83.70}, "coverage": {"radius_km": 8}},
]}
QUIET_RAIL = {"railroads": {"CSX": {"subdivisions": {"Test Sub": {
    "aliases": ["TESTNAME"], "center": {"lat": 42.25, "lon": -83.70}, "radius_km": 8}}}}}


def _counters(freq: str, label: str, activity: int = 0, squelch: int = 0, dropped: int = 0) -> str:
    """One channel's lines in rtl_airband's stats file (v7.0.1 src/output.cpp): the label/frequency
    pair, and the squelch openings Pulse checks against the recordings it got (less those dropped
    as shorter than split_tx_drop_shorter_than)."""
    m = f'{{freq="{freq}",label="{label}"}}'
    return (f"channel_activity_counter{m}\t{activity}\n"
            f"channel_squelch_counter{m}\t{squelch}\n"
            f"channel_split_tx_dropped_counter{m}\t{dropped}\n")


ROAD_ONLY = _counters("160.215", "csx_road")
BOTH_TUNED = ROAD_ONLY + _counters("160.950", "csx_yard")
NEAR = {"lat": 42.26, "lon": -83.71}
BOOT = "test-boot-1"


def _mark(status, name: str, boot: str = BOOT, ago_s: float = 6 * 3600) -> None:
    """<name>.started, the start marker a receiver's entrypoint writes before each start (a new
    boot id every time), `ago_s` seconds ago."""
    import os
    import time
    p = status / f"{name}.started"
    p.write_text(f"{boot} 2026-11-14T00:00:00Z\n")
    t = time.time() - ago_s
    os.utime(p, (t, t))


def _beats(db, component: str, minutes: float, fingerprint: str = "", detail: dict | None = None,
           until_s: float = 5, every_s: float = 15) -> None:
    """Liveness history (core/liveness.py): a beat every `every_s` (INGEST_HEARTBEAT_S) from
    `minutes` ago to `until_s` ago. A receiver's beats carry the start marker BOOT (the one
    _mark writes) unless `detail` says otherwise."""
    from core import liveness
    if component.startswith("receiver:") and "started" not in (detail or {}):
        detail = {**(detail or {}), "started": BOOT}
    now = datetime.now(timezone.utc)
    t, end = now - timedelta(minutes=minutes), now - timedelta(seconds=until_s)
    with db.begin() as c:
        while t <= end:
            liveness.beat(c, component, t, fingerprint, detail)
            t += timedelta(seconds=every_s)


def _listening(db, status, prom: str | None = BOTH_TUNED, minutes: float = 40, eot: bool = True,
               ingest: bool = True) -> None:
    """Receivers up now (fresh heartbeat files) with `minutes` of recorded history, and the
    ingest stage (sdr_files, traintap) beating as long."""
    from core.liveness import pairs_fingerprint
    from core.receivers import parse_airband_pairs
    if prom is not None:
        (status / "rail-airband.prom").write_text(prom)
        pairs = parse_airband_pairs(prom)
        _beats(db, "receiver:rail-airband", minutes, pairs_fingerprint(pairs), {"pairs": [list(p) for p in pairs]})
    if eot:
        (status / "traintap-eot.heartbeat").write_text("")
        _beats(db, "receiver:traintap-eot", minutes)
    if ingest:
        _beats(db, "ingest:sdr_files", minutes)
        _beats(db, "ingest:traintap", minutes)


def _states(a: dict) -> dict:
    return {k: v["state"] for k, v in a["verdicts"].items()}


def _receiver_client(cfg_dir, tmp_path, monkeypatch, channels: dict):
    from core.settings import reset_settings_cache
    (cfg_dir / "channels.local.yaml").write_text(yaml.safe_dump(channels))
    (cfg_dir / "rail_channels.local.yaml").write_text(yaml.safe_dump(QUIET_RAIL))
    status = tmp_path / "receivers"
    status.mkdir()
    for name in ("rail-airband", "traintap-eot"):        # both started 6 h ago (deploy/docker-compose.yml)
        _mark(status, name)
    monkeypatch.setenv("RECEIVER_STATUS_DIR", str(status))
    monkeypatch.setenv("TRAINTAP_CSV", str(tmp_path / "traintap" / "trains.csv"))
    reset_settings_cache()
    from api.main import create_app
    return TestClient(create_app()), status


@pytest.fixture()
def quiet(db, cfg_dir, tmp_path, monkeypatch):
    """Configured rail channels, an empty database, and a receiver status dir the test fills."""
    return _receiver_client(cfg_dir, tmp_path, monkeypatch, QUIET)


MIXED = {"channels": QUIET["channels"] + [
    {"id": "csx_dispatch", "label": "CSX Test dispatcher", "kind": "rail_dispatcher", "railroad": "CSX",
     "subdivision": "Test Sub", "freq_mhz": 160.65}]}


@pytest.fixture()
def mixed(db, cfg_dir, tmp_path, monkeypatch):
    """Three voice channels on one line, for one receiver state each."""
    client, status = _receiver_client(cfg_dir, tmp_path, monkeypatch, MIXED)
    return client, status, db


def test_no_receiver_is_not_a_quiet_channel(quiet):
    client, _status = quiet
    r = client.get("/v1/feeds/status").json()
    feeds = {f["feed_id"]: f for f in r["feeds"]}
    assert feeds["csx_road"]["receiver"] == {"state": "not_installed",
                                             "device": "rail-airband: RTL-SDR serial RAIL160 (rtl_airband)"}
    assert feeds["csx_road"]["coverage"] == {"area": "Test area", "radius_km": 5.0, "lines": [
        {"railroad": "CSX", "name": "Test Sub", "aliases": ["TESTNAME (CSXT)", "Other Sub"]}]}
    assert feeds["example_fire_dispatch"]["example"] is True and feeds["csx_road"]["example"] is False
    assert feeds["eot_hot"]["receiver"]["state"] == "not_installed"
    # the example fire feed doesn't count: public safety is not configured at all
    assert r["coverage_summary"] == {"rail_radio": "no_receiver", "eot": "no_receiver", "public_safety": "not_configured"}
    a = client.get("/v1/rail/activity", params={"lat": 42.26, "lon": -83.71}).json()
    assert a["coverage"]["covered"] is False and a["transcripts"] == []
    assert {c["receiver_state"] for c in a["coverage"]["channels"]} == {"not_installed"}
    assert "no receiver has ever reported" in a["coverage"]["note"] and "not a quiet line" in a["coverage"]["note"]
    assert "No EOT/HOT receiver has reported yet" in a["coverage"]["note"]


def test_fresh_heartbeat_with_no_traffic_is_quiet(quiet, db):
    import os
    import time
    client, status = quiet
    (status / "rail-airband.prom").write_text(ROAD_ONLY)
    (status / "traintap-eot.heartbeat").write_text("")
    r = client.get("/v1/feeds/status").json()
    feeds = {f["feed_id"]: f for f in r["feeds"]}
    assert feeds["csx_road"]["receiver"]["state"] == "online" and feeds["csx_road"]["chunks_last_hour"] == 0
    assert feeds["csx_yard"]["receiver"]["state"] == "not_installed"        # not in rtl_airband's channel list
    assert feeds["eot_hot"]["receiver"]["state"] == "online" and feeds["eot_hot"]["state"] == "up"
    assert r["coverage_summary"] == {"rail_radio": "listening", "eot": "listening", "public_safety": "not_configured"}
    # a fresh heartbeat alone is "online now", not a quiet window: nothing shows the receiver
    # (or the ingest stage) listening before it
    a = client.get("/v1/rail/activity", params=NEAR).json()
    note = a["coverage"]["note"]
    assert a["coverage"]["covered"] is True and _not_quiet(note) and "can't vouch for 1 of 2 channels" in note
    assert _states(a) == {"rail_voice": "unknown", "eot": "unknown"}
    assert any("no heartbeat history" in x for x in a["verdicts"]["rail_voice"]["reasons"])
    # 40 minutes of history: rtl_airband doesn't tune csx_yard, so only csx_road may be called quiet
    _listening(db, status, ROAD_ONLY)
    a = client.get("/v1/rail/activity", params=NEAR).json()
    note = a["coverage"]["note"]
    assert a["coverage"]["covered"] is True and "they were quiet" not in note
    assert "online for 1 of 2 channels on this line (CSX Test Sub road)" in note and "that channel was quiet" in note
    assert "Not covered: 1 channel with no receiver" in note
    assert "location not configured" not in note and "No EOT/HOT receiver" not in note   # eot has a bias + radius
    assert _states(a) == {"rail_voice": "unknown", "eot": "quiet"}            # csx_yard covers the point too
    assert "csx_yard: no receiver has ever reported" in a["verdicts"]["rail_voice"]["reasons"]
    # once it tunes both (a re-render restarts rtl_airband: a new run), the whole line was quiet
    with db.begin() as c:
        c.execute(text("DELETE FROM liveness WHERE component = 'receiver:rail-airband'"))
    _listening(db, status, BOTH_TUNED, eot=False, ingest=False)
    a = client.get("/v1/rail/activity", params=NEAR).json()
    assert {c["receiver_state"] for c in a["coverage"]["channels"]} == {"online"}
    assert "they were quiet" in a["coverage"]["note"] and "Not covered" not in a["coverage"]["note"]
    v = a["verdicts"]["rail_voice"]
    assert v["state"] == "quiet" and datetime.fromisoformat(v["listening_since"].replace("Z", "+00:00")) < \
        datetime.fromisoformat(v["window_start"].replace("Z", "+00:00"))
    # the same heartbeat 10 minutes later: the receiver went offline
    old = time.time() - 600
    for p in status.iterdir():
        os.utime(p, (old, old))
    r2 = client.get("/v1/feeds/status").json()
    assert r2["coverage_summary"] == {"rail_radio": "receiver_offline", "eot": "receiver_offline",
                                      "public_safety": "not_configured"}
    a2 = client.get("/v1/rail/activity", params=NEAR).json()
    assert a2["coverage"]["covered"] is True and "is offline (last heard" in a2["coverage"]["note"]
    assert _states(a2) == {"rail_voice": "unknown", "eot": "unknown"}          # listened to for part of it


def test_mixed_receivers_say_quiet_only_where_listening(mixed):
    """One channel online, one never reported, one offline, nothing heard: only the online one
    was quiet; the note counts the other two as not covered."""
    client, status, db = mixed
    _listening(db, status, ROAD_ONLY)                                # rtl_airband tunes csx_road only
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    with db.begin() as c:                                            # csx_dispatch was last heard 2 h ago
        c.execute(text("""INSERT INTO transcript_chunks (chunk_id, feed_id, channel_id, "timestamp", captured_at,
                          status, raw_transcript, source, source_key, created_at)
                          VALUES (:cid, 'csx_dispatch', 'csx_dispatch', :t, :t, 'done', 'Q326 copy', 'sdr_files',
                          'mixed-1', :t)"""), {"cid": str(uuid.uuid4()), "t": two_hours_ago})
    r = client.get("/v1/feeds/status").json()
    states = {f["feed_id"]: f["receiver"]["state"] for f in r["feeds"]}
    assert (states["csx_road"], states["csx_yard"], states["csx_dispatch"]) == ("online", "not_installed", "offline")
    assert r["coverage_summary"]["rail_radio"] == "listening"
    a = client.get("/v1/rail/activity", params={"lat": 42.26, "lon": -83.71}).json()
    assert a["transcripts"] == []
    # the same receiver states as /v1/feeds/status, and a receiver has reported: covered
    by_label = {c["channel"]: c["receiver_state"] for c in a["coverage"]["channels"] if c["kind"] != "eot"}
    assert by_label == {"CSX Test Sub road": "online", "CSX Test yard": "not_installed",
                        "CSX Test dispatcher": "offline"}
    assert a["coverage"]["covered"] is True
    note = a["coverage"]["note"]
    assert "they were quiet" not in note and "these channels" not in note
    assert ("The rail voice receiver is online for 1 of 3 channels on this line (CSX Test Sub road) and nothing "
            "was heard on it in the window (up to ") in note
    assert ", Pulse's newest heartbeats): that channel was quiet." in note
    last = two_hours_ago.isoformat().replace("+00:00", "Z")
    assert (f"Not covered: 1 channel with no receiver (none has ever reported for it) and 1 channel whose receiver "
            f"is offline (last heard {last}). No transcripts on those channels means no coverage, not a quiet "
            "line.") in note
    assert "CSX Test yard" not in note and "CSX Test dispatcher" not in note   # never called quiet
    assert note.count("quiet") == 2                                  # "was quiet" + "not a quiet line"
    assert a["verdicts"]["rail_voice"]["state"] == "unknown"           # two covering channels aren't listened to


def test_voice_notes_for_a_partly_tuned_line():
    """No database: the wording for 2 of 11 channels tuned, with and without traffic, and for a
    line with no receiver online at all."""
    from api.routes.v1 import _voice_notes
    from core.config_files import Channel
    chans = [Channel(id=f"ch{i}", label=f"Ch {i}", kind="rail_voice") for i in range(11)]
    rstate = {c.id: {"state": "online" if i < 2 else "not_installed"} for i, c in enumerate(chans)}
    # "online now" alone is never quiet
    assert _voice_notes(chans, rstate, heard=False)[0] == (
        "The rail voice receiver is online now, but Pulse can't vouch for 2 of 11 channels on this line across the "
        "whole window (see `verdicts.rail_voice.reasons`): no transcripts here means no proof, not a quiet line.")
    assert _voice_notes(chans, rstate, heard=False, proven={"ch0", "ch1"}) == [
        "The rail voice receiver is online for 2 of 11 channels on this line (Ch 0, Ch 1) and nothing was heard "
        "on them in the window: those channels were quiet.",
        "Not covered: 9 channels with no receiver (none has ever reported for them). No transcripts on those "
        "channels means no coverage, not a quiet line."]
    assert _voice_notes(chans, rstate, heard=True, proven={"ch0", "ch1"}) == [
        "Not covered: 9 channels with no receiver (none has ever reported for them). No transcripts on those "
        "channels means no coverage, not a quiet line."]
    three = chans[:3]
    rstate = {"ch0": {"state": "offline", "last_seen_at": "2026-11-14T10:00:00Z"},
              "ch1": {"state": "offline", "last_seen_at": "2026-11-14T11:00:00Z"}, "ch2": {"state": "not_installed"}}
    assert _voice_notes(three, rstate, heard=False) == [
        "No rail voice receiver for this line is online.",
        "Not covered: 1 channel with no receiver (none has ever reported for it) and 2 channels whose receiver is "
        "offline (last heard 2026-11-14T11:00:00Z). No transcripts on those channels means no coverage, not a "
        "quiet line."]


def test_rail_hint_matches_other_dataset_names(quiet):
    """Another dataset's name for the line resolves through the aliases, at the line and away
    from it, but only the line's own area makes its channels count."""
    client, _ = quiet
    for hint in ("TESTNAME (CSXT)", "testname", "Other Sub", "Test Subdivision"):
        r = client.get("/v1/rail/activity", params={"lat": 42.26, "lon": -83.71, "subdivision": hint}).json()
        assert r["subdivision"] == "Test Sub" and {c["channel"] for c in r["coverage"]["channels"]} >= {
            "CSX Test Sub road"}, hint
        assert "Pulse monitors" not in r["coverage"]["note"], hint
        far = client.get("/v1/rail/activity", params={"lat": 42.9, "lon": -83.0, "subdivision": hint}).json()
        assert far["coverage"]["covered"] is False and far["coverage"]["channels"] == [], hint
        assert "Pulse monitors the CSX Test Sub line, but no receiver for it covers this point" in \
            far["coverage"]["note"], hint
    none = client.get("/v1/rail/activity", params={"lat": 42.9, "lon": -83.0, "subdivision": "Holly"}).json()
    assert "No monitored rail voice channel" in none["coverage"]["note"]
    assert "Pulse monitors" not in none["coverage"]["note"]


def _recording(c, channel: str, status: str, transcript: str | None, minutes_ago: float, key: str,
               ingested_minutes_ago: float | None = None) -> str:
    """One SDR recording in transcript_chunks, `minutes_ago` old, at transcription stage `status`,
    ingested when it was captured unless `ingested_minutes_ago` says otherwise."""
    now = datetime.now(timezone.utc)
    cid, t = str(uuid.uuid4()), now - timedelta(minutes=minutes_ago)
    created = t if ingested_minutes_ago is None else now - timedelta(minutes=ingested_minutes_ago)
    c.execute(text("""INSERT INTO transcript_chunks (chunk_id, feed_id, channel_id, "timestamp", captured_at, status,
                      raw_transcript, source, source_key, created_at)
                      VALUES (:cid, :ch, :ch, :t, :t, :st, :tx, 'sdr_files', :key, :created)"""),
              {"cid": cid, "ch": channel, "t": t, "st": status, "tx": transcript, "key": key, "created": created})
    return cid


def _not_quiet(note: str) -> bool:
    """The note never calls anything quiet (it may still say "not a quiet line")."""
    return "quiet" not in note.replace("not a quiet line", "")


def test_recordings_not_transcribed_yet_are_not_quiet(quiet, db):
    """Recordings in the window that are still queued or transcribing, that failed, or that had no
    speech are traffic: the line is never called quiet, with or without a heartbeat."""
    client, status = quiet
    near = {"lat": 42.26, "lon": -83.71}
    with db.begin() as c:
        for i in range(5):
            _recording(c, "csx_road", "queued", None, i + 1, f"queued-{i}")
    a = client.get("/v1/rail/activity", params=near).json()
    # no heartbeat file: the queued recordings themselves show csx_road's receiver is online
    states = {c["channel"]: c["receiver_state"] for c in a["coverage"]["channels"] if c["kind"] != "eot"}
    assert states == {"CSX Test Sub road": "online", "CSX Test yard": "not_installed"}
    note = a["coverage"]["note"]
    assert a["transcripts"] == [] and a["coverage"]["covered"] is True and _not_quiet(note)
    assert "5 recordings not transcribed yet" in note and "Not covered: 1 channel with no receiver" in note
    assert a["coverage"]["recordings_without_text"] == {"pending": 5, "error": 0, "no_speech": 0}

    # a fresh heartbeat for both channels, and every other kind of recording with no text
    (status / "rail-airband.prom").write_text(BOTH_TUNED)
    with db.begin() as c:
        _recording(c, "csx_yard", "transcribing", None, 2, "transcribing-1")
        _recording(c, "csx_yard", "error", None, 3, "error-1")
        _recording(c, "csx_road", "done", "", 4, "no-speech-1")
    a = client.get("/v1/rail/activity", params=near).json()
    note = a["coverage"]["note"]
    assert {c["receiver_state"] for c in a["coverage"]["channels"] if c["kind"] != "eot"} == {"online"}
    assert a["transcripts"] == [] and _not_quiet(note)
    assert ("Recorded on this line in the window but not in `transcripts`: 6 recordings not transcribed yet, "
            "1 recording whose transcription failed (error) and 1 recording with no speech to transcribe. "
            "That is radio traffic, not a quiet line.") in note
    assert a["coverage"]["recordings_without_text"] == {"pending": 6, "error": 1, "no_speech": 1}

    # a failed recording alone is still traffic; one older than the window isn't
    with db.begin() as c:
        c.execute(text("DELETE FROM transcript_chunks"))
        _recording(c, "csx_road", "error", None, 3, "error-2")
        _recording(c, "csx_road", "queued", None, 40, "queued-old")
    a = client.get("/v1/rail/activity", params=near).json()
    note = a["coverage"]["note"]
    assert _not_quiet(note) and "1 recording whose transcription failed (error)" in note
    assert a["coverage"]["recordings_without_text"] == {"pending": 0, "error": 1, "no_speech": 0}
    assert a["verdicts"]["rail_voice"]["state"] == "activity"
    assert a["verdicts"]["rail_voice"]["counts"]["recordings_without_text"] == 1
    # with 40 minutes of receiver and ingest history, a window that ends before them is quiet
    _listening(db, status, BOTH_TUNED)
    a = client.get("/v1/rail/activity", params={**near, "since_minutes": 2}).json()
    assert "they were quiet" in a["coverage"]["note"] and "recordings_without_text" not in a["coverage"]
    assert a["verdicts"]["rail_voice"]["state"] == "quiet"


def test_voice_notes_count_untranscribed_recordings_as_traffic():
    """No database: recordings with no text yet suppress "quiet", partly tuned lines included."""
    from api.routes.v1 import _voice_notes
    from core.config_files import Channel
    chans = [Channel(id=f"ch{i}", label=f"Ch {i}", kind="rail_voice") for i in range(3)]
    online = {c.id: {"state": "online"} for c in chans}
    assert _voice_notes(chans, online, heard=False, unread={"pending": 5}) == [
        "Recorded on this line in the window but not in `transcripts`: 5 recordings not transcribed yet. That is "
        "radio traffic, not a quiet line."]
    assert _voice_notes(chans, online, heard=False, unread={"pending": 0, "error": 1, "no_speech": 0}) == [
        "Recorded on this line in the window but not in `transcripts`: 1 recording whose transcription failed "
        "(error). That is radio traffic, not a quiet line."]
    assert _voice_notes(chans, online, heard=False, unread={"pending": 0, "error": 0, "no_speech": 0},
                        proven={"ch0", "ch1", "ch2"}) == [
        "The rail voice receiver is online and nothing was heard on these channels in the window: they were quiet."]
    partly = {"ch0": {"state": "online"}, "ch1": {"state": "not_installed"}, "ch2": {"state": "not_installed"}}
    assert _voice_notes(chans, partly, heard=False, unread={"pending": 2, "no_speech": 1}, proven={"ch0"}) == [
        "Recorded on this line in the window but not in `transcripts`: 2 recordings not transcribed yet and 1 "
        "recording with no speech to transcribe. That is radio traffic, not a quiet line.",
        "Not covered: 2 channels with no receiver (none has ever reported for them). No transcripts on those "
        "channels means no coverage, not a quiet line."]


HINT = {"channels": QUIET["channels"] + [
    # a line with no rail-plan entry: only its own receiver area (bias + radius) places it
    {"id": "far_road", "label": "NS Far Sub road", "kind": "rail_road", "railroad": "NS", "subdivision": "Far Sub",
     "bias": {"lat": 42.9, "lon": -83.0},
     "coverage": {"radius_km": 10, "lines": [{"name": "Far Sub", "aliases": ["FARNAME (NS)"]}]}}]}


def test_rail_hint_never_reaches_a_point_outside_the_line(db, cfg_dir, tmp_path, monkeypatch):
    """A point outside every monitored area is not covered, whatever the hints say: never quiet,
    and never the traffic a receiver heard somewhere else. Hints match names exactly, and a
    railroad hint that matches no line leaves none (never all of them)."""
    client, status = _receiver_client(cfg_dir, tmp_path, monkeypatch, HINT)
    (status / "rail-airband.prom").write_text(BOTH_TUNED)
    (status / "traintap-eot.heartbeat").write_text("")
    with db.begin() as c:
        cid = _recording(c, "csx_road", "done", "Q326 copy, holding at the east end", 3, "near-1")
        c.execute(text("""INSERT INTO rail_detector_events (chunk_id, channel_id, ts, railroad, subdivision,
                          detector_name, milepost, track, axle_count, result, alarms, parse_source, raw_text)
                          VALUES (:cid, 'csx_road', now() - interval '3 minutes', 'CSX', 'Test Sub', 'detector',
                          12.3, '1', 200, 'no_defects', '[]', 'rules', 'no defects')"""), {"cid": cid})
        _recording(c, "far_road", "done", "far line traffic", 2, "far-1")
    outside = {"lat": 45.0, "lon": -90.0}                     # hundreds of km from every area
    for hints in ({}, {"subdivision": "Test Sub"}, {"subdivision": "TESTNAME"}, {"subdivision": "est"},
                  {"subdivision": "TESTNAME", "railroad": "CSX"}, {"subdivision": "TESTNAME", "railroad": "NS"},
                  {"subdivision": "Far Sub"}, {"subdivision": "FARNAME (NS)"}, {"railroad": "NS"}):
        r = client.get("/v1/rail/activity", params={**outside, **hints}).json()
        assert r["coverage"]["covered"] is False and r["coverage"]["channels"] == [], hints
        assert r["transcripts"] == [] and r["detector_readouts"] == [] and r["eot_hot"] == [], hints
        assert _not_quiet(r["coverage"]["note"]) and "No monitored rail voice channel" in r["coverage"]["note"], hints
    r = client.get("/v1/rail/activity", params={**outside, "subdivision": "TESTNAME"}).json()
    assert ("Pulse monitors the CSX Test Sub line, but no receiver for it covers this point (it is outside that "
            "line's monitored area)") in r["coverage"]["note"]
    r = client.get("/v1/rail/activity", params={**outside, "subdivision": "est"}).json()   # a substring names nothing
    assert "Pulse monitors" not in r["coverage"]["note"]

    near = {"lat": 42.26, "lon": -83.71}
    # a railroad hint that matches no line here leaves no voice channel, not all of them
    r = client.get("/v1/rail/activity", params={**near, "railroad": "NS"}).json()
    assert [c["kind"] for c in r["coverage"]["channels"]] == ["eot"] and r["transcripts"] == []
    assert r["detector_readouts"] == [] and _not_quiet(r["coverage"]["note"])
    r = client.get("/v1/rail/activity", params={**near, "railroad": "csx"}).json()
    assert r["subdivision"] == "Test Sub" and [t["text_untrusted"][:4] for t in r["transcripts"]] == ["Q326"]
    # a monitored line named at a point it doesn't reach is said so; the line here still counts
    r = client.get("/v1/rail/activity", params={**near, "subdivision": "Far Sub"}).json()
    assert {c["channel"] for c in r["coverage"]["channels"] if c["kind"] != "eot"} == {"CSX Test Sub road",
                                                                                      "CSX Test yard"}
    assert [t["text_untrusted"][:4] for t in r["transcripts"]] == ["Q326"]
    assert "Pulse monitors the NS Far Sub line, but no receiver for it covers this point" in r["coverage"]["note"]

    # inside the far line's own receiver area, an exact (normalised) name or alias finds it
    at_far = {"lat": 42.9, "lon": -83.0}
    for hint in ("Far Sub", "FARNAME (NS)", "farname"):
        r = client.get("/v1/rail/activity", params={**at_far, "subdivision": hint}).json()
        assert [c["channel"] for c in r["coverage"]["channels"]] == ["NS Far Sub road"], hint
        assert [t["text_untrusted"] for t in r["transcripts"]] == ["far line traffic"], hint
        assert r["coverage"]["covered"] is True and "Pulse monitors" not in r["coverage"].get("note", ""), hint
    # its own area covers the point with or without a hint: a substring hint names nothing (no
    # note) and changes nothing; the wrong railroad leaves no channel
    for hints in ({}, {"subdivision": "Fa"}, {"subdivision": "arname"}):
        r = client.get("/v1/rail/activity", params={**at_far, **hints}).json()
        assert [c["channel"] for c in r["coverage"]["channels"]] == ["NS Far Sub road"], hints
        assert "Pulse monitors" not in r["coverage"].get("note", ""), hints
    r = client.get("/v1/rail/activity", params={**at_far, "subdivision": "Far Sub", "railroad": "CSX"}).json()
    assert r["coverage"]["channels"] == [] and r["transcripts"] == [] and r["coverage"]["covered"] is False


def test_feeds_status_with_shipped_config_reports_examples(db):
    from api.main import create_app
    r = TestClient(create_app()).get("/v1/feeds/status").json()
    feeds = {f["feed_id"]: f for f in r["feeds"]}
    assert feeds["example_rail_road"]["example"] is True and feeds["eot_hot"]["example"] is False
    assert r["coverage_summary"] == {"rail_radio": "not_configured", "eot": "no_receiver",
                                     "public_safety": "not_configured"}


def test_ui_routes(seeded):
    assert len(seeded.get("/incidents/active").json()) == 2
    ch = seeded.get(f"/incidents/{INC}/chunks").json()
    assert ch["chunk_count"] == 2
    assert {c["id"] for c in seeded.get("/channels").json()} == {"aa_fire", "csx_road", "eot_hot"}
    assert seeded.get("/rail/recent").json()["eot_hot"][0]["unit_id"] == 45678
    assert seeded.get("/external/counties").json()["type"] == "FeatureCollection"
    assert seeded.get("/audio/stream/anything").status_code == 404                 # the Broadcastify relay is gone


def test_bearer_token_required_when_set(seeded, monkeypatch):
    from core.settings import reset_settings_cache
    monkeypatch.setenv("PULSE_API_TOKEN", "s3cret-token")
    reset_settings_cache()
    _err(seeded.get("/v1/feeds/status"), 401, "unauthorized")
    _err(seeded.get("/incidents/active", headers={"Authorization": "Bearer wrong"}), 401, "unauthorized")
    assert seeded.get("/v1/feeds/status", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
    assert seeded.get("/v1/health").status_code == 200                              # health stays open
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with seeded.websocket_connect("/ws") as ws:
            ws.receive_text()
    with seeded.websocket_connect("/ws", headers={"Authorization": "Bearer s3cret-token"}) as ws:
        assert json.loads(ws.receive_text())["event"] == "connected"


def test_admin_disabled_unless_configured(seeded, monkeypatch):
    from core.settings import reset_settings_cache
    assert seeded.delete("/admin/reset/all", params={"confirm": "RESET-LIVE-DATA"}).status_code == 404
    assert seeded.get("/admin/overview").status_code == 404
    monkeypatch.setenv("PULSE_ADMIN_TOKEN", "adm1n")
    reset_settings_cache()
    assert seeded.get("/admin/overview").status_code == 403
    assert seeded.get("/admin/overview", headers={"X-Pulse-Admin-Token": "nope"}).status_code == 403
    h = {"X-Pulse-Admin-Token": "adm1n"}
    ov = seeded.get("/admin/overview", headers=h).json()
    assert ov["queue"]["queued"] == 1 and ov["llm"]["daily_cap_usd"] == 1.0
    assert seeded.delete("/admin/reset/all", headers=h).status_code == 400            # confirm required
    assert seeded.post(f"/admin/incidents/{INC}/resolve", headers=h).json()["status"] == "RESOLVED"
    assert seeded.delete("/admin/reset/all", headers=h, params={"confirm": "RESET-LIVE-DATA"}).json() == {"reset": True}
    assert seeded.get("/incidents/active").json() == []
