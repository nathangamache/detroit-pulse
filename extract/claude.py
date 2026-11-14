"""One batched, structured Claude Haiku 5.5 call per micro-batch (eval "Config C").

Request shape (no temperature/top_p/top_k: the current API version rejects them):

    model           claude-haiku-5-5
    system          [static instructions + few-shots, cache_control ephemeral ttl 1h]
    messages        [user: <transmission id=...> blocks for up to 10 transmissions]
    thinking        {"type": "disabled"}          (allowed on Haiku 5.5 at effort <= high)
    output_config   {"effort": "medium", "format": {"type": "json_schema", "schema": ...}}
    max_tokens      sized to the batch (structured items are ~120-200 tokens each)

Handling:
  * the cost meter reserves the worst case first (CapReached -> rules fallback)
  * stop_reason "refusal": no content is read; items fall back to rules (Haiku 5.5 has
    no server-side fallback, so the `fallbacks` parameter is never sent)
  * stop_reason "max_tokens": the batch is split in half and retried once
  * missing/duplicate ids: missing items fall back to rules; duplicates keep the first
  * spend-limit errors (workspace 400 "usage limits", 429 enforced_spend_limit_reached)
    stop all calls until next month; other API errors degrade only this batch
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.settings import get_settings
from extract import prompt as P
from extract.cost import CapReached, CostMeter
from extract.schema import ItemResult, output_format, parse_item

log = structlog.get_logger()

PER_ITEM_TOKENS = 260
BASE_TOKENS = 200


@dataclass
class BatchOutcome:
    results: dict[str, ItemResult] = field(default_factory=dict)
    fallback_ids: list[str] = field(default_factory=list)      # need the rules path
    fallback_reason: str | None = None
    usage_ids: list[int] = field(default_factory=list)
    cost_usd: float = 0.0
    calls: int = 0
    stop_reasons: list[str] = field(default_factory=list)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def max_tokens_for(n: int) -> int:
    return min(8000, BASE_TOKENS + PER_ITEM_TOKENS * n)


def make_client(api_key: str | None = None):
    """Real SDK client. Key comes from settings (.env), never logged."""
    import anthropic

    s = get_settings()
    key = api_key or (s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else "")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key, max_retries=s.llm_max_retries, timeout=s.llm_timeout_s)


def _is_spend_limit(e: Exception) -> bool:
    msg = (getattr(e, "message", "") or str(e)).lower()
    body = getattr(e, "body", None)
    code = ""
    if isinstance(body, dict):
        err = body.get("error") or {}
        code = str(err.get("error_code") or err.get("type") or "")
    return ("usage limit" in msg or "spend limit" in msg or "enforced_spend_limit" in msg
            or "enforced_spend_limit" in code)


class ClaudeExtractor:
    def __init__(self, client: Any, meter: CostMeter, workload: str = "live_extract"):
        self.client = client
        self.meter = meter
        self.workload = workload
        self.s = get_settings()

    def request_kwargs(self, items: list[dict]) -> dict:
        return {
            "model": self.s.llm_model,
            "max_tokens": max_tokens_for(len(items)),
            "system": P.system_blocks(),
            "messages": [{"role": "user", "content": P.user_message(items)}],
            "thinking": {"type": "disabled"},
            "output_config": {"effort": self.s.llm_effort, "format": output_format()},
        }

    def extract(self, items: list[dict], _depth: int = 0) -> BatchOutcome:
        """items: [{"id", "transcript", "context", "captured_at"}]. Never raises for API trouble."""
        out = BatchOutcome()
        if not items:
            return out
        if self.client is None:
            out.fallback_ids = [it["id"] for it in items]
            out.fallback_reason = "no_api_key"
            return out
        kwargs = self.request_kwargs(items)
        prompt_chars = len(P.system_text()) + len(kwargs["messages"][0]["content"])
        try:
            res = self.meter.reserve(self.workload, kwargs["model"], prompt_chars, kwargs["max_tokens"])
        except CapReached as e:
            out.fallback_ids = [it["id"] for it in items]
            out.fallback_reason = f"cap:{e.kind}"
            return out

        import anthropic

        t0 = time.perf_counter()
        resp = None
        err: str | None = None
        try:
            resp = self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            err = f"400: {getattr(e, 'message', str(e))}"[:500]
            if _is_spend_limit(e):
                self.meter.mark_spend_limit(err)
        except anthropic.RateLimitError as e:
            err = f"429: {getattr(e, 'message', str(e))}"[:500]
            if _is_spend_limit(e):
                self.meter.mark_spend_limit(err)
        except (anthropic.APIStatusError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            err = f"{type(e).__name__}: {getattr(e, 'message', str(e))}"[:500]
        latency = int((time.perf_counter() - t0) * 1000)
        out.calls += 1

        if resp is None:
            rid, cost = self.meter.settle(res, usage=None, items=len(items), latency_ms=latency, error=err)
            out.usage_ids.append(rid)
            out.fallback_ids = [it["id"] for it in items]
            out.fallback_reason = "api_error"
            log.warning("claude call failed; rules fallback for batch", error=err, items=len(items))
            return out

        stop = getattr(resp, "stop_reason", None)
        sd = getattr(resp, "stop_details", None)
        category = getattr(sd, "category", None) if sd is not None else None
        rid, cost = self.meter.settle(
            res, usage=resp.usage, served_model=getattr(resp, "model", None), items=len(items),
            stop_reason=stop, stop_category=category, request_id=getattr(resp, "_request_id", None),
            latency_ms=latency)
        out.usage_ids.append(rid)
        out.cost_usd += cost
        out.stop_reasons.append(str(stop))
        u = resp.usage
        out.cache_read_tokens += int(getattr(u, "cache_read_input_tokens", 0) or 0)
        out.cache_write_tokens += int(getattr(u, "cache_creation_input_tokens", 0) or 0)

        if stop == "refusal":
            # check stop_reason before reading content: a refusal's content may not match the schema
            log.warning("claude refused batch; rules fallback", category=category, items=len(items))
            out.fallback_ids = [it["id"] for it in items]
            out.fallback_reason = f"refusal:{category}"
            return out
        if stop == "max_tokens":
            if len(items) > 1 and _depth == 0:
                mid = len(items) // 2
                log.info("max_tokens on batch; splitting and retrying once", items=len(items))
                for part in (items[:mid], items[mid:]):
                    sub = self.extract(part, _depth=1)
                    out.results.update(sub.results)
                    out.fallback_ids += sub.fallback_ids
                    out.fallback_reason = out.fallback_reason or sub.fallback_reason
                    out.usage_ids += sub.usage_ids
                    out.cost_usd += sub.cost_usd
                    out.calls += sub.calls
                    out.stop_reasons += sub.stop_reasons
                    out.cache_read_tokens += sub.cache_read_tokens
                    out.cache_write_tokens += sub.cache_write_tokens
                return out
            out.fallback_ids = [it["id"] for it in items]
            out.fallback_reason = "max_tokens"
            return out

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            parsed = json.loads(text)["results"]
        except (ValueError, KeyError, TypeError) as e:
            log.warning("unparseable structured output; rules fallback", error=str(e)[:200])
            out.fallback_ids = [it["id"] for it in items]
            out.fallback_reason = "parse_error"
            return out
        wanted = {it["id"] for it in items}
        for d in parsed:
            if not isinstance(d, dict):
                continue
            item = parse_item(d)
            if item.id in wanted and item.id not in out.results:
                out.results[item.id] = item
        missing = [it["id"] for it in items if it["id"] not in out.results]
        if missing:
            log.warning("batch result missing ids; rules fallback for those", missing=missing)
            out.fallback_ids += missing
            out.fallback_reason = out.fallback_reason or "missing_ids"
        return out
