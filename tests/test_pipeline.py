"""End-to-end extraction stage on synthetic fixtures: prefilter, batching, geocode, correlation, storage."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from extract.cost import CostMeter
from extract.worker import ExtractWorker
from geocode.base import GeoResult
from geocode.chain import Geocoder
from ingest.sources.fixture import FixtureSource
from tests.mock_anthropic import MockAnthropic, default_item

CHANNELS = """
channels:
  - id: aa_fire
    label: Test fire dispatch
    kind: fire
    unit_prefix: AAFD
    county: Washtenaw
    city: Ann Arbor
    area_id: ann_arbor
  - id: test_rail
    label: Test rail road
    kind: rail_road
    railroad: CSX
    subdivision: Test Sub
"""


class FixedGeo:
    name = "fake"

    def supports(self, kind):
        return True

    def geocode(self, q):
        if q.street1 and "Packard" in q.street1:
            return GeoResult(42.2650, -83.7310, "Packard St & Stadium Blvd, Ann Arbor", "HIGH", "fake")
        return None


def responder(tid, transcript):
    d = default_item(tid, transcript)
    t = transcript.lower()
    if "packard" in t:
        d.update(has_location=True, incident_type="VEHICLE_FIRE", priority="MEDIUM", units=["Engine 4", "Ladder 1"],
                 location={"kind": "intersection", "street1": "Packard Street", "street2": "Stadium Boulevard",
                           "number": "", "city": "Ann Arbor", "raw": "Packard and Stadium"},
                 short_summary="Vehicle fire at Packard and Stadium")
    elif "fully involved" in t:
        d.update(incident_type="VEHICLE_FIRE", priority="HIGH", units=["Engine 4"], short_summary="Car fully involved")
    elif "q326" in t:
        d.update(incident_type="RAIL_OPERATIONS", priority="LOW", units=["Q326"], is_rail=True,
                 rail={"railroad": "CSX", "subdivision": "", "train_symbol": "Q326", "milepost": "",
                       "reason_label": "meet_or_hold", "detector": None}, short_summary="Q326 holding for a meet")
    return d


@pytest.fixture()
def setup(db, cfg_dir):
    (cfg_dir / "channels.local.yaml").write_text(CHANNELS)
    client = MockAnthropic(responder)
    w = ExtractWorker(client=client, meter=CostMeter(daily_cap=1.0, monthly_cap=15.0),
                      geocoder=Geocoder(providers=[FixedGeo()], use_cache=False), summary_client=client)
    return w, client, db


def _fixture(tmp_path, lines):
    import json
    p = tmp_path / "fx.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return p


def test_full_flow(setup, tmp_path):
    w, client, db = setup
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    lines = [
        {"channel_id": "aa_fire", "text": "Engine 4, Ladder 1, respond to Packard and Stadium, vehicle fire.", "offset_s": 0},
        {"channel_id": "aa_fire", "text": "Engine 4 on scene.", "offset_s": 60},                  # chatter -> prefilter
        {"channel_id": "aa_fire", "text": "Engine 4, car is fully involved, need a second line.", "offset_s": 90},
        {"channel_id": "test_rail", "text": "Q326 holding at the east end for the westbound.", "offset_s": 100},
        {"channel_id": "test_rail", "text": "CSX equipment detector, milepost 123.4, track 1. No defects. Total axles 412. "
                                            "Detector out.", "offset_s": 110},
        {"channel_id": "aa_fire", "text": "Thank you.", "offset_s": 115},                         # whisper noise
        {"channel_id": "aa_fire", "text": "Ladder 1, Engine 4 clear, back in service.", "offset_s": 120},
    ]
    assert FixtureSource(_fixture(tmp_path, lines), start=start, run_id="e2e").poll_once() == 7
    w.poll(force_flush=True)
    # one batched Claude call for the 3 transmissions that passed the prefilter (+0 summary calls on update)
    extract_calls = [c for c in client.calls if "results" in str(c["output_config"]["format"]["schema"])]
    assert len(extract_calls) == 1
    assert extract_calls[0]["messages"][0]["content"].count("<transmission ") == 3

    with db.connect() as c:
        rows = c.execute(text("""SELECT raw_transcript, status, prefilter, extract_source, has_incident,
                                        correlation_action, incident_id, loc_kind, lat FROM transcript_chunks
                                 ORDER BY captured_at""")).mappings().all()
        assert all(r["status"] == "done" for r in rows)                    # nothing silently dropped
        by = {r["raw_transcript"][:12]: r for r in rows}
        first = by["Engine 4, La"]
        assert first["extract_source"] == "claude" and first["correlation_action"] == "NEW"
        assert first["loc_kind"] == "intersection" and first["lat"] == pytest.approx(42.2650)
        assert by["Engine 4 on "]["prefilter"] == "chatter" and by["Engine 4 on "]["has_incident"] is False
        assert by["Engine 4 on "]["correlation_action"] == "UPDATE"         # status joins the timeline
        assert by["Engine 4, ca"]["correlation_action"] == "UPDATE"        # same units, same channel
        assert by["Thank you."]["prefilter"] == "whisper_noise"
        assert by["Q326 holding"]["correlation_action"] == "UNASSOCIATED"   # routine rail ops
        assert by["CSX equipmen"]["prefilter"] == "defect_detector" and by["CSX equipmen"]["extract_source"] == "prefilter"
        assert by["Engine 4, La"]["incident_id"] == by["Engine 4, ca"]["incident_id"]
        assert by["Ladder 1, En"]["prefilter"] == "chatter" and by["Ladder 1, En"]["correlation_action"] == "RESOLVE"
        inc = c.execute(text("""SELECT status, units, priority, ST_AsText(location), loc_street1, geocode_source,
                                       summary, short_summary FROM incidents""")).mappings().all()
        assert len(inc) == 1
        i = inc[0]
        assert i["status"] == "RESOLVED"                                    # units cleared -> resolved
        assert set(i["units"]) == {"AAFD:Engine 4", "AAFD:Ladder 1"}
        assert i["priority"] == "HIGH"                                       # escalated by the update
        assert i["st_astext"] == "POINT(-83.731 42.265)" and i["loc_street1"] == "Packard Street"
        assert i["geocode_source"] == "fake" and i["summary"]               # summary written on close
        det = c.execute(text("SELECT milepost, result, axle_count, railroad, subdivision, parse_source "
                             "FROM rail_detector_events")).mappings().one()
        assert float(det["milepost"]) == 123.4 and det["result"] == "no_defects" and det["axle_count"] == 412
        assert det["railroad"] == "CSX" and det["subdivision"] == "Test Sub" and det["parse_source"] == "rules"
        usage = c.execute(text("SELECT workload, items FROM llm_usage ORDER BY id")).all()
        assert usage[0] == ("live_extract", 3)


def test_cap_hit_degrades_to_rules(setup, tmp_path):
    w, client, db = setup
    w.meter.daily_cap = 0.0
    lines = [{"channel_id": "aa_fire", "text": "Engine 4 respond to 7 Mile and Evergreen, shots fired", "offset_s": 0}]
    FixtureSource(_fixture(tmp_path, lines), run_id="cap").poll_once()
    w.poll(force_flush=True)
    assert client.calls == []
    with db.connect() as c:
        r = c.execute(text("SELECT status, extract_source, incident_type, priority, extraction->'flags' AS flags "
                           "FROM transcript_chunks")).mappings().one()
        assert r["status"] == "done" and r["extract_source"] == "rules"
        assert r["incident_type"] == "SHOOTING" and r["priority"] == "HIGH" and r["flags"] == ["fallback:cap:daily"]
        assert c.execute(text("SELECT count(*) FROM pipeline_events WHERE kind = 'llm_cap_hit'")).scalar() == 1


def test_batching_waits_for_ten_or_timeout(setup, tmp_path, monkeypatch):
    w, client, db = setup
    lines = [{"channel_id": "aa_fire", "text": f"Medic {i} respond to {100 + i} Main Street for a fall", "offset_s": i}
             for i in range(12)]
    FixtureSource(_fixture(tmp_path, lines), run_id="batch").poll_once()
    w.poll()                                    # 10 go out at once; 2 wait for the 45 s timer
    assert len(client.calls) == 1 and len(w.pending) == 2
    w.poll()
    assert len(client.calls) == 1
    monkeypatch.setattr(w, "oldest_wait_s", lambda: 46.0)
    w.poll()
    assert len(client.calls) == 2 and not w.pending
    assert client.calls[1]["messages"][0]["content"].count("<transmission ") == 2


def test_resolver_idle_and_requeue(setup):
    from correlation.resolver import housekeeping_once
    w, client, db = setup
    with db.begin() as c:
        c.execute(text("""INSERT INTO incidents (incident_id, feed_id, channel_id, county, status, opened_at, last_updated,
                          incident_type, priority) VALUES (gen_random_uuid(), 'aa_fire', 'aa_fire', '', 'ACTIVE',
                          now() - interval '6 hours', now() - interval '5 hours', 'MEDICAL', 'LOW')"""))
        c.execute(text("""INSERT INTO transcript_chunks (chunk_id, feed_id, channel_id, status, claimed_at, created_at,
                          source_key) VALUES (gen_random_uuid(), 'aa_fire', 'aa_fire', 'extracting',
                          now() - interval '1 hour', now() - interval '1 hour', 'stuck')"""))
    out = housekeeping_once()
    assert len(out["resolved"]) == 1 and out["requeued"] == 1
    with db.connect() as c:
        assert c.execute(text("SELECT status, summary IS NOT NULL FROM incidents")).one() == ("RESOLVED", True)
        assert c.execute(text("SELECT status FROM transcript_chunks")).scalar() == "transcribed"
