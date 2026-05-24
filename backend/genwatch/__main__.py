"""Command-line entry point.

Usage:
  python -m genwatch serve         # run the service
  python -m genwatch hash <pw>     # bcrypt-hash a password for config
  python -m genwatch gensecret     # generate a jwt_secret
  python -m genwatch modbusdump    # single-block read for diagnostics
  python -m genwatch scan          # walk a range of addresses, classify each
  python -m genwatch panel         # decoded snapshot for cross-check vs H-100 LCD
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

    if cmd == "scan":
        return _scan(args[1:])

    if cmd == "panel":
        return _panel(args[1:])

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

    # --- Link / Modbus probe ---
    if settings.mock:
        print("  Link:     SKIPPED (mock mode)")
        return rc

    print(f"  Transport: {settings.transport}")

    if settings.transport == "tcp":
        host = settings.modbus_tcp.host
        port = settings.modbus_tcp.port
        # Cheap TCP reachability check before pymodbus so we separate
        # "host unreachable" from "Modbus slave silent on a valid socket".
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(min(settings.modbus_tcp.connect_timeout_s, 5.0))
        try:
            sock.connect((host, port))
            sock.close()
            print(f"  TCP:      {host}:{port} reachable")
        except Exception as e:  # noqa: BLE001
            print(f"  TCP:      CANNOT REACH {host}:{port} — {e}")
            print("            · Is the bridge powered and on the LAN? Try: ping " + host)
            print(f"            · Is the bridge listening on TCP {port}? Try: nc -vz {host} {port}")
            print("            · Lantronix: Channel 1 → Connection → Connect Mode")
            print("              should be passive (Active=None, Passive=Yes).")
            return 1
    else:
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
        client = _build_client_from_settings(settings, rm, retries=1)
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
        if settings.transport == "tcp":
            print("            TCP socket connected but no Modbus reply. Most common causes:")
            print("              · Bridge serial port settings don't match the H-100 (need 9600 8N1)")
            print("              · Lantronix 'Pack Control' is splitting RTU frames — set Idle Gap")
            print("                Time to ~10 ms in Channel 1 → Connection → Pack Control")
            print("              · Wrong slave ID (H-100 default is 100 / 0x64; currently {})".format(rm.slave))
            print("              · Bridge wired to the wrong panel port, or null-modem missing")
            print("                (the H-100 RS-232 port needs TX↔RX crossover; use the 0F7707)")
        else:
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


def _build_client_from_settings(settings, rm, retries: int = 2):
    """Construct the right Modbus client for the configured transport.

    Used by doctor / modbusdump / scan / panel so the diagnostics follow
    whatever transport is in config.yaml. Honors mock so the diagnostic
    tools can be smoke-tested without hardware.
    """
    from .modbus.client import MockModbusClient, SerialModbusClient, TcpRtuModbusClient

    if settings.mock:
        return MockModbusClient(rm)

    if settings.transport == "tcp":
        return TcpRtuModbusClient(
            host=settings.modbus_tcp.host,
            port=settings.modbus_tcp.port,
            framer=settings.modbus_tcp.framer,
            timeout_s=settings.modbus_tcp.timeout_s,
            connect_timeout_s=settings.modbus_tcp.connect_timeout_s,
            slave=rm.slave,
            retries=retries,
            backoff_s=[0.25, 0.5],
        )
    return SerialModbusClient(
        device=settings.serial.device,
        baud=settings.serial.baud,
        parity=settings.serial.parity,
        stopbits=settings.serial.stopbits,
        bytesize=settings.serial.bytesize,
        timeout_s=settings.serial.timeout_s,
        slave=rm.slave,
        retries=retries,
        backoff_s=[0.25, 0.5],
    )


def _modbusdump(args: list[str]) -> int:
    import argparse
    import asyncio

    from .config import load
    from .modbus.registers import load_register_map

    p = argparse.ArgumentParser(prog="genwatch modbusdump")
    p.add_argument("--device", default=None, help="(serial only) override serial.device")
    p.add_argument("--host", default=None, help="(tcp only) override modbus_tcp.host")
    p.add_argument("--port", type=int, default=None, help="(tcp only) override modbus_tcp.port")
    p.add_argument("--baud", type=int, default=None)
    p.add_argument("--slave", type=int, default=None)
    p.add_argument("--addr", type=lambda x: int(x, 0), default=0x0001)
    p.add_argument("--count", type=int, default=16)
    p.add_argument("--fc", type=int, default=3)
    p.add_argument("--config", default=None)
    opts = p.parse_args(args)

    settings = load(opts.config)
    # CLI overrides for either transport — patch the in-memory settings
    # before handing them to the client builder.
    overrides: dict = {}
    if opts.device is not None:
        overrides["serial"] = settings.serial.model_copy(update={"device": opts.device})
    if opts.baud is not None:
        overrides["serial"] = (overrides.get("serial") or settings.serial).model_copy(update={"baud": opts.baud})
    if opts.host is not None or opts.port is not None:
        tcp_update: dict = {}
        if opts.host is not None:
            tcp_update["host"] = opts.host
        if opts.port is not None:
            tcp_update["port"] = opts.port
        overrides["modbus_tcp"] = settings.modbus_tcp.model_copy(update=tcp_update)
    if overrides:
        settings = settings.model_copy(update=overrides)
    rm = load_register_map(settings.register_file_path)
    if opts.slave is not None:
        rm.slave = opts.slave  # type: ignore[misc]

    async def run():
        client = _build_client_from_settings(settings, rm, retries=2)
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


def _scan(args: list[str]) -> int:
    """Walk a range of Modbus addresses, classify each register.

    Tries batched reads for speed; falls back to single-register reads
    when a batch returns an exception (so one bad address doesn't lose
    15 good neighbors). Tries both FC3 (holding) and FC4 (input) by
    default. Decodes any ASCII bytes inline so model/serial strings
    embedded in the register space are easy to spot.

    The service holds the Lantronix's only TCP socket, so stop it first:
        sudo systemctl stop genwatch
        sudo PYTHONPATH=/opt/genwatch /opt/genwatch/venv/bin/python -m \\
            genwatch scan --start 0x0000 --end 0x07FF
        sudo systemctl start genwatch
    """
    import argparse
    import asyncio
    import time

    from .config import load
    from .modbus.registers import load_register_map

    p = argparse.ArgumentParser(prog="genwatch scan")
    p.add_argument("--config", default=None)
    p.add_argument("--start", type=lambda x: int(x, 0), default=0x0000,
                   help="First address to scan (default 0x0000)")
    p.add_argument("--end", type=lambda x: int(x, 0), default=0x07FF,
                   help="Last address (inclusive, default 0x07FF = 2048 regs)")
    p.add_argument("--batch", type=int, default=16,
                   help="Words per batched read; fall back to 1 on error (default 16)")
    p.add_argument("--fc", default="3,4",
                   help="Function codes to try, comma-separated (default '3,4')")
    p.add_argument("--slave", type=int, default=None,
                   help="Override slave ID from register map")
    p.add_argument("--out", default=None,
                   help="Also write summary to this file")
    opts = p.parse_args(args)

    settings = load(opts.config)
    rm = load_register_map(settings.register_file_path)
    if opts.slave is not None:
        rm.slave = opts.slave  # type: ignore[misc]

    fcs = [int(x) for x in opts.fc.split(",") if x.strip()]
    total_regs = opts.end - opts.start + 1
    print(f"== Modbus scan ==")
    print(f"  Transport: {settings.transport}")
    if settings.transport == "tcp":
        print(f"  Target:    {settings.modbus_tcp.host}:{settings.modbus_tcp.port}")
    else:
        print(f"  Target:    {settings.serial.device}")
    print(f"  Slave:     {rm.slave} (0x{rm.slave:02X})")
    print(f"  Range:     0x{opts.start:04X}-0x{opts.end:04X} ({total_regs} registers)")
    print(f"  Func:      {fcs}")
    print(f"  Batch:     {opts.batch}")
    print()

    async def scan_one_fc(fc: int) -> dict[int, int | str]:
        """Return {addr: value_or_error_tag} for one function code."""
        client = _build_client_from_settings(settings, rm, retries=1)
        if not await client.connect():
            print(f"  fc={fc}: connect failed (is the service holding the socket?)")
            return {}

        result: dict[int, int | str] = {}
        addr = opts.start
        t0 = time.time()
        last_print = t0
        try:
            while addr <= opts.end:
                count = min(opts.batch, opts.end - addr + 1)
                r = await client.read(addr, count, fc=fc)
                if r.ok and r.words is not None:
                    for i, w in enumerate(r.words):
                        result[addr + i] = w
                    addr += count
                else:
                    # batch failed; walk one at a time so we keep neighbors
                    for a in range(addr, addr + count):
                        if a > opts.end:
                            break
                        rr = await client.read(a, 1, fc=fc)
                        if rr.ok and rr.words is not None:
                            result[a] = rr.words[0]
                        else:
                            result[a] = rr.error or "err"
                    addr += count

                now = time.time()
                if now - last_print > 2.0:
                    done = addr - opts.start
                    pct = 100 * done / total_regs
                    print(f"  fc={fc}: {done}/{total_regs} ({pct:.0f}%) elapsed {now-t0:.0f}s")
                    last_print = now
        finally:
            await client.close()

        print(f"  fc={fc}: done in {time.time()-t0:.1f}s")
        return result

    async def run() -> dict[int, dict[int, int | str]]:
        out: dict[int, dict[int, int | str]] = {}
        for fc in fcs:
            out[fc] = await scan_one_fc(fc)
        return out

    by_fc = asyncio.run(run())

    # ── Summarize ────────────────────────────────────────────────────────
    lines: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit()
    emit("== Results ==")
    for fc in fcs:
        results = by_fc.get(fc, {})
        if not results:
            emit(f"\nfc={fc}: no data")
            continue

        live: list[tuple[int, int]] = []
        zeros: list[int] = []
        errors: dict[str, list[int]] = {}
        for a in sorted(results):
            v = results[a]
            if isinstance(v, int):
                if v == 0:
                    zeros.append(a)
                else:
                    live.append((a, v))
            else:
                errors.setdefault(v, []).append(a)

        emit(f"\nfc={fc}: {len(live)} non-zero, {len(zeros)} zero, "
             f"{sum(len(v) for v in errors.values())} error")

        if live:
            emit("\n  Non-zero registers:")
            # Try to detect ASCII runs (printable bytes in big-endian word order).
            i = 0
            while i < len(live):
                addr_i, val_i = live[i]
                hi, lo = (val_i >> 8) & 0xFF, val_i & 0xFF
                if 0x20 <= hi <= 0x7E and 0x20 <= lo <= 0x7E:
                    # Look ahead for more printable words at consecutive addrs.
                    j = i
                    buf = bytearray()
                    while j < len(live) and live[j][0] == addr_i + (j - i):
                        v = live[j][1]
                        h, l = (v >> 8) & 0xFF, v & 0xFF
                        if 0x20 <= h <= 0x7E and 0x20 <= l <= 0x7E:
                            buf.append(h); buf.append(l)
                            j += 1
                        else:
                            break
                    if j - i >= 2:  # at least 4 ASCII bytes
                        emit(f"    0x{addr_i:04X}-0x{addr_i + (j - i) - 1:04X}  "
                             f"ASCII '{buf.decode('ascii').rstrip()}'")
                        i = j
                        continue
                emit(f"    0x{addr_i:04X}  {val_i:6d}  0x{val_i:04X}")
                i += 1

        if zeros:
            emit(f"\n  Zero registers ({len(zeros)}): "
                 f"{_compact_ranges(zeros)}")

        for tag, addrs in errors.items():
            emit(f"\n  {tag} ({len(addrs)}): {_compact_ranges(addrs)}")

    if opts.out:
        with open(opts.out, "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n[saved to {opts.out}]")

    return 0


def _panel(args: list[str]) -> int:
    """Decoded snapshot of every named register, for cross-checking against
    the H-100 LCD.

    The use case: the UI shows you a number that doesn't match the panel,
    or a warning bit you don't expect, and you want to know which
    bit-to-meaning mapping in `registers/h100.yaml` is responsible — or
    whether the panel itself disagrees. This command:

      - reads every register in the loaded map (using single-register
        reads so one bad address can't take out the report)
      - decodes each value with its scale + unit
      - for every bitfield register, lists every bit that's SET and
        labels it with the name from `alarm_bits` / `engine_state_bits`
        (or "?" if we have no name for that bit on this panel)
      - calls out telemetry that looks structurally suspicious
        (0xFFFF sentinels, percentages > 100, etc.)

    Output is plain text designed to sit next to the panel display so
    you can tick through it. Use `--json` if you want it machine-readable.
    """
    import argparse
    import asyncio
    import time

    from .config import load
    from .modbus.client import ModbusResult
    from .modbus.registers import (
        batch_reads,
        decode_value,
        load_register_map,
    )

    p = argparse.ArgumentParser(prog="genwatch panel")
    p.add_argument("--config", default=None, help="config.yaml path")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of the text report")
    p.add_argument("--slave", type=int, default=None,
                   help="override modbus.slave (default: from register map)")
    opts = p.parse_args(args)

    settings = load(opts.config)
    rm = load_register_map(settings.register_file_path)
    if opts.slave is not None:
        rm.slave = opts.slave  # type: ignore[misc]

    async def run() -> tuple[dict[int, int], list[str], float]:
        """Read every register in the map. Returns (addr→word, errors, elapsed_ms)."""
        client = _build_client_from_settings(settings, rm, retries=1)
        ok = await client.connect()
        if not ok:
            return {}, ["connect failed"], 0.0
        t0 = time.perf_counter()
        words: dict[int, int] = {}
        errs: list[str] = []
        # Use the same batching the poller uses so we issue ~5 reads not
        # ~35. Fall back to single-register reads if a batch fails so one
        # bad address can't blow out the whole report.
        for start, count in batch_reads(rm.registers):
            r: ModbusResult = await client.read(start, count, fc=rm.read_fc)
            if r.ok and r.words:
                for i, w in enumerate(r.words):
                    words[start + i] = int(w)
                continue
            errs.append(f"batch 0x{start:04X}+{count} failed ({r.error}); falling back to singles")
            for i in range(count):
                rr = await client.read(start + i, 1, fc=rm.read_fc)
                if rr.ok and rr.words:
                    words[start + i] = int(rr.words[0])
                else:
                    errs.append(f"  0x{start + i:04X} {rr.error}")
        await client.close()
        return words, errs, (time.perf_counter() - t0) * 1000.0

    words, errs, elapsed_ms = asyncio.run(run())
    if "connect failed" in errs:
        print("connect failed — check transport in config.yaml", file=sys.stderr)
        return 2

    # Build the decoded view: per-register name → {value, raw_words, def}
    decoded: list[dict] = []
    for reg in rm.registers:
        raw_words = [words.get(reg.addr + i) for i in range(reg.words)]
        if any(w is None for w in raw_words):
            value = None
        else:
            value = decode_value(reg, [w for w in raw_words if w is not None])  # type: ignore[arg-type]
        decoded.append({
            "name": reg.name,
            "addr": reg.addr,
            "type": reg.type,
            "scale": reg.scale,
            "unit": reg.unit,
            "group": reg.group,
            "tier": reg.tier,
            "value": value,
            "raw_words": raw_words,
        })

    # Engine state — both the semantic result and the *evidence* (which
    # rule fired). We re-evaluate rules in priority order so the user can
    # see what the poller saw.
    value_map: dict[str, float | int] = {
        d["name"]: int(d["value"]) if isinstance(d["value"], (int, float)) else 0
        for d in decoded if d["value"] is not None
    }
    fired_state = "unknown"
    fired_rule = None
    for rule in rm.engine_state_bits:
        raw = value_map.get(rule.register)
        if raw is None:
            continue
        if (int(raw) & rule.mask) == rule.mask:
            fired_state = rule.state
            fired_rule = rule
            break

    # Per-bit dictionaries: register name → {bit_mask: AlarmBit or StateBitRule}
    bit_meanings: dict[str, dict[int, str]] = {}
    for ab in rm.alarm_bits:
        bit_meanings.setdefault(ab.register, {})[ab.mask] = f"{ab.code} ({ab.severity})"
    for rule in rm.engine_state_bits:
        existing = bit_meanings.setdefault(rule.register, {}).get(rule.mask)
        label = f"state:{rule.state}"
        bit_meanings[rule.register][rule.mask] = (
            f"{existing}, {label}" if existing else label
        )

    # ─── JSON output path ─────────────────────────────────────────────
    if opts.json:
        import json
        out = {
            "transport": settings.transport,
            "slave": rm.slave,
            "engine_state": fired_state,
            "engine_state_via": (
                {"register": fired_rule.register, "mask": f"0x{fired_rule.mask:04X}"}
                if fired_rule else None
            ),
            "registers": [
                {
                    "name": d["name"],
                    "addr": f"0x{d['addr']:04X}",
                    "type": d["type"],
                    "value": d["value"],
                    "unit": d["unit"],
                    "raw_hex": [None if w is None else f"0x{w:04X}" for w in d["raw_words"]],
                }
                for d in decoded
            ],
            "active_alarms": [
                {"code": ab.code, "desc": ab.desc, "severity": ab.severity}
                for ab in rm.derive_active_alarms(value_map)
            ],
            "errors": errs,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        print(json.dumps(out, indent=2))
        return 0 if not errs else 1

    # ─── Text report ──────────────────────────────────────────────────
    if settings.transport == "tcp":
        link = f"tcp {settings.modbus_tcp.host}:{settings.modbus_tcp.port}"
    else:
        link = f"serial {settings.serial.device} {settings.serial.baud}"
    print(f"== Castle Generator Monitor — panel ({len(decoded)} registers in {elapsed_ms:.0f} ms) ==")
    print(f"  Link:    {link}  slave={rm.slave}")
    print(f"  Map:     {rm.path}")
    if errs:
        print(f"  WARN:    {len(errs)} read issue(s); some values may be missing")
    print()

    # Engine state with provenance
    if fired_rule is not None:
        print(f"Engine state: {fired_state.upper()}")
        print(f"  (matched: {fired_rule.register} bit 0x{fired_rule.mask:04X} set)")
    else:
        print(f"Engine state: {fired_state.upper()}  (no rule matched)")
    print()

    # Group registers by their YAML `group:` tag, render each register
    # according to its own type so a u16 count sitting in a status group
    # isn't dumped as a bitfield.
    groups: dict[str, list[dict]] = {}
    for d in decoded:
        groups.setdefault(d["group"] or "Other", []).append(d)

    for g, rows in groups.items():
        print(f"{g}")
        for d in rows:
            raw = _fmt_raw(d["raw_words"])
            if d["type"] == "bitfld":
                v = int(d["value"]) & 0xFFFF if d["value"] is not None else 0
                print(f"  {d['name']:<22} 0x{v:04X}                raw={raw}")
                if v == 0:
                    print("                           (no bits set)")
                    continue
                meanings = bit_meanings.get(d["name"], {})
                # Walk bits high → low so the report reads like the panel
                for bit in range(15, -1, -1):
                    mask = 1 << bit
                    if not (v & mask):
                        continue
                    label = meanings.get(mask, "?")
                    marker = "  " if label != "?" else " ?"
                    print(f"     {marker}  0x{mask:04X}  {label}")
            else:
                val = _fmt_value(d["value"], d["scale"], d["unit"])
                note = _value_note(d)
                unit = d["unit"] or ""
                print(f"  {d['name']:<22} {val:>14}  {unit:<6} raw={raw}{note}")
        print()

    # Active alarms summary
    active = rm.derive_active_alarms(value_map)
    print("Active alarms (from alarm_bits map)")
    if not active:
        print("  none")
    else:
        for ab in active:
            print(f"  - {ab.severity.upper():<5}  {ab.code:<28}  {ab.desc}")
    print()

    # Cross-check checklist
    print("Cross-check against the H-100 LCD:")
    print("  1. Does the panel show the same engine state as above?")
    print("  2. Does the panel show the same active warnings/alarms?")
    print("     If a warning is listed here but absent on the panel, the bit")
    print("     mapping in registers/h100.yaml is wrong for your revision.")
    print("  3. Spot-check rpm, oil pressure, coolant temp, battery V, fuel %.")
    print("     Values flagged with ← are structurally suspicious (sentinel,")
    print("     out-of-range, etc.) and worth confirming.")
    print()
    return 0 if not errs else 1


def _fmt_value(value, scale: float, unit: str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        # Scaled values; print 2 decimals unless they're effectively integers
        if abs(value - round(value)) < 1e-6:
            return f"{value:.0f}"
        return f"{value:.2f}"
    return f"{value}"


def _fmt_raw(words: list[int | None]) -> str:
    parts = []
    for w in words:
        parts.append("----" if w is None else f"{w:04X}")
    return " ".join(parts)


def _value_note(d: dict) -> str:
    """Heuristic flag for values that look structurally wrong.

    These are tells that the register's bit-to-meaning or scale doesn't
    match the panel revision. False positives are fine — better to point
    at three things than miss the broken one.
    """
    v = d["value"]
    unit = (d["unit"] or "").lower()
    name = d["name"].lower()
    if v is None:
        return "  ← read failed"
    if isinstance(v, (int, float)):
        # Common 16-bit sentinel for "no data" / unconfigured channel
        if d["type"] in ("u16", "s16") and int(v) == 0xFFFF:
            return "  ← 0xFFFF sentinel (likely unconfigured on this panel)"
        if d["type"] in ("u32", "s32") and int(v) == 0xFFFFFFFF:
            return "  ← 0xFFFFFFFF sentinel (likely unconfigured)"
        if "pct" in unit and v > 100:
            return f"  ← {v} > 100% (scale or register-meaning mismatch?)"
        if "psi" in unit and v > 200:
            return f"  ← {v} psi unusually high"
        if name == "rpm" and v > 3600:
            return f"  ← {v} rpm above redline for a genset"
    return ""


def _compact_ranges(addrs: list[int]) -> str:
    """Compress [1,2,3,5,6,9] -> '0x0001-0x0003, 0x0005-0x0006, 0x0009'."""
    if not addrs:
        return ""
    addrs = sorted(addrs)
    out: list[str] = []
    start = prev = addrs[0]
    for a in addrs[1:]:
        if a == prev + 1:
            prev = a
            continue
        out.append(f"0x{start:04X}" if start == prev else f"0x{start:04X}-0x{prev:04X}")
        start = prev = a
    out.append(f"0x{start:04X}" if start == prev else f"0x{start:04X}-0x{prev:04X}")
    return ", ".join(out)


if __name__ == "__main__":
    sys.exit(main())
