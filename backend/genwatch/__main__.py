"""Command-line entry point.

Usage:
  python -m genwatch serve         # run the service
  python -m genwatch hash <pw>     # bcrypt-hash a password for config
  python -m genwatch gensecret     # generate a jwt_secret
  python -m genwatch modbusdump    # quick on-bus diagnostic
"""
from __future__ import annotations

import secrets
import sys


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "serve"

    if cmd == "serve":
        import os
        import uvicorn
        host = os.environ.get("GENWATCH_HOST", "0.0.0.0")
        port = int(os.environ.get("GENWATCH_PORT", "8000"))
        uvicorn.run(
            "genwatch.main:app",
            host=host,
            port=port,
            log_level=os.environ.get("GENWATCH_LOG_LEVEL", "info").lower(),
            access_log=False,
            workers=1,  # single worker — Modbus serial is single-master
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
        return 0

    if cmd == "hash":
        if len(args) < 2:
            print("usage: genwatch hash <password>", file=sys.stderr)
            return 2
        from .services.auth import hash_password
        print(hash_password(args[1]))
        return 0

    if cmd == "gensecret":
        print(secrets.token_hex(32))
        return 0

    if cmd == "modbusdump":
        return _modbusdump(args[1:])

    print(__doc__, file=sys.stderr)
    return 2


def _modbusdump(args: list[str]) -> int:
    import argparse
    import asyncio

    from .config import load
    from .modbus.client import SerialModbusClient
    from .modbus.registers import load_register_map

    p = argparse.ArgumentParser(prog="genwatch modbusdump")
    p.add_argument("--device", default=None)
    p.add_argument("--baud", type=int, default=None)
    p.add_argument("--slave", type=int, default=None)
    p.add_argument("--addr", type=lambda x: int(x, 0), default=0x0001)
    p.add_argument("--count", type=int, default=16)
    p.add_argument("--fc", type=int, default=3)
    p.add_argument("--config", default=None)
    opts = p.parse_args(args)

    settings = load(opts.config)
    rm = load_register_map(settings.register_file_path)

    async def run():
        client = SerialModbusClient(
            device=opts.device or settings.serial.device,
            baud=opts.baud or settings.serial.baud,
            parity=settings.serial.parity,
            stopbits=settings.serial.stopbits,
            bytesize=settings.serial.bytesize,
            timeout_s=settings.serial.timeout_s,
            slave=opts.slave or rm.slave,
            retries=2,
            backoff_s=[0.2, 0.4],
        )
        ok = await client.connect()
        if not ok:
            print("connect failed", file=sys.stderr)
            return 2
        r = await client.read(opts.addr, opts.count, fc=opts.fc)
        await client.close()
        if not r.ok:
            print(f"read failed: {r.error}", file=sys.stderr)
            return 2
        print(f"# read addr=0x{opts.addr:04X} count={opts.count} fc={opts.fc} in {r.elapsed_ms:.1f}ms")
        for i, w in enumerate(r.words or []):
            print(f"0x{opts.addr + i:04X}  {w:5d}  0x{w:04X}")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
