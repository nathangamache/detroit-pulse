"""`uv run python -m api` -> uvicorn on PULSE_API_HOST:PULSE_API_PORT (default 127.0.0.1:7710)."""
from __future__ import annotations

import ipaddress
import sys

import uvicorn

from core.settings import get_settings


def main() -> None:
    s = get_settings()
    host = s.pulse_api_host
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and "--allow-non-loopback" not in sys.argv:
        sys.exit(f"refusing to bind {host}: Pulse is meant to sit behind the gateway on 127.0.0.1 "
                 f"(pass --allow-non-loopback for a private container network)")
    if not (s.pulse_api_token and s.pulse_api_token.get_secret_value()):
        print("WARNING: PULSE_API_TOKEN is not set; the API is open to anything on this host.", file=sys.stderr)
    uvicorn.run("api.main:app", host=host, port=s.pulse_api_port, proxy_headers=False, log_level="info",
                ws_max_size=1 << 20)


if __name__ == "__main__":
    main()
