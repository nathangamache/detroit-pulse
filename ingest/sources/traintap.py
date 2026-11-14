"""`traintap` source: tail trickv/traintap's packet CSV into rail_telemetry.

traintap (`traintap --csv trains.csv`) appends one row per BCH-valid packet with
columns (traintap/output.py CSV_COLUMNS):

    timestamp, epoch, source, freq_hz, valid, corrected, unit_addr, pressure_psig,
    batt_charge_pct, batt_cond, message_type, arm_status, motion, marker_light,
    turbine, valve_ckt, data_block, checkbits_rx

and touches a heartbeat file about once a minute (`heartbeat` next to the CSV, or
`--heartbeat FILE`; the container stack writes RECEIVER_STATUS_DIR/traintap-eot.heartbeat,
which /v1/feeds/status reads, see core/receivers.py). If a run starts with
a different header, traintap renames the old file to trains.csv.N and starts a
new one; we follow the path (inode change => read from the top).

Each packet becomes one rail_telemetry row (deduplicated on epoch/source/unit/
data block) with an interpreted status from rail/eot.py.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import text

from core import bus
from core.db import get_engine
from core.settings import get_settings
from ingest.sources.base import Source
from rail.eot import EotReading, interpret

log = structlog.get_logger()


def _b(v: str | None) -> bool | None:
    if v is None or v == "":
        return None
    return v.strip() in ("1", "true", "True")


def _i(v: str | None) -> int | None:
    if v is None or v.strip() == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def parse_row(row: dict) -> dict | None:
    if row.get("valid") not in ("1", 1, "true", "True"):
        return None                                   # only BCH-valid packets
    try:
        epoch = float(row["epoch"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "ts": datetime.fromtimestamp(epoch, tz=timezone.utc),
        "epoch": epoch,
        "source": (row.get("source") or "EOT").upper(),
        "freq_hz": _i(row.get("freq_hz")),
        "unit_id": _i(row.get("unit_addr")),
        "brake_pipe_psig": _i(row.get("pressure_psig")),
        "batt_charge_pct": _i(row.get("batt_charge_pct")),
        "batt_cond": row.get("batt_cond") or None,
        "message_type": row.get("message_type") or None,
        "arm_status": row.get("arm_status") or None,
        "motion": _b(row.get("motion")),
        "marker_light": _b(row.get("marker_light")),
        "turbine": _b(row.get("turbine")),
        "valve_ckt": _b(row.get("valve_ckt")),
        "bch_corrected": _i(row.get("corrected")) or 0,
        "data_block": row.get("data_block") or None,
    }


class TraintapSource(Source):
    name = "traintap"

    def __init__(self, csv_path: Path | None = None, channel: str | None = None):
        s = get_settings()
        self.path = Path(csv_path or s.traintap_csv)
        self.channel = channel or s.traintap_channel
        self._inode: int | None = None
        self._offset = 0
        self._header: list[str] | None = None
        self._last: dict[tuple[str, int | None], EotReading] = {}
        self.ingested = 0
        self.last_packet: datetime | None = None

    def heartbeat_age_s(self) -> float | None:
        """Age of traintap's heartbeat: RECEIVER_STATUS_DIR/traintap-eot.heartbeat (the container
        stack's `--heartbeat`) or `heartbeat` next to the CSV (traintap's default)."""
        now, ages = datetime.now(timezone.utc).timestamp(), []
        for hb in (get_settings().receiver_status_dir / "traintap-eot.heartbeat", self.path.parent / "heartbeat"):
            try:
                ages.append(now - hb.stat().st_mtime)
            except OSError:
                pass
        return min(ages) if ages else None

    def poll_once(self) -> int:
        try:
            st = self.path.stat()
        except OSError:
            return 0
        if self._inode != st.st_ino or st.st_size < self._offset:
            self._inode, self._offset, self._header = st.st_ino, 0, None
        if st.st_size == self._offset:
            return 0
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        # only consume complete lines; a half-written last line waits for the next poll
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return 0
        complete = chunk[: last_nl + 1]
        self._offset += len(complete)
        lines = complete.decode("utf-8", "replace").splitlines()
        if self._header is None and lines:
            self._header = next(csv.reader([lines[0]]))
            lines = lines[1:]
        rows = csv.DictReader(io.StringIO("\n".join(lines)), fieldnames=self._header)
        return self._store([r for r in (parse_row(x) for x in rows) if r])

    def _store(self, pkts: list[dict]) -> int:
        n = 0
        with get_engine().begin() as c:
            for p in pkts:
                key = (p["source"], p["unit_id"])
                reading = EotReading(ts=p["ts"], unit_id=p["unit_id"], brake_pipe_psig=p["brake_pipe_psig"],
                                     motion=p["motion"], source=p["source"], message_type=p["message_type"])
                prev = self._last.get(key)
                if prev is None and p["unit_id"] is not None:
                    prev = self._previous_from_db(c, p["source"], p["unit_id"], p["ts"])
                st = interpret(reading, prev)
                self._last[key] = reading
                dedupe = f"{p['epoch']:.3f}:{p['source']}:{p['unit_id']}:{p['data_block']}"
                raw = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in p.items()}
                res = c.execute(text("""
                    INSERT INTO rail_telemetry
                      (ts, channel, source, freq_hz, unit_id, brake_pipe_psig, motion, marker_light,
                       emergency, status, arm_status, message_type, batt_cond, batt_charge_pct,
                       turbine, valve_ckt, valid, bch_corrected, data_block, raw, dedupe_key)
                    VALUES (:ts, :ch, :src, :freq, :unit, :psig, :motion, :marker, :emerg, :status,
                            :arm, :mt, :bc, :bp, :turb, :valve, true, :corr, :db, CAST(:raw AS jsonb), :key)
                    ON CONFLICT (dedupe_key) DO NOTHING"""), {
                    "ts": p["ts"], "ch": self.channel, "src": p["source"], "freq": p["freq_hz"],
                    "unit": p["unit_id"], "psig": p["brake_pipe_psig"], "motion": p["motion"],
                    "marker": p["marker_light"], "emerg": st.emergency, "status": st.status,
                    "arm": p["arm_status"], "mt": p["message_type"], "bc": p["batt_cond"],
                    "bp": p["batt_charge_pct"], "turb": p["turbine"], "valve": p["valve_ckt"],
                    "corr": p["bch_corrected"], "db": p["data_block"],
                    "raw": json.dumps(raw), "key": dedupe,
                })
                if res.rowcount:
                    n += 1
                    self.last_packet = p["ts"]
                    if st.emergency or prev is None or self._status_changed(prev, reading):
                        bus.publish_event("rail:telemetry", {
                            "unit_id": p["unit_id"], "source": p["source"], "ts": p["ts"].isoformat(),
                            "status": st.status, "emergency": st.emergency, "reason": st.reason,
                            "brake_pipe_psig": p["brake_pipe_psig"], "motion": p["motion"]})
                    if st.emergency:
                        log.warning("EOT emergency", unit=p["unit_id"], reason=st.reason)
        self.ingested += n
        return n

    @staticmethod
    def _status_changed(prev: EotReading, cur: EotReading) -> bool:
        return prev.motion != cur.motion or abs((prev.brake_pipe_psig or 0) - (cur.brake_pipe_psig or 0)) >= 10

    @staticmethod
    def _previous_from_db(c, source: str, unit_id: int, ts: datetime) -> EotReading | None:
        r = c.execute(text("""SELECT ts, brake_pipe_psig, motion, message_type FROM rail_telemetry
                              WHERE source = :s AND unit_id = :u AND ts < :ts ORDER BY ts DESC LIMIT 1"""),
                      {"s": source, "u": unit_id, "ts": ts}).first()
        if not r:
            return None
        return EotReading(ts=r[0], unit_id=unit_id, brake_pipe_psig=r[1], motion=r[2], source=source,
                          message_type=r[3])

    def status(self) -> dict:
        return {"source": self.name, "csv": str(self.path), "exists": self.path.exists(),
                "ingested": self.ingested, "heartbeat_age_s": self.heartbeat_age_s(),
                "last_packet": self.last_packet.isoformat() if self.last_packet else None}
