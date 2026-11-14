"""Batched structured-output extraction, with a mocked Anthropic client (never the real API)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text

from extract import prompt as P
from extract.claude import ClaudeExtractor, max_tokens_for
from extract.cost import CostMeter
from extract.schema import BATCH_SCHEMA, ITEM_SCHEMA, count_unions, parse_item
from tests.mock_anthropic import MockAnthropic, default_item


def _walk_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from _walk_objects(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _walk_objects(v)


def test_schema_fits_structured_outputs_rules():
    objs = list(_walk_objects(BATCH_SCHEMA))
    assert len(objs) >= 5
    for o in objs:
        assert o["additionalProperties"] is False
        assert set(o["required"]) == set(o["properties"])
    dumped = json.dumps(BATCH_SCHEMA)
    for unsupported in ("minimum", "maximum", "minLength", "maxLength", "multipleOf"):
        assert unsupported not in dumped
    # the API rejects schemas with more than 16 union-typed parameters (400 seen on 2026-11-14 at 17)
    assert count_unions(BATCH_SCHEMA) <= 16
    assert set(ITEM_SCHEMA["properties"]) == {"id", "has_location", "location", "incident_type", "priority",
                                              "units", "is_rail", "rail", "short_summary"}


def _items(n=3):
    at = datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc)
    tx = ["Engine 4 respond to Packard and Stadium for a vehicle fire",
          "Medic 7 one nine four two two Grand River, fall",
          "Car 12 copy 10-4", "Q326 holding east end for the westbound", "shots fired 7 Mile and Evergreen"]
    return [{"id": f"t{i}", "transcript": tx[i % len(tx)], "context": {"channel": "Test", "kind": "fire"},
             "captured_at": at} for i in range(n)]


def test_request_shape_matches_config_c():
    ex = ClaudeExtractor(MockAnthropic(), meter=None)
    kw = ex.request_kwargs(_items(10))
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["thinking"] == {"type": "disabled"}
    assert kw["output_config"]["effort"] == "medium"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    for banned in ("temperature", "top_p", "top_k", "fallbacks"):
        assert banned not in kw
    sys_block = kw["system"][0]
    assert sys_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert kw["max_tokens"] == max_tokens_for(10)
    assert kw["messages"][0]["content"].count("<transmission ") == 10


def test_system_prompt_is_static_and_cacheable():
    a, b = P.system_text(), P.system_text()
    assert a == b
    assert P.approx_tokens(a) >= 1024            # comfortably above Haiku 5.5's 512-token cache minimum
    assert "2026" not in a                        # nothing time-varying in the cached prefix
    assert "EXAMPLES" in a


@pytest.fixture()
def meter(db):
    return CostMeter(daily_cap=1.0, monthly_cap=15.0)


def test_batch_parses_and_meters(meter, db):
    client = MockAnthropic(lambda i, t: {**default_item(i, t), "incident_type": "VEHICLE_FIRE", "units": ["Engine 4"]})
    ex = ClaudeExtractor(client, meter)
    out = ex.extract(_items(3))
    assert set(out.results) == {"t0", "t1", "t2"} and not out.fallback_ids
    assert out.results["t0"].incident_type == "VEHICLE_FIRE"
    assert out.results["t0"].units == ["Engine 4"]
    # second call reads the cached prefix
    out2 = ex.extract(_items(2))
    assert out2.cache_read_tokens > 0 and out.cache_write_tokens > 0
    with db.connect() as c:
        rows = c.execute(text("SELECT items, cost_usd, cache_creation_1h, cache_read_input_tokens, stop_reason, "
                              "price_version FROM llm_usage ORDER BY id")).all()
    assert [r[0] for r in rows] == [3, 2]
    assert all(float(r[1]) > 0 for r in rows)
    assert rows[0][2] > 0 and rows[1][3] > 0
    assert rows[0][4] == "end_turn" and rows[0][5] == "2026-11-13"
    assert meter.spent_today() == pytest.approx(float(rows[0][1]) + float(rows[1][1]))


def test_missing_ids_fall_back_to_rules(meter):
    client = MockAnthropic()
    client.drop_ids = {"t1"}
    out = ClaudeExtractor(client, meter).extract(_items(3))
    assert set(out.results) == {"t0", "t2"} and out.fallback_ids == ["t1"]


def test_refusal_never_reads_content(meter):
    client = MockAnthropic()
    client.force_stop = "refusal"
    out = ClaudeExtractor(client, meter).extract(_items(2))
    assert not out.results and out.fallback_ids == ["t0", "t1"]
    assert out.fallback_reason == "refusal:cyber"


def test_max_tokens_splits_once(meter):
    client = MockAnthropic()
    client.max_items_before_truncation = 2          # 4 items truncate; halves of 2 succeed
    out = ClaudeExtractor(client, meter).extract(_items(4))
    assert len(client.calls) == 3 and set(out.results) == {"t0", "t1", "t2", "t3"}


def test_api_error_degrades_batch_only(meter, db):
    import anthropic
    client = MockAnthropic()
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client.raise_exc = anthropic.APIConnectionError(request=req)
    ex = ClaudeExtractor(client, meter)
    out = ex.extract(_items(2))
    assert out.fallback_reason == "api_error" and out.fallback_ids == ["t0", "t1"]
    assert ex.extract(_items(2)).results            # the next batch is unaffected
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM llm_usage WHERE error IS NOT NULL")).scalar() == 1


def test_spend_limit_429_stops_until_next_month(meter, db):
    import anthropic
    client = MockAnthropic()
    resp = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
                          json={"type": "error", "error": {"type": "rate_limit_error",
                                                           "error_code": "enforced_spend_limit_reached",
                                                           "message": "spend limit reached"}})
    client.raise_exc = anthropic.RateLimitError("spend limit reached", response=resp,
                                                body={"error": {"error_code": "enforced_spend_limit_reached"}})
    ex = ClaudeExtractor(client, meter)
    assert ex.extract(_items(1)).fallback_reason == "api_error"
    out = ex.extract(_items(1))
    assert out.fallback_reason == "cap:spend_limit" and len(client.calls) == 1   # no second request sent


def test_no_client_means_rules():
    out = ClaudeExtractor(None, meter=None).extract(_items(2))
    assert out.fallback_reason == "no_api_key" and out.fallback_ids == ["t0", "t1"]


def test_parse_item_coerces_and_guards():
    it = parse_item({"id": "x", "has_location": True,
                     "location": {"kind": "address", "street1": "Grand River Avenue", "street2": None,
                                  "number": "19,422", "city": None, "raw": "one nine four two two Grand River"},
                     "incident_type": "NOT_A_TYPE", "priority": "HIGH", "units": [" Medic 7 ", ""],
                     "is_rail": False, "rail": None, "short_summary": "Fall"})
    assert it.location.number == "19422" and it.incident_type == "UNKNOWN" and it.units == ["Medic 7"]
    assert it.location.display() == "19422 Grand River Avenue"
    none = parse_item({"id": "y", "has_location": True, "location": {"kind": "none"}, "incident_type": "NONE",
                       "priority": "LOW", "units": [], "is_rail": False, "rail": None, "short_summary": ""})
    assert none.has_location is False and none.has_incident is False
