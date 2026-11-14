#!/usr/bin/env python3
"""Re-run eval "Config C" through the production extraction code path (extract.claude).

    # no network, no spend: the batch path end to end with a mock client
    uv run python scripts/eval_extract.py --eval /path/normalize_eval.jsonl --mock rules
    uv run python scripts/eval_extract.py --eval /path/normalize_eval.jsonl --mock gold

    # ONE real batch of 10 rows, hard-capped by its own meter (default $0.25 all-time for 'smoke')
    uv run python scripts/eval_extract.py --eval /path/normalize_eval.jsonl --real --limit 10

Mock modes:
  rules  the mock "model" answers with the rules-only extractor -> a $0 baseline and a full
         exercise of batching (10/call), structured parsing, id matching and scoring
  gold   the mock answers with the gold label converted to structured fields -> checks the
         structured->string conversion and the scorer (should be ~1.0)
Scoring uses the verbatim lora/eval_baseline.py functions (scripts/eval_scoring.py) after
converting each structured location back to the old "<address>, <City>, MI" string. When
the model leaves city null, the eval fills the old feed's city (what the geocoder bias
does in production); --no-city-fill disables that.

Prints aggregate metrics only. The eval rows are real transcripts and stay local.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import eval_scoring as S  # noqa: E402

from extract import rules  # noqa: E402
from extract.claude import ClaudeExtractor, make_client  # noqa: E402
from extract.cost import CapReached, CostMeter  # noqa: E402
from extract.legacy import parse_legacy, to_legacy  # noqa: E402

# Channel context for the OLD Broadcastify feed ids found in the eval rows (public descriptions).
FEED_CONTEXT = {
    "wayneco_detroit_police_fire": ({"channel": "Detroit police and fire", "kind": "public_safety",
                                     "area": "Detroit, Wayne County"}, "Detroit"),
    "wayneco_detroit_police_dispatch": ({"channel": "Detroit police dispatch", "kind": "police",
                                         "area": "Detroit, Wayne County"}, "Detroit"),
    "wayneco_detroit_fire": ({"channel": "Detroit fire", "kind": "fire", "area": "Detroit, Wayne County"}, "Detroit"),
    "wayneco_detroit_ems": ({"channel": "Detroit EMS", "kind": "ems", "area": "Detroit, Wayne County"}, "Detroit"),
    "wayneco_public_safety": ({"channel": "Wayne County public safety", "kind": "public_safety",
                               "area": "Wayne County"}, None),
    "wayneco_plymouthnorthville": ({"channel": "Plymouth/Northville fire and EMS", "kind": "fire",
                                    "area": "Plymouth and Northville, Wayne County"}, None),
    "wayneco_northville_plymouth_city": ({"channel": "Northville/Plymouth city police and fire",
                                          "kind": "public_safety", "area": "Northville and Plymouth, Wayne County"},
                                         None),
}


def transcript_of(text: str) -> str:
    return text.split("<|im_start|>user\n")[1].split("<|im_end|>")[0].replace("Transmission: ", "").strip()


def load_eval(path: str) -> list[dict]:
    out = []
    for i, line in enumerate(open(path)):
        r = json.loads(line)
        ctx, city = FEED_CONTEXT.get(r.get("feed_id", ""), ({"channel": r.get("feed_id", "")}, None))
        out.append({"id": f"e{i:03d}", "transcript": transcript_of(r["text"]), "gold": r["label"],
                    "context": ctx, "default_city": city,
                    "captured_at": datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)})
    return out


def _item_dict(res) -> dict:
    d = res.to_dict()
    d["location"] = asdict(res.location)
    return {k: d[k] for k in ("id", "has_location", "location", "incident_type", "priority", "units",
                              "is_rail", "rail", "short_summary")}


def rules_responder(rows_by_id):
    def f(tid, transcript):
        return _item_dict(rules.extract(tid, rows_by_id[tid]["transcript"]))
    return f


def gold_responder(rows_by_id):
    def f(tid, transcript):
        loc = parse_legacy(rows_by_id[tid]["gold"])
        res = rules.extract(tid, rows_by_id[tid]["transcript"])
        res.location = loc
        res.has_location = loc.usable
        return _item_dict(res)
    return f


class SmokeMeter(CostMeter):
    """Caps the real smoke run by ALL-TIME spend on workload 'smoke' (default $0.25)."""

    def __init__(self, max_usd: float, **kw):
        super().__init__(**kw)
        self.max_usd = max_usd

    def smoke_spent(self) -> float:
        from sqlalchemy import text
        from core.db import get_engine
        with get_engine().connect() as c:
            return float(c.execute(text("SELECT coalesce(sum(cost_usd),0) FROM llm_usage WHERE workload='smoke'"))
                         .scalar() or 0)

    def reserve(self, workload, model, prompt_chars, max_tokens):
        worst = self.prices.worst_case(model, prompt_chars, max_tokens)
        spent = self.smoke_spent()
        if spent + worst > self.max_usd:
            raise CapReached("smoke", f"smoke spend ${spent:.4f} + worst case ${worst:.4f} > ${self.max_usd}")
        print(f"[smoke meter] all-time smoke spend ${spent:.6f}; reserving worst case ${worst:.6f} "
              f"(cap ${self.max_usd})")
        return super().reserve(workload, model, prompt_chars, max_tokens)


def run(rows, extractor, batch_size, city_fill=True):
    preds, golds = [], []
    stats = {"calls": 0, "fallback": 0, "cost_usd": 0.0, "cache_read": 0, "cache_write": 0, "stop_reasons": {}}
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        out = extractor.extract([{k: r[k] for k in ("id", "transcript", "context", "captured_at")} for r in batch])
        stats["calls"] += out.calls
        stats["cost_usd"] += out.cost_usd
        stats["cache_read"] += out.cache_read_tokens
        stats["cache_write"] += out.cache_write_tokens
        for sr in out.stop_reasons:
            stats["stop_reasons"][sr] = stats["stop_reasons"].get(sr, 0) + 1
        for r in batch:
            res = out.results.get(r["id"])
            if res is None:
                stats["fallback"] += 1
                res = rules.extract(r["id"], r["transcript"])
            preds.append(to_legacy(res.location, r["default_city"] if city_fill else None))
            golds.append(r["gold"])
    return preds, golds, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", required=True)
    ap.add_argument("--mock", choices=["rules", "gold"])
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--max-usd", type=float, default=0.25)
    ap.add_argument("--no-city-fill", action="store_true")
    a = ap.parse_args()
    rows = load_eval(a.eval)
    if a.limit:
        rows = rows[: a.limit]
    by_id = {r["id"]: r for r in rows}

    if a.mock:
        from tests.mock_anthropic import MockAnthropic
        client = MockAnthropic(rules_responder(by_id) if a.mock == "rules" else gold_responder(by_id))

        class NullMeter:
            prices = None

            def reserve(self, *a_, **k):
                return None

            def settle(self, *a_, **k):
                return 0, 0.0

            def mark_spend_limit(self, *a_):
                pass

        ex = ClaudeExtractor(client, NullMeter(), workload="eval")
        preds, golds, stats = run(rows, ex, a.batch_size, not a.no_city_fill)
        calls = client.calls
        shape = {k: v for k, v in calls[0].items() if k not in ("system", "messages")} if calls else {}
        shape["output_config"] = {"effort": shape["output_config"]["effort"], "format": "json_schema(BATCH_SCHEMA)"}
        print(json.dumps({"mode": f"mock-{a.mock}", "rows": len(rows), "api_calls": len(calls),
                          "request_shape": shape, "metrics": S.aggregate(preds, golds),
                          "fallback_items": stats["fallback"]}, indent=1))
        return

    if a.real:
        if len(rows) > a.batch_size:
            sys.exit("--real runs exactly ONE batch; pass --limit <= --batch-size")
        client = make_client()
        if client is None:
            sys.exit("ANTHROPIC_API_KEY not configured; nothing sent")
        meter = SmokeMeter(a.max_usd, daily_cap=1.0, monthly_cap=15.0)
        ex = ClaudeExtractor(client, meter, workload="smoke")
        preds, golds, stats = run(rows, ex, a.batch_size, not a.no_city_fill)
        print(json.dumps({"mode": "real", "rows": len(rows), "metrics": S.aggregate(preds, golds),
                          "stats": stats, "smoke_spend_all_time_usd": round(meter.smoke_spent(), 6)}, indent=1))
        for p, g, r in zip(preds, golds, rows):
            print(f"  {r['id']}  {'OK ' if S.exact_match(p, g) else 'DIFF'}  pred={p!r}  gold={g!r}")
        return
    sys.exit("choose --mock rules|gold or --real")


if __name__ == "__main__":
    main()
