"""Interpret EOT (end-of-train) telemetry: is the train running, stopped normally, or in emergency?

traintap (AAR S-9152 decoder) reports, per EOT packet: unit address, rear brake-pipe
pressure (psig), motion flag, marker light, arm status, battery. It does not report
"emergency" directly, so we infer it:

* Freight brake pipe is charged to ~90 psig (passenger ~110). A service application
  reduces it by 5-26 psi; an EMERGENCY application vents the pipe to ~0 psig within
  seconds, and the EOT at the rear sees that collapse.
* A pipe near 0 psig is only an emergency if the same unit was recently charged
  (>= CHARGED_PSIG) or is still moving. A cold, uncharged EOT on a stored cut of cars
  also reads ~0 psig and is reported as `no_air`, not an emergency.
* HOT (head-of-train) command semantics are provisional in traintap, so HOT packets
  are only flagged as emergency when their message type is listed in
  HOT_EMERGENCY_MESSAGE_TYPES (empty until real captures confirm the code).

Thresholds are module constants so they can be tuned once real captures exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

EMERGENCY_PSIG = 15          # at or below: pipe is vented
CHARGED_PSIG = 60            # at or above: pipe was charged (train had air)
RELEASED_PSIG = 75           # at or above: brakes essentially released
RECENT = timedelta(minutes=10)
HOT_EMERGENCY_MESSAGE_TYPES: set[str] = set()


@dataclass
class EotReading:
    ts: datetime
    unit_id: int | None
    brake_pipe_psig: int | None
    motion: bool | None
    source: str = "EOT"
    message_type: str | None = None


@dataclass
class EotStatus:
    status: str          # moving | moving_braking | stopped_released | stopped_service |
                         # stopped_heavy | emergency_moving | emergency_stopped | no_air | hot | unknown
    emergency: bool
    reason: str


def interpret(reading: EotReading, previous: EotReading | None = None) -> EotStatus:
    """Classify one packet, using the same unit's previous packet (if recent) for context."""
    if reading.source == "HOT":
        if reading.message_type and reading.message_type in HOT_EMERGENCY_MESSAGE_TYPES:
            return EotStatus("emergency_stopped", True, f"HOT emergency command {reading.message_type}")
        return EotStatus("hot", False, "head-of-train command (semantics provisional)")
    if reading.source == "DPU":
        return EotStatus("unknown", False, "DPU telemetry (field semantics provisional)")

    p, moving = reading.brake_pipe_psig, reading.motion
    if p is None:
        return EotStatus("unknown", False, "no brake pipe pressure in packet")

    prev_recent = (previous is not None and previous.brake_pipe_psig is not None
                   and abs(reading.ts - previous.ts) <= RECENT)
    was_charged = prev_recent and previous.brake_pipe_psig >= CHARGED_PSIG

    if p <= EMERGENCY_PSIG:
        if moving:
            return EotStatus("emergency_moving", True,
                             f"brake pipe {p} psig while moving (pipe vented)")
        if was_charged:
            return EotStatus("emergency_stopped", True,
                             f"brake pipe fell from {previous.brake_pipe_psig} to {p} psig")
        return EotStatus("no_air", False, f"brake pipe {p} psig with no recent charge (uncharged EOT)")

    if moving:
        if p >= RELEASED_PSIG:
            return EotStatus("moving", False, f"moving, brake pipe {p} psig")
        return EotStatus("moving_braking", False, f"moving with service reduction ({p} psig)")

    # not moving
    if p >= RELEASED_PSIG:
        return EotStatus("stopped_released", False, f"stopped, brakes released ({p} psig)")
    if p >= 45:
        return EotStatus("stopped_service", False, f"normal stop, service application ({p} psig)")
    return EotStatus("stopped_heavy", False, f"stopped with heavy reduction ({p} psig)")
