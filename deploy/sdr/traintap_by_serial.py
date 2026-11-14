"""Run traintap on the RTL-SDR with a given EEPROM serial (traintap itself only takes an index).

    python traintap_by_serial.py EOT457 -- --mode eot --csv /data/trains.csv

Mounted read-only into the traintap-eot container (deploy/docker-compose.yml, profile sdr).
Exits non-zero, with the serials it can see, if the dongle isn't plugged in.
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: traintap_by_serial.py SERIAL [-- traintap args]")
    serial, rest = sys.argv[1], sys.argv[2:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
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
    os.execvp("traintap", ["traintap", "--device", str(index), *rest])


if __name__ == "__main__":
    main()
