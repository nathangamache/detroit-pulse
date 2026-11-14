"""Extraction stage: prefilter -> micro-batch -> one Claude call -> geocode -> correlate -> store.

Micro-batching: transcribed transmissions that pass the prefilter wait in memory (their
rows are claimed as `extracting`, so a crash only delays them) until LLM_BATCH_SIZE (10)
are waiting or the oldest has waited LLM_BATCH_MAX_WAIT_S (45 s); then ONE structured
Claude call handles them all. Prefiltered chatter, Whisper noise and fully parsed
detector readouts are finished immediately with no API call.

Every transmission ends in status `done` with its extraction stored, whatever happened
upstream: Claude refusals, errors and cap hits fall back to the rules extractor and are
recorded in extract_source ("claude" | "rules" | "prefilter").
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

import structlog
from sqlalchemy import text

from core import bus
from core.config_files import Channel, channel_by_id, default_bias, load_areas
from core.db import get_engine
from core.settings import get_settings
from core.workqueue import ST_EXTRACTING, ST_TRANSCRIBED, claim
from correlation import engine as corr
from extract import rules
from extract.claude import ClaudeExtractor, make_client
from extract.cost import CostMeter
from extract.prefilter import PrefilterResult, classify
from extract.prompt import channel_context
from extract.schema import ItemResult
from extract.summary import summarize_incident
from geocode.base import GeoQuery, GeoResult
from geocode.chain import Geocoder, get_geocoder
from rail import detector as det

log = structlog.get_logger()


def _bias(channel: Channel | None) -> tuple[float, float]:
    if channel and channel.bias_lat is not None and channel.bias_lon is not None:
        return channel.bias_lat, channel.bias_lon
    if channel and channel.area_id:
        for a in load_areas():
            if a.id == channel.area_id:
                return a.lat, a.lon
    return default_bias()


def _merge_detector(partial: det.DetectorReading | None, item: ItemResult | None) -> tuple[det.DetectorReading | None, str]:
    """Rules first; fill gaps from the LLM's rail.detector (the fallback)."""
    llm = (item.rail or {}).get("detector") if item and item.rail else None
    if partial is None and not llm:
        return None, "rules"
    r = partial or det.DetectorReading()
    src = "rules"
    if llm:
        if r.result == "unknown" and llm.get("result") in ("no_defects", "alarm", "integrity_failure"):
            r.result, src = llm["result"], "llm"
        for k in ("track", "direction", "axle_count", "speed_mph", "temperature_f"):
            if getattr(r, k) is None and llm.get(k) not in (None, ""):
                setattr(r, k, llm[k])
                src = "llm"
        if not r.alarms and llm.get("alarms"):
            r.alarms = [det.Alarm(a) for a in llm["alarms"]]
            src = "llm"
        if r.result == "alarm" and not r.alarms:
            r.alarms = [det.Alarm("other")]
    if r.milepost is None and item and item.rail and item.rail.get("milepost"):
        try:
            r.milepost = float(item.rail["milepost"])
            src = "llm"
        except ValueError:
            pass
    if item and item.rail and not r.railroad:
        r.railroad = item.rail.get("railroad")
    if r.integrity_failure and r.result == "unknown":
        r.result = "integrity_failure"
    return r, src


class ExtractWorker:
    def __init__(self, client=None, meter: CostMeter | None = None, geocoder: Geocoder | None = None,
                 summary_client=None):
        self.s = get_settings()
        if client is None and self.s.llm_enabled:
            client = make_client()
        self.client = client if self.s.llm_enabled else None
        self.meter = meter or CostMeter()
        self.extractor = ClaudeExtractor(self.client, self.meter)
        self.geocoder = geocoder or get_geocoder()
        self.summary_client = summary_client if summary_client is not None else self.client
        self.pending: list[tuple[dict, PrefilterResult]] = []
        self.stats = {"prefiltered": 0, "llm_items": 0, "rules_items": 0, "batches": 0}

    # ── loop ──────────────────────────────────────────────────────────────
    def poll(self, force_flush: bool = False) -> int:
        """Claim new work, then finish everything that is ready, in capture order.

        Prefiltered items need no API call, but they still wait behind any earlier item that
        is waiting for a batch, so "Engine 4 on scene" is never correlated before the dispatch
        that created the incident.
        """
        for row in claim(ST_TRANSCRIBED, ST_EXTRACTING, limit=50):
            ch = channel_by_id(row.get("channel_id") or "")
            self.pending.append((row, classify(row.get("raw_transcript") or "", ch)))
        self.pending.sort(key=lambda x: x[0].get("captured_at") or datetime.now(timezone.utc))
        n = 0
        bs = max(1, self.s.llm_batch_size)
        while True:
            # leading items that need no LLM: finish now
            while self.pending and self.pending[0][1].skip_llm:
                n += self._finish_prefiltered(*self.pending.pop(0))
            llm_idx = [i for i, (_, pf) in enumerate(self.pending) if not pf.skip_llm]
            if len(llm_idx) >= bs:
                cut = llm_idx[bs - 1] + 1
            elif self.pending and (force_flush or self.oldest_wait_s() >= self.s.llm_batch_max_wait_s):
                cut = len(self.pending)
            else:
                break
            group, self.pending = self.pending[:cut], self.pending[cut:]
            n += self.flush(group)
        return n

    def _finish_prefiltered(self, row: dict, pf: PrefilterResult) -> int:
        self.stats["prefiltered"] += 1
        ch = channel_by_id(row.get("channel_id") or "")
        res = rules.extract("p", row.get("raw_transcript") or "", ch, pf.detector)
        res.source = "prefilter"
        if pf.label in ("chatter", "whisper_noise"):
            res.incident_type, res.priority = "NONE", "LOW"
            res.location.kind = "none"
            res.has_location = False
        self.finalize(row, res, pf, usage_id=None)
        return 1

    def oldest_wait_s(self) -> float:
        waiting = [r for r, pf in self.pending if not pf.skip_llm]
        if not waiting:
            return 0.0
        oldest = min((r.get("created_at") or datetime.now(timezone.utc)) for r in waiting)
        return (datetime.now(timezone.utc) - oldest).total_seconds()

    def flush(self, group: list[tuple[dict, PrefilterResult]]) -> int:
        """One Claude call for the group's LLM items; then finish the whole group in capture order."""
        llm = [(row, pf) for row, pf in group if not pf.skip_llm]
        items = []
        for i, (row, _pf) in enumerate(llm):
            cid = row.get("channel_id") or row.get("feed_id") or ""
            items.append({"id": f"t{i + 1}", "transcript": row.get("raw_transcript") or "",
                          "context": channel_context(channel_by_id(cid), cid), "captured_at": row.get("captured_at")})
        out = self.extractor.extract(items) if items else None
        if items:
            self.stats["batches"] += 1
        usage_id = out.usage_ids[-1] if out and out.usage_ids else None
        ids = {id(row): it["id"] for it, (row, _) in zip(items, llm)}
        for row, pf in group:
            if pf.skip_llm:
                self._finish_prefiltered(row, pf)
                continue
            ch = channel_by_id(row.get("channel_id") or "")
            res = out.results.get(ids[id(row)])
            if res is None:
                res = rules.extract(ids[id(row)], row.get("raw_transcript") or "", ch, pf.detector)
                res.flags.append(f"fallback:{out.fallback_reason}")
                self.stats["rules_items"] += 1
            else:
                self.stats["llm_items"] += 1
            self.finalize(row, res, pf, usage_id=usage_id)
        if out is not None:
            bus.publish_debug("extract", "batch", items=len(items), llm=len(out.results),
                              fallback=out.fallback_reason, cost_usd=round(out.cost_usd, 6))
        return len(group)

    # ── per transmission ──────────────────────────────────────────────────
    def finalize(self, row: dict, res: ItemResult, pf: PrefilterResult, usage_id: int | None) -> None:
        chunk_id = str(row["chunk_id"])
        channel_id = row.get("channel_id") or row.get("feed_id") or ""
        ch = channel_by_id(channel_id)
        transcript = row.get("raw_transcript") or ""
        captured_at = row.get("captured_at") or datetime.now(timezone.utc)
        if ch is not None and ch.is_rail:
            res.is_rail = True
            if res.rail is not None:
                res.rail.setdefault("railroad", ch.railroad)
                res.rail["railroad"] = res.rail.get("railroad") or ch.railroad
                res.rail["subdivision"] = res.rail.get("subdivision") or ch.subdivision

        geo = GeoResult.failed()
        if res.location.usable:
            loc = res.location
            geo = self.geocoder.geocode(GeoQuery(kind=loc.kind, street1=loc.street1, street2=loc.street2,
                                                 number=loc.number, city=loc.city or (ch.city if ch else None),
                                                 raw=loc.raw, bias=_bias(ch), county=ch.county if ch else None))
        inferred = None
        if not geo.found and res.has_incident and res.units and ch and (ch.unit_prefix or "").upper() == "DFD":
            try:
                from api.geo_enrichment import infer_location_from_units
                guess = infer_location_from_units(res.units)
                if guess and guess.get("lat"):
                    inferred = GeoResult(guess["lat"], guess["lng"], guess.get("station"), "LOW", "unit_station")
            except Exception:  # noqa: BLE001
                inferred = None

        reading = None
        reading_src = "rules"
        if pf.detector is not None or (res.rail or {}).get("reason_label") == "defect_detector":
            reading, reading_src = _merge_detector(pf.detector, res if res.source == "claude" else None)

        extraction = res.to_dict()
        extraction["location"] = asdict(res.location)
        extraction["prefilter"] = {"label": pf.label, "reason": pf.reason}
        if reading:
            extraction["detector"] = reading.to_dict()
        published = None
        with get_engine().begin() as c:
            c.execute(text("""
                UPDATE transcript_chunks SET status = 'done', extracted_at = now(), prefilter = :pf,
                    extract_source = :src, has_incident = :hi, has_location = :hl, loc_kind = :lk,
                    loc_street1 = :l1, loc_street2 = :l2, loc_number = :ln, loc_city = :lc, loc_raw = :lr,
                    incident_type = :it, priority = :pr, units = :units, is_rail = :rail, rail = CAST(:railj AS jsonb),
                    short_summary = :ss, extraction = CAST(:ex AS jsonb), llm_usage_id = :uid,
                    normalized_address = :na, lat = :lat, lng = :lng, location = ST_GeomFromEWKT(:ewkt),
                    geocode_source = :gs, geocode_confidence = :gc, error = NULL
                 WHERE chunk_id = :cid"""), {
                "pf": pf.label, "src": res.source, "hi": res.has_incident, "hl": res.has_location,
                "lk": res.location.kind, "l1": res.location.street1, "l2": res.location.street2,
                "ln": res.location.number, "lc": res.location.city, "lr": res.location.raw,
                "it": res.incident_type, "pr": res.priority, "units": res.units, "rail": res.is_rail,
                "railj": json.dumps(res.rail) if res.rail else None, "ss": res.short_summary,
                "ex": json.dumps(extraction, default=str), "uid": usage_id,
                "na": res.location.display() or "NO_LOCATION", "lat": geo.lat, "lng": geo.lng,
                "ewkt": corr.ewkt(geo.lat, geo.lng), "gs": geo.source if geo.found else "none",
                "gc": geo.confidence, "cid": chunk_id})
            if reading is not None:
                c.execute(text("""
                    INSERT INTO rail_detector_events (chunk_id, channel_id, ts, railroad, subdivision, detector_name,
                        milepost, track, direction, axle_count, speed_mph, temperature_f, result, alarms,
                        integrity_failure, parse_source, raw_text)
                    VALUES (:cid, :ch, :ts, :rr, :sub, :name, :mp, :tr, :dir, :ax, :sp, :temp, :res,
                            CAST(:al AS jsonb), :integ, :ps, :raw)"""), {
                    "cid": chunk_id, "ch": channel_id, "ts": captured_at,
                    "rr": reading.railroad or (ch.railroad if ch else None), "sub": ch.subdivision if ch else None,
                    "name": reading.detector_name, "mp": reading.milepost, "tr": reading.track,
                    "dir": reading.direction, "ax": reading.axle_count, "sp": reading.speed_mph,
                    "temp": reading.temperature_f, "res": reading.result,
                    "al": json.dumps([asdict(a) for a in reading.alarms]), "integ": reading.integrity_failure,
                    "ps": reading_src, "raw": transcript})
            d = corr.decide(c, res, channel_id=channel_id, channel=ch, transcript=transcript, geo=geo,
                            captured_at=captured_at)
            inc = corr.apply(c, d, res, chunk_id=chunk_id, channel_id=channel_id, channel=ch,
                             transcript=transcript, geo=geo, captured_at=captured_at, inferred=inferred)
            if inc is not None:
                published = (d.action, inc)
        if published:
            action, inc = published
            self._enrich(inc, action)
            event = {"NEW": "incident:new", "UPDATE": "incident:update", "RESOLVE": "incident:resolve"}[action]
            if action in ("RESOLVE", "UPDATE"):
                try:
                    summarize_incident(inc["incident_id"], "resolve" if action == "RESOLVE" else "update",
                                       client=self.summary_client, meter=self.meter)
                except Exception as e:  # noqa: BLE001 - a summary must never block storing the transmission
                    log.warning("summary failed", incident_id=inc["incident_id"][:8], error=str(e))
            bus.publish_event(event, inc)
        if reading is not None:
            bus.publish_event("rail:detector", {"chunk_id": chunk_id, "channel_id": channel_id,
                                                "ts": captured_at.isoformat(), **reading.to_dict()})

    def _enrich(self, inc: dict, action: str) -> None:
        """Precinct/battalion/nearest stations (fixes the old enrich_incident(lat, lng, units) TypeError)."""
        if inc.get("lat") is None or action not in ("NEW", "UPDATE"):
            return
        try:
            from api.geo_enrichment import enrich_incident
            e = enrich_incident({"lat": inc["lat"], "lng": inc["lng"], "incident_type": inc.get("incident_type")})
        except Exception as ex:  # noqa: BLE001
            log.warning("geo enrichment failed", error=str(ex))
            return
        if not (e.get("precinct") or e.get("battalion") or e.get("nearest_stations")):
            return
        with get_engine().begin() as c:
            c.execute(text("""UPDATE incidents SET precinct = :p, battalion = :b, nearest_stations = CAST(:n AS jsonb)
                              WHERE incident_id = :id"""),
                      {"p": e.get("precinct"), "b": e.get("battalion"),
                       "n": json.dumps(e.get("nearest_stations")) if e.get("nearest_stations") else None,
                       "id": inc["incident_id"]})
        inc.update({k: e.get(k) for k in ("precinct", "battalion", "nearest_stations") if e.get(k)})


def run_extract_worker(stop: threading.Event | None = None, worker: ExtractWorker | None = None) -> None:
    stop = stop or threading.Event()
    worker = worker or ExtractWorker()
    log.info("extract worker ready", llm=worker.client is not None, batch=worker.s.llm_batch_size,
             max_wait_s=worker.s.llm_batch_max_wait_s)
    while not stop.is_set():
        try:
            n = worker.poll()
        except Exception as e:  # noqa: BLE001 - keep the stage alive; rows are recovered by requeue_stale
            log.error("extract poll failed", error=str(e))
            n = 0
            time.sleep(2)
        if n == 0:
            wait = 5.0
            if worker.pending:
                wait = max(1.0, min(5.0, worker.s.llm_batch_max_wait_s - worker.oldest_wait_s()))
            bus.wait_for_ring("extract", wait)
