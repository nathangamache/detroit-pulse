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
    {"id": "eot_hot", "label": "EOT/HOT telemetry", "kind": "eot", "freq_mhz": 457.9375},
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
    far = seeded.get("/v1/rail/activity", params={"lat": 42.9, "lon": -83.0}).json()
    assert far["coverage"]["channels"] == [{"channel": "EOT/HOT telemetry", "frequency_mhz": 457.9375, "kind": "eot"}]
    assert "No monitored rail voice channel" in far["coverage"]["note"]
    hinted = seeded.get("/v1/rail/activity", params={"lat": 42.9, "lon": -83.0, "subdivision": "test"}).json()
    assert hinted["subdivision"] == "Test Sub"
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
