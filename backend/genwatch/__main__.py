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
            # Trust X-Forwarded-* only from the loopback proxy by default
            # (Caddy / `tailscale serve` on the same Pi). This pins what was
            # previously an implicit uvicorn default: a LAN client can no
            # longer spoof X-Forwarded-Proto to flip the Secure cookie, nor
            # X-Forwarded-For to forge the rate-limiter / audit source IP.
            # Widen via GENWATCH_TRUSTED_PROXIES only with a header-
            # sanitizing proxy you control.
            proxy_headers=True,
            forwarded_allow_ips=os.environ.get("GENWATCH_TRUSTED_PROXIES", "127.0.0.1"),
        )
        return 0

    if cmd == "hash":
        from .services.auth import hash_password
        # Two modes:
        #
        # 1. Interactive (recommended) — `genwatch hash` with no argv.
        #    Reads the password from stdin via getpass with no echo, so
        #    the plaintext never lands in ~/.bash_history, never appears
        #    in `ps aux`, and isn't captured by SSH session recorders
        #    that snapshot argv on exec. Re-prompts on mismatch.
        #
        # 2. Argv (legacy/scripting) — `genwatch hash <password>`. The
        #    plaintext IS visible in shell history and (briefly) to
        #    `ps`. We keep this path for non-interactive provisioning
        #    (Ansible, cloud-init, install.sh-driven hand-offs) but
        #    print a stderr warning so an interactive operator who
        #    didn't know about the safer form sees it.
        if len(args) >= 2:
            print(
                "warning: passing the password on the command line exposes "
                "it via shell history and `ps aux`. Run `genwatch hash` "
                "with no argument to be prompted instead.",
                file=sys.stderr,
            )
            print(hash_password(args[1]))
            return 0
        # Interactive prompt. Refuse to run when stdin isn't a TTY —
        # otherwise piping a password in defeats the point (`echo pw |
        # genwatch hash` would still leak via the calling shell's
        # history). Operators who really want non-interactive hashing
        # should use the argv form with the warning.
        import getpass
        if not sys.stdin.isatty():
            print(
                "error: `genwatch hash` with no argument requires an "
                "interactive terminal. For non-interactive use, pass "
                "the password as an argument (less secure — see the "
                "warning that prints in that mode).",
                file=sys.stderr,
            )
            return 2
        try:
            pw1 = getpass.getpass("New password: ")
            pw2 = getpass.getpass("Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 130
        if pw1 != pw2:
            print("error: passwords do not match", file=sys.stderr)
            return 1
        if not pw1:
            print("error: password cannot be empty", file=sys.stderr)
            return 1
        print(hash_password(pw1))
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
            # The old check was buggy on two fronts: g.gr_mem contains
            # usernames (strings), so `os.getuid() in g.gr_mem` compared
            # int↔str and was always False; and the `or g.gr_name ==
            # "dialout"` clause made the comprehension unconditionally
            # include the dialout group, making in_dialout=True on every
            # Debian system. Net effect: the "Likely cause: not in
            # dialout" hint never surfaced when it was actually the
            # cause. Use the dialout group's member list directly.
            try:
                import grp
                import pwd
                me = pwd.getpwuid(os.getuid()).pw_name
                dialout_members = grp.getgrnam("dialout").gr_mem
                in_dialout = me in (dialout_members or [])
            except (KeyError, Exception):  # noqa: BLE001
                # KeyError if the dialout group doesn't exist on this
                # system (very unusual on Debian/Pi). Any other failure
                # → assume not in group so we still surface the hint.
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
    print("== Modbus scan ==")
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
                        hi, lo = (v >> 8) & 0xFF, v & 0xFF
                        if 0x20 <= hi <= 0x7E and 0x20 <= lo <= 0x7E:
                            buf.append(hi)
                            buf.append(lo)
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
    p.add_argument("--html", action="store_true",
                   help="emit a printable HTML cross-check sheet "
                        "(pre-filled with current readings; save and open in a "
                        "browser to print)")
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

    # Shared between HTML and text branches
    if settings.transport == "tcp":
        link = f"tcp {settings.modbus_tcp.host}:{settings.modbus_tcp.port}"
    else:
        link = f"serial {settings.serial.device} {settings.serial.baud}"

    # ─── HTML printable cross-check sheet ─────────────────────────────
    if opts.html:
        html = _render_panel_html(
            decoded=decoded,
            value_map=value_map,
            rm=rm,
            link=link,
            fired_state=fired_state,
            fired_rule=fired_rule,
            bit_meanings=bit_meanings,
            elapsed_ms=elapsed_ms,
            errs=errs,
        )
        print(html)
        return 0 if not errs else 1

    # ─── Text report ──────────────────────────────────────────────────
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


def _render_panel_html(
    *,
    decoded: list[dict],
    value_map: dict[str, float | int],
    rm,
    link: str,
    fired_state: str,
    fired_rule,
    bit_meanings: dict[str, dict[int, str]],
    elapsed_ms: float,
    errs: list[str],
) -> str:
    """Generate a single-page printable cross-check sheet (HTML).

    Pre-fills every reading from the live Modbus poll so the operator
    walks to the H-100 panel with a checklist of "GenWatch reads X — does
    your panel show X?" and a blank column to write the panel value into
    when it doesn't match. The sheet is laid out for US Letter / A4 and
    has print-only CSS so saving as PDF from a browser produces a clean
    page.
    """
    import html as _html
    from datetime import datetime, timezone

    from . import __version__

    by_name = {d["name"]: d for d in decoded}

    def get_val(name: str) -> str:
        d = by_name.get(name)
        if d is None or d["value"] is None:
            return "—"
        return _fmt_value(d["value"], d.get("scale", 1.0), d.get("unit"))

    def get_unit(name: str) -> str:
        d = by_name.get(name)
        return (d.get("unit") or "") if d else ""

    def get_raw(name: str) -> str:
        d = by_name.get(name)
        if d is None:
            return ""
        return _fmt_raw(d["raw_words"])

    active_alarms = rm.derive_active_alarms(value_map)
    site_id = _html.escape(getattr(rm.site, "id", "SITE-1"))
    site_name = _html.escape(getattr(rm.site, "name", "Generac H-100"))
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    fired_rule_text = (
        f"{fired_rule.register} bit 0x{fired_rule.mask:04X} set" if fired_rule else "no rule matched"
    )

    # Section 1: warnings to confirm/deny on the panel
    warning_rows = "".join(
        f"""
        <tr class="warn">
          <td><strong>{_html.escape(ab.code)}</strong><br><span class="desc">{_html.escape(ab.desc)}</span></td>
          <td class="mono">{_html.escape(ab.register)} 0x{ab.mask:04X}</td>
          <td class="chk">☐ Yes &nbsp;&nbsp; ☐ No</td>
          <td class="write"></td>
        </tr>"""
        for ab in active_alarms
    ) or """
        <tr><td colspan="4" style="text-align:center; color:#666;">
          GenWatch reports no active warnings. Confirm the panel agrees:
          ☐ Panel also shows no warnings &nbsp;&nbsp;&nbsp;
          ☐ Panel shows warnings (write here): _______________________
        </td></tr>"""

    # Section 2: high-confidence numeric cross-checks
    numeric_targets = [
        ("battery_volts",       "Battery voltage",      "0.1 V"),
        ("run_hours",           "Run hours (lifetime)", "1 h"),
        ("coolant_temp",        "Coolant temperature",  "1 °"),
        ("oil_temp",            "Oil temperature",      "1 °"),
        ("fuel_level_pct",      "Fuel level",           "1 %"),
    ]
    numeric_rows = "".join(
        f"""
        <tr>
          <td>{_html.escape(label)}</td>
          <td class="mono num">{_html.escape(get_val(name))} {_html.escape(get_unit(name))}</td>
          <td class="write"></td>
          <td class="chk">☐ Match &nbsp; ☐ Off</td>
        </tr>"""
        for name, label, _ in numeric_targets
    )

    # Section 3: suspicious / diagnostic values
    diag_rows = []
    cl = by_name.get("coolant_level")
    if cl and cl["value"] is not None:
        diag_rows.append(f"""
        <tr>
          <td>Coolant level<br><span class="desc">GenWatch reads {cl['value']} (impossible if % — likely scale issue)</span></td>
          <td class="write"></td>
          <td class="note">If panel shows ~{cl['value']/10:.1f} → set <span class="mono">scale: 0.1</span><br>If different → register may not mean coolant level</td>
        </tr>""")
    qts = by_name.get("quiettest_status")
    if qts and qts["value"] is not None:
        diag_rows.append(f"""
        <tr>
          <td>Quiet-test status<br><span class="desc">GenWatch reads {qts['value']} (0xFFFF = "unconfigured" sentinel)</span></td>
          <td class="write">Has a quiet-test ever been run? ☐ Yes ☐ No / unknown</td>
          <td class="note">If "no", the 0xFFFF is expected; will populate on first quiet-test</td>
        </tr>""")
    esc = by_name.get("engine_status_code")
    if esc and esc["value"] is not None:
        diag_rows.append(f"""
        <tr>
          <td>Engine status code<br><span class="desc">GenWatch reads {esc['value']}</span></td>
          <td class="write">Panel home screen status text / code:</td>
          <td class="note">Tells us whether 0x0132 is the actual status enum on your panel</td>
        </tr>""")
    kss = by_name.get("key_switch_state")
    if kss and kss["value"] is not None:
        diag_rows.append(f"""
        <tr>
          <td>Key switch position<br><span class="desc">GenWatch raw 0x{int(kss['value']) & 0xFFFF:04X}</span></td>
          <td class="write">Physical key position:&nbsp;&nbsp;☐ OFF &nbsp;☐ AUTO &nbsp;☐ MANUAL</td>
          <td class="note">Lets us label the bits set in key_switch_state</td>
        </tr>""")
    diag_table = "".join(diag_rows) or "<tr><td colspan='3'>No diagnostic values flagged.</td></tr>"

    # Section 4: unknown bits — just a freeform notes box.
    # We list how many "?" bits there are per register so the operator knows
    # how much there is to look for, but don't ask them to enumerate.
    unknown_summary = []
    for d in decoded:
        if d["type"] != "bitfld" or d["value"] is None:
            continue
        v = int(d["value"]) & 0xFFFF
        meanings = bit_meanings.get(d["name"], {})
        unknown = [b for b in range(16) if (v >> b) & 1 and (1 << b) not in meanings]
        if unknown:
            bit_list = ", ".join(f"0x{1 << b:04X}" for b in sorted(unknown, reverse=True))
            unknown_summary.append(
                f"<li><span class='mono'>{_html.escape(d['name'])}</span>: {bit_list}</li>"
            )
    unknown_html = "".join(unknown_summary) or "<li>None — every set bit has a known meaning. Nice.</li>"

    # Engine readings summary at the top
    state_class = "state-alarm" if fired_state == "alarm" else (
        "state-running" if fired_state in ("running", "cranking", "exercising", "cooling") else "state-stopped"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>H-100 Panel Cross-Check — {site_name}</title>
<style>
  @page {{ size: Letter; margin: 0.5in; }}
  * {{ box-sizing: border-box; }}
  body {{
    font: 10.5pt/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #111; max-width: 7.5in; margin: 0 auto; padding: 0.25in;
  }}
  h1 {{ font-size: 18pt; margin: 0 0 4pt 0; }}
  h2 {{
    font-size: 12pt; margin: 14pt 0 6pt 0; padding: 3pt 6pt;
    background: #222; color: #fff; border-radius: 2pt;
  }}
  .meta {{ font-size: 9pt; color: #444; line-height: 1.5; margin-bottom: 8pt; }}
  .state {{
    display: inline-block; padding: 4pt 10pt; border-radius: 3pt;
    font-weight: 600; letter-spacing: 0.05em;
  }}
  .state-stopped {{ background: #e8eef5; color: #1a3a5c; }}
  .state-running {{ background: #e6f4e0; color: #2a5a1a; }}
  .state-alarm {{ background: #ffe0e0; color: #8a1a1a; }}
  table {{ width: 100%; border-collapse: collapse; margin: 4pt 0 8pt 0; font-size: 10pt; }}
  th, td {{ border: 1px solid #999; padding: 5pt 7pt; text-align: left; vertical-align: top; }}
  th {{ background: #efefef; font-weight: 600; font-size: 9.5pt; }}
  td.chk {{ width: 1.4in; text-align: center; white-space: nowrap; }}
  td.write {{ background: #fffdf2; min-width: 1.6in; min-height: 0.4in; }}
  td.note {{ font-size: 8.5pt; color: #555; font-style: italic; max-width: 2in; }}
  td.mono, .mono {{ font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9.5pt; }}
  td.num {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
  .warn td:first-child {{ border-left: 3pt solid #d97706; }}
  .desc {{ font-size: 9pt; color: #555; font-weight: normal; }}
  .signoff {{ margin-top: 18pt; }}
  .signoff td {{ border: none; padding: 8pt 6pt; }}
  .signoff td:first-child {{ width: 1.2in; font-weight: 600; }}
  .line {{ border-bottom: 1px solid #444; min-height: 18pt; width: 100%; display: block; }}
  .notes {{ background: #fffdf2; border: 1px solid #999; min-height: 0.8in; padding: 6pt 8pt; margin-top: 4pt; }}
  ul.bits {{ font-size: 9pt; margin: 4pt 0 8pt 0; padding-left: 18pt; color: #444; }}
  .footer {{ font-size: 8.5pt; color: #666; margin-top: 16pt; border-top: 1px solid #ccc; padding-top: 6pt; }}
  @media print {{ body {{ padding: 0; }} h2 {{ break-after: avoid; }} table {{ break-inside: avoid; }} }}
</style>
</head>
<body>

<h1>H-100 Panel Cross-Check</h1>
<div class="meta">
  Site: <strong>{site_name}</strong> ({site_id}) &nbsp;·&nbsp;
  Link: <span class="mono">{_html.escape(link)}</span> &nbsp;·&nbsp;
  Slave {rm.slave} &nbsp;·&nbsp;
  Captured: {timestamp}<br>
  Castle Generator Monitor v{__version__} &nbsp;·&nbsp;
  {len(decoded)} registers polled in {elapsed_ms:.0f} ms
  {('<br><strong style="color:#a00;">⚠ ' + str(len(errs)) + ' read warnings</strong>') if errs else ''}
</div>

<p>Engine state at capture: <span class="state {state_class}">{_html.escape(fired_state.upper())}</span>
&nbsp;<span class="desc">(matched: {_html.escape(fired_rule_text)})</span></p>

<h2>1. Active warnings — does the panel agree?</h2>
<p class="desc">If GenWatch flags a warning that the panel doesn't show, our bit-to-meaning
map for your H-100 revision is wrong and we'll fix it. If both agree the warning is set
but the panel hasn't cleared it, it's a latched warning from a previous run.</p>
<table>
  <colgroup>
    <col><col><col style="width:1.4in"><col style="width:1.8in">
  </colgroup>
  <thead>
    <tr><th>GenWatch reports</th><th>Source bit</th><th>Panel shows it?</th><th>If different, what does panel actually show?</th></tr>
  </thead>
  <tbody>
    {warning_rows}
  </tbody>
</table>

<h2>2. Numeric cross-check — high-confidence values</h2>
<p class="desc">Walk these in order. If any are far off, the corresponding register address
in <span class="mono">registers/h100.yaml</span> is wrong for your panel revision.</p>
<table>
  <colgroup>
    <col style="width:2.2in"><col style="width:1.4in"><col><col style="width:1.4in">
  </colgroup>
  <thead>
    <tr><th>Reading</th><th>GenWatch shows</th><th>Panel shows</th><th>Match?</th></tr>
  </thead>
  <tbody>
    {numeric_rows}
  </tbody>
</table>

<h2>3. Diagnostic values — write down what the panel says</h2>
<p class="desc">These are values GenWatch can't fully interpret without seeing what the
panel reports for them. Your answers here let us patch the register map.</p>
<table>
  <colgroup>
    <col style="width:2.4in"><col><col style="width:2in">
  </colgroup>
  <thead>
    <tr><th>Reading</th><th>What the panel shows</th><th>Why we ask</th></tr>
  </thead>
  <tbody>
    {diag_table}
  </tbody>
</table>

<h2>4. Unknown bits — only fill if anything on the panel looks unexpected</h2>
<p class="desc">These status bits are set right now but our map has no name for them on
your panel. They're almost always informational (battery-charger active, AC sensing OK,
etc.). Note here if any panel indicator changes that you can't explain from sections 1–3.</p>
<ul class="bits">
  {unknown_html}
</ul>
<div class="notes">Notes:</div>

<h2>5. Sign-off</h2>
<table class="signoff">
  <tr><td>Technician:</td><td><span class="line"></span></td></tr>
  <tr><td>Date / time:</td><td><span class="line"></span></td></tr>
  <tr><td>Generator s/n:</td><td><span class="line"></span></td></tr>
  <tr><td>Signature:</td><td><span class="line"></span></td></tr>
</table>

<div class="footer">
  Generated by <span class="mono">genwatch panel --html</span> ·
  Save this page (or print to PDF) and tick through it next to the H-100 LCD.
  Paste the filled-in answers back to your software contact and we'll patch
  <span class="mono">registers/h100.yaml</span> to match your panel exactly.
</div>

</body>
</html>
"""


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
