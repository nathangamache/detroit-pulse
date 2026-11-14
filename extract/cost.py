"""Claude cost meter: price table, llm_usage rows, daily/monthly caps, degradation.

Every API response's `usage` becomes one llm_usage row with its computed cost.
Before each call the meter reserves the call's WORST-CASE cost (generous input
estimate + max_tokens of output) and refuses if

    spent today + in-flight + this call  > LLM_DAILY_CAP_USD    (default $1)
    spent month + in-flight + this call  > LLM_MONTHLY_CAP_USD  (default $15)

or if Anthropic itself reported a spend limit (workspace 400 "usage limits" or a
429 enforced_spend_limit_reached), which stops calls until the next UTC month.
A refusal raises CapReached; the extractor then falls back to rules-only
extraction and a `llm_cap_hit` pipeline_event is logged once per period.
Periods are UTC days and UTC calendar months.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import structlog
import yaml
from sqlalchemy import text

from core.db import get_engine
from core.settings import get_settings

log = structlog.get_logger()


class CapReached(Exception):
    def __init__(self, kind: str, detail: str, until: datetime | None = None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.until = until


# ── Prices ──────────────────────────────────────────────────────────────────

@dataclass
class Prices:
    version: str
    models: dict[str, dict[str, float]]

    @classmethod
    def load(cls, path: Path | None = None) -> "Prices":
        path = Path(path or get_settings().llm_price_file)
        d = yaml.safe_load(path.read_text())
        return cls(version=str(d["price_version"]), models=d["models"])

    def rates(self, model: str) -> dict[str, float]:
        if model in self.models:
            return self.models[model]
        keys = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
        return {k: max(m.get(k, 0.0) for m in self.models.values()) for k in keys}

    def cost(self, model: str, u: dict[str, int]) -> float:
        r = self.rates(model)
        prompt = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        mult = 1.0
        if r.get("long_context_threshold") and prompt > r["long_context_threshold"]:
            mult = float(r.get("long_context_multiplier", 1.0))
        cw = u.get("cache_creation_input_tokens", 0)
        cw1h = u.get("cache_creation_1h", 0)
        cw5m = u.get("cache_creation_5m", 0)
        unattributed = max(0, cw - cw1h - cw5m)          # no TTL breakdown: price at the 1h (higher) rate
        usd = (u.get("input_tokens", 0) * r["input"]
               + u.get("output_tokens", 0) * r["output"]
               + u.get("cache_read_input_tokens", 0) * r["cache_read"]
               + cw5m * r["cache_write_5m"]
               + (cw1h + unattributed) * r["cache_write_1h"]) / 1e6
        return usd * mult

    def worst_case(self, model: str, prompt_chars: int, max_tokens: int) -> float:
        r = self.rates(model)
        est_in = prompt_chars // 2 + 64                    # deliberately generous (real: ~3 chars/token)
        mult = float(r.get("long_context_multiplier", 1.0)) if est_in > r.get("long_context_threshold", 1e12) else 1.0
        in_rate = max(r["input"], r["cache_write_1h"], r["cache_write_5m"])
        return (est_in * in_rate + max_tokens * r["output"]) / 1e6 * mult


def usage_dict(usage: Any) -> dict[str, int]:
    """Normalise an SDK `response.usage` object (or a dict) into flat token counts."""
    def g(o, k):
        return (o.get(k) if isinstance(o, dict) else getattr(o, k, None)) or 0
    cc = usage.get("cache_creation") if isinstance(usage, dict) else getattr(usage, "cache_creation", None)
    return {
        "input_tokens": int(g(usage, "input_tokens")),
        "output_tokens": int(g(usage, "output_tokens")),
        "cache_creation_input_tokens": int(g(usage, "cache_creation_input_tokens")),
        "cache_read_input_tokens": int(g(usage, "cache_read_input_tokens")),
        "cache_creation_5m": int(g(cc, "ephemeral_5m_input_tokens")) if cc else 0,
        "cache_creation_1h": int(g(cc, "ephemeral_1h_input_tokens")) if cc else 0,
    }


# ── Meter ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def day_start(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(t: datetime) -> datetime:
    return day_start(t).replace(day=1)


def next_month(t: datetime) -> datetime:
    m = month_start(t)
    return (m + timedelta(days=32)).replace(day=1)


@dataclass
class Reservation:
    id: str
    amount: float
    workload: str
    model: str


@dataclass
class CostMeter:
    daily_cap: float | None = None
    monthly_cap: float | None = None
    prices: Prices | None = None
    clock: Callable[[], datetime] = _utcnow
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _inflight: dict[str, float] = field(default_factory=dict)
    _hard_stop_until: datetime | None = None
    _logged: set[str] = field(default_factory=set)

    def __post_init__(self):
        s = get_settings()
        self.daily_cap = s.llm_daily_cap_usd if self.daily_cap is None else self.daily_cap
        self.monthly_cap = s.llm_monthly_cap_usd if self.monthly_cap is None else self.monthly_cap
        self.prices = self.prices or Prices.load()

    # spend so far, from the durable table (so caps survive restarts)
    def spent_since(self, since: datetime) -> float:
        with get_engine().connect() as c:
            v = c.execute(text("SELECT coalesce(sum(cost_usd), 0) FROM llm_usage WHERE ts >= :t"),
                          {"t": since}).scalar()
        return float(v or 0)

    def spent_today(self) -> float:
        return self.spent_since(day_start(self.clock()))

    def spent_month(self) -> float:
        return self.spent_since(month_start(self.clock()))

    def reserve(self, workload: str, model: str, prompt_chars: int, max_tokens: int) -> Reservation:
        amount = self.prices.worst_case(model, prompt_chars, max_tokens)
        now = self.clock()
        with self._lock:
            if self._hard_stop_until and now < self._hard_stop_until:
                self._cap_event("spend_limit", now, "Anthropic reported a spend limit", self._hard_stop_until)
                raise CapReached("spend_limit", "Anthropic spend limit reached", self._hard_stop_until)
            inflight = sum(self._inflight.values())
            today, month = self.spent_today(), self.spent_month()
            if today + inflight + amount > self.daily_cap:
                until = day_start(now) + timedelta(days=1)
                self._cap_event("daily", now, f"today ${today:.4f} + in-flight ${inflight:.4f} + "
                                f"call ${amount:.4f} > cap ${self.daily_cap:.2f}", until)
                raise CapReached("daily", f"daily cap ${self.daily_cap:.2f} reached", until)
            if month + inflight + amount > self.monthly_cap:
                until = next_month(now)
                self._cap_event("monthly", now, f"month ${month:.4f} + in-flight ${inflight:.4f} + "
                                f"call ${amount:.4f} > cap ${self.monthly_cap:.2f}", until)
                raise CapReached("monthly", f"monthly cap ${self.monthly_cap:.2f} reached", until)
            rid = uuid.uuid4().hex
            self._inflight[rid] = amount
        return Reservation(rid, amount, workload, model)

    def release(self, res: Reservation) -> None:
        with self._lock:
            self._inflight.pop(res.id, None)

    def settle(self, res: Reservation, *, usage: Any | None, served_model: str | None = None,
               items: int = 0, stop_reason: str | None = None, stop_category: str | None = None,
               request_id: str | None = None, latency_ms: int | None = None,
               error: str | None = None) -> tuple[int, float]:
        """Record one response (or failed call) in llm_usage. Returns (row id, cost)."""
        u = usage_dict(usage) if usage is not None else {k: 0 for k in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
            "cache_creation_5m", "cache_creation_1h")}
        model_for_price = served_model or res.model
        cost = self.prices.cost(model_for_price, u)
        try:
            with get_engine().begin() as c:
                rid = c.execute(text("""
                    INSERT INTO llm_usage (ts, workload, model, served_model, items, input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_creation_5m, cache_creation_1h,
                        cache_read_input_tokens, stop_reason, stop_category, request_id, latency_ms,
                        cost_usd, price_version, error)
                    VALUES (:ts, :w, :m, :sm, :n, :i, :o, :cc, :c5, :c1, :cr, :sr, :sc, :rq, :lat, :cost, :pv, :err)
                    RETURNING id"""), {
                    "ts": self.clock(), "w": res.workload, "m": res.model, "sm": served_model, "n": items,
                    "i": u["input_tokens"], "o": u["output_tokens"], "cc": u["cache_creation_input_tokens"],
                    "c5": u["cache_creation_5m"], "c1": u["cache_creation_1h"],
                    "cr": u["cache_read_input_tokens"], "sr": stop_reason, "sc": stop_category,
                    "rq": request_id, "lat": latency_ms, "cost": round(cost, 8),
                    "pv": self.prices.version, "err": (error or None) and error[:1000],
                }).scalar()
        finally:
            self.release(res)
        return int(rid), cost

    def mark_spend_limit(self, detail: str) -> None:
        now = self.clock()
        with self._lock:
            self._hard_stop_until = next_month(now)
        self._cap_event("spend_limit", now, detail, self._hard_stop_until)

    def _cap_event(self, kind: str, now: datetime, detail: str, until: datetime | None) -> None:
        period = now.strftime("%Y-%m-%d") if kind == "daily" else now.strftime("%Y-%m")
        key = f"{kind}:{period}"
        if key in self._logged:
            return
        self._logged.add(key)
        log.warning("LLM cap reached: degrading to rules-only extraction", kind=kind, detail=detail,
                    until=until.isoformat() if until else None)
        try:
            import json
            with get_engine().begin() as c:
                c.execute(text("INSERT INTO pipeline_events (kind, detail) VALUES ('llm_cap_hit', CAST(:d AS jsonb))"),
                          {"d": json.dumps({"cap": kind, "detail": detail,
                                            "until": until.isoformat() if until else None})})
        except Exception as e:  # noqa: BLE001
            log.warning("could not record cap event", error=str(e))

    def state(self) -> dict:
        now = self.clock()
        today, month = self.spent_today(), self.spent_month()
        degraded = None
        if self._hard_stop_until and now < self._hard_stop_until:
            degraded = "spend_limit"
        elif today >= self.daily_cap:
            degraded = "daily"
        elif month >= self.monthly_cap:
            degraded = "monthly"
        return {"spent_today_usd": round(today, 6), "spent_month_usd": round(month, 6),
                "daily_cap_usd": self.daily_cap, "monthly_cap_usd": self.monthly_cap,
                "degraded": degraded, "price_version": self.prices.version}
