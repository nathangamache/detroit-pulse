"""Cost meter: price math, llm_usage rows, daily/monthly caps and degradation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from extract.cost import CapReached, CostMeter, Prices, month_start, next_month, usage_dict


def test_price_math_haiku():
    p = Prices.load()
    u = {"input_tokens": 1000, "output_tokens": 1000, "cache_read_input_tokens": 2000,
         "cache_creation_input_tokens": 2000, "cache_creation_1h": 2000, "cache_creation_5m": 0}
    # 1000*0.10 + 1000*0.50 + 2000*0.01 + 2000*0.20 = 100 + 500 + 20 + 400 = 1020 per M
    assert p.cost("claude-haiku-5-5", u) == pytest.approx(1020 / 1e6)
    u5 = dict(u, cache_creation_1h=0, cache_creation_5m=2000)
    assert p.cost("claude-haiku-5-5", u5) == pytest.approx((100 + 500 + 20 + 250) / 1e6)
    # creation tokens without a TTL breakdown are priced at the (higher) 1h rate
    u_unk = dict(u, cache_creation_1h=0, cache_creation_5m=0)
    assert p.cost("claude-haiku-5-5", u_unk) == pytest.approx(1020 / 1e6)
    # long prompts (>100K) bill 5x on Haiku 5.5
    big = {"input_tokens": 120_000, "output_tokens": 0}
    assert p.cost("claude-haiku-5-5", big) == pytest.approx(120_000 * 0.10 * 5 / 1e6)
    # unknown models are priced at the most expensive row
    assert p.cost("claude-mystery", {"input_tokens": 1_000_000}) == pytest.approx(4.00)


def test_usage_dict_reads_sdk_objects():
    from types import SimpleNamespace as NS
    u = usage_dict(NS(input_tokens=5, output_tokens=7, cache_creation_input_tokens=11, cache_read_input_tokens=13,
                      cache_creation=NS(ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=11)))
    assert u == {"input_tokens": 5, "output_tokens": 7, "cache_creation_input_tokens": 11,
                 "cache_read_input_tokens": 13, "cache_creation_5m": 0, "cache_creation_1h": 11}


def _insert_usage(db, ts, cost):
    with db.begin() as c:
        c.execute(text("INSERT INTO llm_usage (ts, workload, model, cost_usd) VALUES (:t, 'live_extract', "
                       "'claude-haiku-5-5', :c)"), {"t": ts, "c": cost})


def test_daily_cap_blocks_and_logs_once(db):
    now = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
    m = CostMeter(daily_cap=1.0, monthly_cap=15.0, clock=lambda: now)
    _insert_usage(db, now - timedelta(hours=1), 0.9995)
    _insert_usage(db, now - timedelta(days=1), 5.0)          # yesterday: doesn't count toward today
    with pytest.raises(CapReached) as e:
        m.reserve("live_extract", "claude-haiku-5-5", prompt_chars=12_000, max_tokens=2800)
    assert e.value.kind == "daily" and e.value.until == datetime(2026, 11, 15, tzinfo=timezone.utc)
    with pytest.raises(CapReached):
        m.reserve("live_extract", "claude-haiku-5-5", 12_000, 2800)
    with db.connect() as c:
        events = c.execute(text("SELECT detail->>'cap' FROM pipeline_events WHERE kind='llm_cap_hit'")).all()
    assert [e[0] for e in events] == ["daily"]                # logged once per period
    assert m.state()["degraded"] == "daily" or m.state()["spent_today_usd"] < 1.0


def test_monthly_cap_and_reservations(db):
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    m = CostMeter(daily_cap=100.0, monthly_cap=15.0, clock=lambda: now)
    _insert_usage(db, month_start(now) + timedelta(hours=3), 14.999)
    _insert_usage(db, month_start(now) - timedelta(hours=1), 50.0)   # last month
    with pytest.raises(CapReached) as e:
        m.reserve("live_extract", "claude-haiku-5-5", 12_000, 2800)
    assert e.value.kind == "monthly" and e.value.until == next_month(now)


def test_inflight_reservations_count(db):
    now = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
    m = CostMeter(daily_cap=0.005, monthly_cap=15.0, clock=lambda: now)
    r1 = m.reserve("live_extract", "claude-haiku-5-5", 12_000, 2800)   # ~0.0026 worst case
    with pytest.raises(CapReached):
        m.reserve("live_extract", "claude-haiku-5-5", 12_000, 2800)     # would exceed with r1 in flight
    rid, cost = m.settle(r1, usage={"input_tokens": 100, "output_tokens": 100}, items=1, stop_reason="end_turn")
    assert cost == pytest.approx((100 * 0.10 + 100 * 0.50) / 1e6)
    m.reserve("live_extract", "claude-haiku-5-5", 12_000, 2800)         # room again after settling


def test_extractor_degrades_to_rules_when_capped(db):
    from extract.claude import ClaudeExtractor
    from tests.mock_anthropic import MockAnthropic
    m = CostMeter(daily_cap=0.0, monthly_cap=15.0)
    client = MockAnthropic()
    out = ClaudeExtractor(client, m).extract([{"id": "a", "transcript": "Engine 5 respond to 7 Mile and Evergreen"}])
    assert out.fallback_reason == "cap:daily" and client.calls == []
