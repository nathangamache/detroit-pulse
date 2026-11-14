"""Run traintap on the RTL-SDR with a given EEPROM serial (traintap itself only takes an index).

    python traintap_by_serial.py EOT457 [--started /status/traintap-eot.started] -- --mode eot --csv /data/trains.csv

Mounted read-only into the traintap-eot container (deploy/docker-compose.yml, profile sdr).
Exits non-zero, with the serials it can see, if the dongle isn't plugged in.

`--started FILE`: right before it execs traintap, write the start marker Pulse reads
(core/receivers.py, <receiver>.started): a new boot id and the time, through a temp file and a
rename. traintap's heartbeat is a bare touch with no counters, and traintap touches it as soon
as it starts, so a restart is invisible in the heartbeat alone; the marker shows it, and
/v1/rail/activity calls nothing quiet across it. If the marker can't be written, traintap is not
started: a stale marker would hide this restart.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone


def parse_args(argv: list[str]) -> tuple[str, str | None, list[str]]:
    """(serial, start marker path or None, traintap's args) from argv[1:]."""
    if not argv:
        sys.exit("usage: traintap_by_serial.py SERIAL [--started FILE] [-- traintap args]")
    serial, rest, started = argv[0], argv[1:], None
    if rest[:1] == ["--started"]:
        if len(rest) < 2:
            sys.exit("--started needs a file")
        started, rest = rest[1], rest[2:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return serial, started, rest


def write_started(path: str) -> str:
    """Write a new start marker at `path` atomically; returns its boot id."""
    boot = uuid.uuid4().hex
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"{boot} {at}\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return boot


def main() -> None:
    serial, started, rest = parse_args(sys.argv[1:])
    from rtlsdr import RtlSdr
    try:
        index = RtlSdr.get_device_index_by_serial(serial)
    except Exception as e:  # noqa: BLE001 - pyrtlsdr raises IOError/LibUSBError variants
        try:
            seen = RtlSdr.get_device_serial_addresses()
        except Exception:  # noqa: BLE001
            seen = []
        sys.exit(f"RTL-SDR with serial {serial!r} not found ({e}); visible serials: {seen or 'none'}")
    print(f"traintap: serial {serial} is device index {index}", flush=True)
    if started:
        try:
            boot = write_started(started)
        except OSError as e:
            sys.exit(f"cannot write the start marker {started} ({e}); not starting traintap, since Pulse "
                     "could not see this restart")
        print(f"traintap: start marker {started} boot {boot}", flush=True)
    os.execvp("traintap", ["traintap", "--device", str(index), *rest])


if __name__ == "__main__":
    main()
