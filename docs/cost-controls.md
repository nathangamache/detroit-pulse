# Cost controls

Pulse's only paid dependency is the Claude API (Haiku 5.5) for extraction. Everything
else (Whisper, geocoding, tiles) is $0. Budget target: about $10-20/month.

## Prices

`config/llm_prices.yaml` (version `2026-11-13`), USD per million tokens for prompts up to
100K tokens: input $0.10, output $0.50, cache read $0.01, 5-minute cache write $0.125,
1-hour cache write $0.20. Prompts over 100K tokens bill 5x (Pulse's are ~8K). Unknown
models are priced at the most expensive row. Every `llm_usage` row records the
`price_version` it was costed with.

## Measured: one real batch (2026-11-14 smoke test)

10 eval transmissions through `extract/claude.py` (`scripts/eval_extract.py --real --limit 10`):

| | tokens | cost |
|---|---|---|
| input (uncached) | 1,042 | $0.000104 |
| output | 1,521 | $0.000761 |
| 1-hour cache write (system prompt + few-shots) | 6,846 | $0.001369 |
| **total** | | **$0.0022339** |

Latency 7.5 s, `stop_reason` end_turn, all 10 ids returned. A warm batch reads the
6,846-token prefix for $0.000068 instead of writing it, so steady state is about
**$0.00093 per 10 transmissions**. Output dominates (~150 tokens per item for the full
schema), which is why this is ~3.6x the location-only eval Config C ($0.026 per 1K
chunks). The first attempt was rejected with a 400 before any tokens were billed (schema
had 17 union-typed fields, limit 16); see `extract/schema.py`.

## Projection (30-day month)

| Volume | After prefilter (~11% skipped) | Claude extraction | Summaries on close | Total |
|---|---|---|---|---|
| 1,900 transmissions/day (old pipeline's average) | ~1,690 | ~$4.7 | ~$0.1 | **~$5** |
| 6,000/day | ~5,340 | ~$15 | ~$0.3 | ~$15 (the monthly cap binds near month end) |
| 19,500/day (every channel busy 24/7) | ~17,350 | ~$48 uncapped | ~$1 | $15: the $1 daily cap stops Claude after ~10,700 items/day and the monthly cap mid-month; the rest is rules-only |

Assumptions: steady-state batch cost above; 1-hour cache writes only after idle gaps over
an hour (a read refreshes the TTL), a few per day; summaries use Claude only for
incidents with 3+ transmissions (6.7% of incidents in the archive). SDR files are single
transmissions and shorter than the old 25 s chunks the smoke test used, so real input
and output per item should be lower; the prefilter skip rate on real per-transmission
audio should be higher than the 11% measured on sentence-split archive text. Measure
with `GET /admin/llm/usage` after the first week.

## Guards

1. **Prefilter** (`extract/prefilter.py`): chatter, Whisper hallucinations and parsed
   detector readouts never reach Claude.
2. **Batching + caching**: 10 transmissions per call, one static cached prefix (1-hour
   TTL because at low volume batches arrive more than 5 minutes apart).
3. **Bounded summaries** (`extract/summary.py`): live summaries are the per-transmission
   `short_summary` from the batch call (no extra tokens). One summary per incident on
   close (`SUMMARY_MODE=on_close`), reading at most the last 15 short summaries, and only
   via Claude for incidents with 3+ notes; otherwise a template. The old pipeline
   re-summarised the whole incident every 3rd chunk (quadratic in incident length).
4. **Meter + caps** (`extract/cost.py`): before each call the worst case (prompt
   chars / 2 tokens at the 1-hour write rate, plus `max_tokens` at the output rate) is
   reserved. The call is refused when
   * today's spend + in-flight reservations + this call > `LLM_DAILY_CAP_USD` ($1.00), or
   * this month's spend + in-flight + this call > `LLM_MONTHLY_CAP_USD` ($15.00).
   Spend is summed from `llm_usage` (UTC day / UTC month), so caps survive restarts.
5. **Spend-limit errors** from Anthropic (workspace 400 "usage limits", 429
   `enforced_spend_limit_reached`) stop all calls until the next UTC month.
6. **Degradation**: a refused or failed call never drops data. Those items go through
   the rules extractor (`extract/rules.py`) and are stored with
   `extract_source='rules'` and a `fallback:<reason>` flag; the first cap hit per period
   writes an `llm_cap_hit` row to `pipeline_events` and a warning log.
7. **Off switch**: `LLM_ENABLED=false` or an empty `ANTHROPIC_API_KEY` means rules-only.

Recommended backstop outside Pulse: a Console workspace for Pulse with its own key and a
monthly spend limit a little above `LLM_MONTHLY_CAP_USD`.

## Levers if spend runs high

* Drop `location.raw` and cap `short_summary` at ~12 words (output is ~80% of the cost).
* `LLM_EFFORT=low` (measure quality on the eval first).
* Larger `LLM_BATCH_SIZE` (fewer prefix reads; risk of `max_tokens` splits).
* More aggressive prefilter (check recall with `scripts/measure_prefilter.py`).
* Lower `LLM_DAILY_CAP_USD`: past the cap Pulse keeps working on rules.

## Checking spend

* `GET /admin/llm/usage?days=30` (admin token) or the admin panel's COST tab.
* SQL: `SELECT date_trunc('day', ts), workload, sum(cost_usd) FROM llm_usage GROUP BY 1, 2 ORDER BY 1 DESC;`
