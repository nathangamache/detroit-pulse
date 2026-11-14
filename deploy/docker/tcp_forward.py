"""Tiny TCP forwarder for the compose `debug` profile (no extra image needed).

    python deploy/docker/tcp_forward.py 7710:pulse-api:7710 5432:pulse-postgis:5432 6379:pulse-redis:6379

Each LISTEN:HOST:PORT listens on 0.0.0.0:LISTEN inside the pulse-debug container and pipes bytes to
HOST:PORT on the internal network. Compose publishes those ports on 127.0.0.1 only; this exists
because the real services sit on an `internal` network, where published ports don't work.
"""
from __future__ import annotations

import asyncio
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _handle(client_r, client_w, host: str, port: int) -> None:
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    except (OSError, asyncio.TimeoutError):
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


async def main(specs: list[str]) -> None:
    servers = []
    for spec in specs:
        listen, host, port = spec.split(":")
        srv = await asyncio.start_server(lambda r, w, h=host, p=int(port): _handle(r, w, h, p), "0.0.0.0", int(listen))
        servers.append(srv)
        print(f"forwarding :{listen} -> {host}:{port}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1:]))
