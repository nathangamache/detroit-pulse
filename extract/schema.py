"""Output schema for the batched Claude extraction call, and parsing of its results.

One call returns `{"results": [item, ...]}` with exactly one item per transmission.
Sent as `output_config.format = {"type": "json_schema", "schema": BATCH_SCHEMA}`
(structured outputs). Every object sets additionalProperties: false and lists all
properties as required. No numeric/string-length constraints (unsupported).

Structured outputs allows at most 16 union-typed (anyOf / type-array) parameters per
schema (the API returned a 400 at 17 on 2026-11-14), so absent TEXT fields are empty
strings rather than null; only objects and integers are nullable (6 unions in total).
parse_item() turns "" back into None.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field

LOCATION_KINDS = ["intersection", "block", "address", "landmark", "highway", "none"]

INCIDENT_TYPES = [
    "STRUCTURE_FIRE", "VEHICLE_FIRE", "OTHER_FIRE", "FIRE_ALARM", "MEDICAL", "TRAFFIC_ACCIDENT",
    "SHOOTING", "STABBING", "ASSAULT", "DOMESTIC", "ROBBERY", "BURGLARY", "THEFT", "PURSUIT",
    "SUSPICIOUS", "WELFARE_CHECK", "HAZMAT", "GAS_LEAK", "WATER_RESCUE", "BOMB_THREAT",
    "DISTURBANCE", "TRAFFIC_HAZARD", "RAIL_OPERATIONS", "RAIL_INCIDENT", "OTHER", "UNKNOWN",
    "NONE",   # no reportable incident (status, acknowledgement, radio check, chatter)
]
PRIORITIES = ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
RAIL_REASONS = ["meet_or_hold", "signal_problem", "defect_detector", "crew_change_hos", "mechanical",
                "crossing_incident", "trespasser", "switching_yard", "unknown"]
DETECTOR_RESULTS = ["no_defects", "alarm", "integrity_failure", "unknown"]
ALARM_KINDS = ["hot_box", "hot_wheel", "cold_wheel", "dragging_equipment", "high_wide",
               "excessive_alarms", "other"]


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}          # "" when absent (see module docstring)
_NINT = _nullable({"type": "integer"})

LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": LOCATION_KINDS},
        "street1": _STR,
        "street2": _STR,
        "number": _STR,
        "city": _STR,
        "raw": _STR,
    },
    "required": ["kind", "street1", "street2", "number", "city", "raw"],
    "additionalProperties": False,
}

DETECTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "enum": DETECTOR_RESULTS},
        "track": _STR,
        "direction": _STR,
        "axle_count": _NINT,
        "speed_mph": _NINT,
        "temperature_f": _NINT,
        "alarms": {"type": "array", "items": {"type": "string", "enum": ALARM_KINDS}},
    },
    "required": ["result", "track", "direction", "axle_count", "speed_mph", "temperature_f", "alarms"],
    "additionalProperties": False,
}

RAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "railroad": _STR,
        "subdivision": _STR,
        "train_symbol": _STR,
        "milepost": _STR,
        "reason_label": _nullable({"type": "string", "enum": RAIL_REASONS}),
        "detector": _nullable(DETECTOR_SCHEMA),
    },
    "required": ["railroad", "subdivision", "train_symbol", "milepost", "reason_label", "detector"],
    "additionalProperties": False,
}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "has_location": {"type": "boolean"},
        "location": LOCATION_SCHEMA,
        "incident_type": {"type": "string", "enum": INCIDENT_TYPES},
        "priority": {"type": "string", "enum": PRIORITIES},
        "units": {"type": "array", "items": {"type": "string"}},
        "is_rail": {"type": "boolean"},
        "rail": _nullable(RAIL_SCHEMA),
        "short_summary": {"type": "string"},
    },
    "required": ["id", "has_location", "location", "incident_type", "priority", "units", "is_rail",
                 "rail", "short_summary"],
    "additionalProperties": False,
}

BATCH_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": ITEM_SCHEMA}},
    "required": ["results"],
    "additionalProperties": False,
}


def output_format() -> dict:
    return {"type": "json_schema", "schema": copy.deepcopy(BATCH_SCHEMA)}


def count_unions(schema=BATCH_SCHEMA) -> int:
    n = 0
    if isinstance(schema, dict):
        if "anyOf" in schema or isinstance(schema.get("type"), list):
            n += 1
        for v in schema.values():
            n += count_unions(v)
    elif isinstance(schema, list):
        for v in schema:
            n += count_unions(v)
    return n


_TEXT_FIELDS = {"location": ("street1", "street2", "number", "city", "raw"),
                "rail": ("railroad", "subdivision", "train_symbol", "milepost"),
                "detector": ("track", "direction")}


def to_wire(result: dict) -> dict:
    """Render a result the way the schema expects it on the wire (None text -> "")."""
    import copy as _c
    d = _c.deepcopy(result)
    for k in _TEXT_FIELDS["location"]:
        if d.get("location") is not None and d["location"].get(k) is None:
            d["location"][k] = ""
    rail = d.get("rail")
    if isinstance(rail, dict):
        for k in _TEXT_FIELDS["rail"]:
            if rail.get(k) is None:
                rail[k] = ""
        det = rail.get("detector")
        if isinstance(det, dict):
            for k in _TEXT_FIELDS["detector"]:
                if det.get(k) is None:
                    det[k] = ""
    return d


# ── Parsed result ───────────────────────────────────────────────────────────

@dataclass
class Location:
    kind: str = "none"
    street1: str | None = None
    street2: str | None = None
    number: str | None = None
    city: str | None = None
    raw: str | None = None

    @property
    def usable(self) -> bool:
        return self.kind != "none" and bool(self.street1 or self.raw)

    def display(self) -> str | None:
        """Human/legacy string, e.g. '15100 block of Kelly' or 'Packard & State, Ann Arbor'."""
        if not self.usable:
            return None
        if self.kind in ("intersection", "highway") and self.street1 and self.street2:
            head = f"{self.street1} & {self.street2}"
        elif self.kind == "block" and self.number and self.street1:
            head = f"{self.number} block of {self.street1}"
        elif self.kind == "address" and self.number and self.street1:
            head = f"{self.number} {self.street1}"
        else:
            head = self.street1 or self.raw or ""
        return f"{head}, {self.city}" if self.city else head


@dataclass
class ItemResult:
    id: str
    has_location: bool = False
    location: Location = field(default_factory=Location)
    incident_type: str = "UNKNOWN"
    priority: str = "UNKNOWN"
    units: list[str] = field(default_factory=list)
    is_rail: bool = False
    rail: dict | None = None
    short_summary: str = ""
    source: str = "claude"            # claude | rules | prefilter
    flags: list[str] = field(default_factory=list)

    @property
    def has_incident(self) -> bool:
        return self.incident_type not in ("NONE",)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["has_incident"] = self.has_incident
        return d


def _s(v) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def parse_item(d: dict) -> ItemResult:
    """Validate/coerce one item from the model (schema-constrained, but stay defensive)."""
    loc_d = d.get("location") or {}
    kind = loc_d.get("kind") if loc_d.get("kind") in LOCATION_KINDS else "none"
    loc = Location(kind=kind, street1=_s(loc_d.get("street1")), street2=_s(loc_d.get("street2")),
                   number=_s(loc_d.get("number")), city=_s(loc_d.get("city")), raw=_s(loc_d.get("raw")))
    if loc.number:
        digits = "".join(ch for ch in loc.number if ch.isdigit())
        loc.number = digits or None
    has_loc = bool(d.get("has_location")) and loc.usable
    if not has_loc:
        loc = Location(kind="none", raw=loc.raw)
    itype = d.get("incident_type") if d.get("incident_type") in INCIDENT_TYPES else "UNKNOWN"
    prio = d.get("priority") if d.get("priority") in PRIORITIES else "UNKNOWN"
    units = [str(u).strip() for u in (d.get("units") or []) if str(u).strip()][:20]
    rail = dict(d["rail"]) if isinstance(d.get("rail"), dict) else None
    if rail is not None:
        for k in _TEXT_FIELDS["rail"]:
            rail[k] = _s(rail.get(k))
        if rail.get("reason_label") not in RAIL_REASONS + [None]:
            rail["reason_label"] = "unknown"
        if isinstance(rail.get("detector"), dict):
            det = dict(rail["detector"])
            for k in _TEXT_FIELDS["detector"]:
                det[k] = _s(det.get(k))
            rail["detector"] = det
    is_rail = bool(d.get("is_rail")) or rail is not None and any(rail.get(k) for k in ("railroad", "milepost", "train_symbol"))
    return ItemResult(id=str(d.get("id", "")), has_location=has_loc, location=loc, incident_type=itype,
                      priority=prio, units=units, is_rail=is_rail, rail=rail,
                      short_summary=(_s(d.get("short_summary")) or "")[:200])
