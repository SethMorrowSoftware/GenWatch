"""Command-line entry point.

Usage:
  python -m genwatch serve         # run the service
  python -m genwatch hash <pw>     # bcrypt-hash a password for config
  python -m genwatch gensecret     # generate a jwt_secret
  python -m genwatch modbusdump    # quick on-bus diagnostic
  python -m genwatch doctor        # pre-flight diagnostics (hardware, config, DB)
  python -m genwatch version       # print version
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

    if cmd == "version":
        from . import __version__
        print(__version__)
        return 0

    if cmd == "modbusdump":
        return _modbusdump(args[1:])

    if cmd == "doctor":
        return _doctor(args[1:])

    print(__doc__, file=sys.stderr)
    return 2


def _doctor(args: list[str]) -> int:
    """Pre-flight diagnostics: prints config, checks serial, talks Modbus."""
    import argparse
    import asyncio
    import os
    from pathlib import Path

    from . import __version__

    p = argparse.ArgumentParser(prog="genwatch doctor")
    p.add_argument("--config", default=None, help="config.yaml path")
    p.add_argument("--probe-addr", type=lambda x: int(x, 0), default=0x0001,
                   help="Modbus address to probe (default 0x0001)")
    opts = p.parse_args(args)

    rc = 0
    print(f"== Castle Generator Monitor — doctor (v{__version__}) ==")
    print(f"  Python:   {sys.version.split()[0]}")

    # --- Config ---
    try:
        from .config import load
        settings = load(opts.config)
    except Exception as e:  # noqa: BLE001
        print(f"  Config:   FAIL — {e}")
        return 1
    print(f"  Config:   {settings.config_path or '(env-only)'}")
    print(f"  Mock:     {settings.mock}")
    print(f"  Data dir: {settings.data_dir} "
          f"({'writable' if os.access(settings.data_dir, os.W_OK) else 'NOT WRITABLE'})")

    # --- Auth ---
    auth = settings.auth
    pw_ok = bool(auth.admin_password_hash and auth.admin_password_hash != "REPLACE_ME")
    sec_ok = bool(auth.jwt_secret and auth.jwt_secret != "REPLACE_ME")
    if pw_ok and sec_ok:
        print("  Auth:     configured")
    else:
        if not pw_ok:
            print("  Auth:     MISSING admin_password_hash — run: genwatch hash <password>")
            rc = 1
        if not sec_ok:
            print("  Auth:     MISSING jwt_secret — run: genwatch gensecret")
            rc = 1

    # --- Register map ---
    try:
        from .modbus.registers import load_register_map
        rm = load_register_map(settings.register_file_path)
        print(f"  Registers: {settings.register_file_path}")
        print(f"             {len(rm.registers)} read + {len(rm.controls)} write, slave={rm.slave}")
    except Exception as e:  # noqa: BLE001
        print(f"  Registers: FAIL — {e}")
        return 1

    # --- Database ---
    try:
        from .db import Database
        db = Database(settings.db_path)
        print(f"  Database:  {db.path} ({db.disk_usage_bytes():,} bytes)")
    except Exception as e:  # noqa: BLE001
        print(f"  Database:  FAIL — {e}")
        rc = 1

    # --- Serial / Modbus probe ---
    if settings.mock:
        print("  Serial:   SKIPPED (mock mode)")
        return rc

    dev = Path(settings.serial.device)
    if not dev.exists():
        print(f"  Serial:   {dev} DOES NOT EXIST")
        glob_candidates = sorted(
            [str(p) for p in Path("/dev").glob("tty*")
             if any(s in p.name for s in ("USB", "ACM", "AMA", "serial"))]
        )
        if glob_candidates:
            print(f"            Available serial devices: {' '.join(glob_candidates)}")
        else:
            print("            No serial devices detected at all — is the adapter plugged in?")
        return 1

    # Try opening the port directly first — separates 'permission denied'
    # from 'device exists but Modbus slave silent'.
    try:
        import serial as pyserial
        s = pyserial.Serial(
            str(dev),
            settings.serial.baud,
            parity=settings.serial.parity,
            stopbits=settings.serial.stopbits,
            bytesize=settings.serial.bytesize,
            timeout=0.5,
        )
        s.close()
        print(f"  Serial:   {dev} opens OK at {settings.serial.baud} "
              f"{settings.serial.bytesize}{settings.serial.parity}{settings.serial.stopbits}")
    except Exception as e:  # noqa: BLE001
        print(f"  Serial:   CANNOT OPEN {dev} — {e}")
        try:
            import grp
            in_dialout = "dialout" in [g.gr_name for g in grp.getgrall() if os.getuid() in g.gr_mem or g.gr_name == "dialout"]
        except Exception:  # noqa: BLE001
            in_dialout = False
        if not in_dialout:
            print("            Likely cause: user is not in the 'dialout' group.")
            print("            Fix:          sudo usermod -aG dialout $USER  (then log out and back in)")
        return 1

    # Talk Modbus.
    async def _probe() -> tuple[object, str | None]:
        from .modbus.client import SerialModbusClient
        client = SerialModbusClient(
            device=str(dev),
            baud=settings.serial.baud,
            parity=settings.serial.parity,
            stopbits=settings.serial.stopbits,
            bytesize=settings.serial.bytesize,
            timeout_s=settings.serial.timeout_s,
            slave=rm.slave,
            retries=1,
            backoff_s=[0.25],
        )
        ok = await client.connect()
        if not ok:
            return None, "connect failed"
        r = await client.read(opts.probe_addr, 1, fc=rm.read_fc)
        await client.close()
        return r, None

    try:
        result, err = asyncio.run(_probe())
    except Exception as e:  # noqa: BLE001
        result, err = None, str(e)

    if err or result is None or not getattr(result, "ok", False):
        reason = err or getattr(result, "error", "unknown")
        print(f"  Modbus:   NO RESPONSE from slave {rm.slave} at 0x{opts.probe_addr:04X} ({reason})")
        print("            Things to check (the H-100 RS-232 port is the factory-default")
        print("            Modbus slave; RS-485 is master by default and won't respond unless")
        print("            you've reconfigured it):")
        print("              · Cable to the correct port (RS-232 slave by default)")
        print("              · RS-232: TX↔RX crossover — use the Generac 0F7707 cable or")
        print("                a USB-to-DB9 cable + DB9 null-modem adapter inline")
        print("              · RS-485 (advanced): A/B swap, 120Ω termination at both ends, GND")
        print(f"              · Baud / parity / stop bits match the panel "
              f"(currently {settings.serial.baud} {settings.serial.bytesize}{settings.serial.parity}{settings.serial.stopbits})")
        print(f"              · Slave ID — H-100 default is 100 (0x64); currently set to {rm.slave}")
        rc = 1
    else:
        print(f"  Modbus:   slave {rm.slave} responded with {result.words} "  # type: ignore[attr-defined]
              f"({result.elapsed_ms:.0f}ms)")  # type: ignore[attr-defined]

    return rc


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
