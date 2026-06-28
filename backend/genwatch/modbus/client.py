"""Modbus client wrapper.

Three implementations behind one interface:
  - SerialModbusClient:  pymodbus AsyncModbusSerialClient over /dev/ttyUSB0
  - TcpRtuModbusClient:  pymodbus AsyncModbusTcpClient with the RTU framer,
                         used with a Lantronix-style network serial bridge
                         (raw-TCP tunnel; *not* Modbus/TCP)
  - MockModbusClient:    synthesised registers + state machine for dev/CI

All three expose the same async methods so the poller doesn't care which
is attached. All reads return a ModbusResult that carries either values
or an error reason; the poller updates comms health based on the result.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import socket as _socket
import time
from dataclasses import dataclass
from typing import Protocol

from .registers import ControlDef, RegisterMap

log = logging.getLogger("genwatch.modbus.client")


# TCP keepalive parameters (Linux). The Lantronix bridge (and any NAT or
# stateful firewall between it and the Pi) can silently drop a TCP
# connection without sending FIN/RST — switch reboots, idle timeouts,
# the bridge itself rebooting. Without keepalive we only notice when a
# read times out, then burn the entire retry budget before failing.
# With these settings the kernel drops a wedged socket after roughly
# KEEPIDLE + KEEPCNT * KEEPINTVL ≈ 60 s of silence; the next poll then
# fails fast on a dead socket and _ensure_connected triggers a clean
# reconnect instead of pymodbus sitting on a zombie peer.
_TCP_KEEPIDLE_S = 30
_TCP_KEEPINTVL_S = 10
_TCP_KEEPCNT = 3


def _apply_tcp_keepalive(transport, host: str, port: int) -> None:
    """Enable SO_KEEPALIVE + tune timings on the underlying asyncio socket.

    No-op if the platform doesn't expose the Linux TCP_KEEP* options, or
    if the transport hasn't exposed a socket (rare; future pymodbus
    versions may swap the underlying transport). Failure here must not
    prevent the connection from being used — keepalive is a defence in
    depth, not a correctness requirement.
    """
    if transport is None:
        return
    try:
        sock = transport.get_extra_info("socket")
    except Exception:  # noqa: BLE001
        sock = None
    if sock is None:
        return
    try:
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
        if hasattr(_socket, "TCP_KEEPIDLE"):  # Linux
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE, _TCP_KEEPIDLE_S)
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL, _TCP_KEEPINTVL_S)
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT, _TCP_KEEPCNT)
        log.debug(
            "TCP keepalive enabled for %s:%d (idle=%ds intvl=%ds cnt=%d)",
            host, port, _TCP_KEEPIDLE_S, _TCP_KEEPINTVL_S, _TCP_KEEPCNT,
        )
    except OSError as e:
        log.warning("could not apply TCP keepalive to %s:%d: %s", host, port, e)


@dataclass
class ModbusResult:
    ok: bool
    words: list[int] | None = None
    error: str | None = None
    elapsed_ms: float = 0.0

    @classmethod
    def success(cls, words: list[int], elapsed_ms: float) -> "ModbusResult":
        return cls(ok=True, words=list(words), elapsed_ms=elapsed_ms)

    @classmethod
    def failure(cls, reason: str, elapsed_ms: float = 0.0) -> "ModbusResult":
        return cls(ok=False, error=reason, elapsed_ms=elapsed_ms)


class ModbusClient(Protocol):
    async def connect(self) -> bool: ...
    async def close(self) -> None: ...
    async def read(self, addr: int, count: int, fc: int = 3) -> ModbusResult: ...
    async def write(
        self,
        addr: int,
        value: int | None = None,
        fc: int = 6,
        values: list[int] | None = None,
    ) -> ModbusResult: ...


def _coerce_write_args(
    value: int | None, values: list[int] | None, fc: int
) -> tuple[list[int], str | None]:
    """Resolve (value, values) into a definitive list of words, or an error."""
    if values is not None and value is not None:
        return [], "both_value_and_values_provided"
    if values is None and value is None:
        return [], "no_value_provided"
    word_list = list(values) if values is not None else [int(value)]  # type: ignore[arg-type]
    if not word_list:
        return [], "empty_values"
    if fc == 6 and len(word_list) != 1:
        return [], "fc6_requires_single_value"
    return word_list, None


# ─── Real client ─────────────────────────────────────────────────────────


class SerialModbusClient:
    def __init__(
        self,
        *,
        device: str,
        baud: int,
        parity: str,
        stopbits: int,
        bytesize: int,
        timeout_s: float,
        slave: int,
        retries: int,
        backoff_s: list[float],
    ):
        self.device = device
        self.baud = baud
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self.timeout_s = timeout_s
        self.slave = slave
        self.retries = max(0, retries)
        self.backoff_s = list(backoff_s) or [0.25]
        self._client = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        # Defer import so the package can be imported without pymodbus
        # installed (useful for type-checking on the dev host).
        from pymodbus.client import AsyncModbusSerialClient  # type: ignore

        self._client = AsyncModbusSerialClient(
            port=self.device,
            baudrate=self.baud,
            parity=self.parity,
            stopbits=self.stopbits,
            bytesize=self.bytesize,
            timeout=self.timeout_s,
        )
        ok = await self._client.connect()
        if ok:
            log.info(
                "Modbus connected: %s @ %d %s%d%d slave=%d",
                self.device, self.baud, self.parity, self.bytesize, self.stopbits, self.slave,
            )
        else:
            log.error("Modbus connect failed on %s", self.device)
        return bool(ok)

    async def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _read_once(self, addr: int, count: int, fc: int):
        assert self._client is not None
        if fc == 3:
            rr = await self._client.read_holding_registers(address=addr, count=count, slave=self.slave)
        elif fc == 4:
            rr = await self._client.read_input_registers(address=addr, count=count, slave=self.slave)
        else:
            raise ValueError(f"unsupported read fc {fc}")
        return rr

    async def read(self, addr: int, count: int, fc: int = 3) -> ModbusResult:
        if self._client is None:
            return ModbusResult.failure("not_connected")
        attempts = self.retries + 1
        last_err = "unknown"
        t0 = time.perf_counter()
        for i in range(attempts):
            # Lock acquired per-attempt (not across the entire retry
            # chain) so a queued control write can pre-empt our retry
            # backoff. Without this, a degraded comms link can hold the
            # lock for ~5s across three failing attempts plus their
            # backoffs — exactly when an operator's Stop command is
            # most likely to be queued, and exactly when we want it
            # serviced quickly. The Modbus wire transaction is still
            # serialized (one transaction per lock acquisition); we're
            # only giving up the lock during sleeps.
            async with self._lock:
                try:
                    rr = await asyncio.wait_for(
                        self._read_once(addr, count, fc),
                        timeout=self.timeout_s + 0.2,
                    )
                    if rr is None:
                        last_err = "no_response"
                    elif rr.isError():
                        last_err = f"exc_{getattr(rr, 'exception_code', '?')}"
                    elif rr.registers is None or len(rr.registers) != count:
                        # Short/truncated frame. A Lantronix bridge under
                        # packet fragmentation can return fewer registers
                        # than requested without isError() being set.
                        # Treat as a failure so it counts against comms
                        # health and triggers the fan-out — never accept a
                        # short read as success (it would zero-extend
                        # downstream decoding and read "healthy").
                        last_err = "short_read"
                    else:
                        return ModbusResult.success(rr.registers, (time.perf_counter() - t0) * 1000)
                except asyncio.TimeoutError:
                    last_err = "timeout"
                except Exception as e:  # noqa: BLE001
                    last_err = type(e).__name__
            # Lock released. Inter-attempt backoff runs outside the
            # lock so any task waiting on it (typically a control
            # write) can jump the queue.
            if i < attempts - 1:
                backoff = self.backoff_s[min(i, len(self.backoff_s) - 1)]
                await asyncio.sleep(backoff)
        return ModbusResult.failure(last_err, (time.perf_counter() - t0) * 1000)

    async def write(
        self,
        addr: int,
        value: int | None = None,
        fc: int = 6,
        values: list[int] | None = None,
    ) -> ModbusResult:
        if self._client is None:
            return ModbusResult.failure("not_connected")
        words, err = _coerce_write_args(value, values, fc)
        if err is not None:
            return ModbusResult.failure(err)
        async with self._lock:
            t0 = time.perf_counter()
            try:
                if fc == 6:
                    rr = await asyncio.wait_for(
                        self._client.write_register(address=addr, value=words[0], slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                elif fc == 16:
                    rr = await asyncio.wait_for(
                        self._client.write_registers(address=addr, values=words, slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                else:
                    return ModbusResult.failure(f"unsupported_write_fc_{fc}")
                if rr is None or rr.isError():
                    return ModbusResult.failure(
                        f"write_failed_{getattr(rr, 'exception_code', '?')}",
                        (time.perf_counter() - t0) * 1000,
                    )
                return ModbusResult.success(words, (time.perf_counter() - t0) * 1000)
            except asyncio.TimeoutError:
                return ModbusResult.failure("timeout", (time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001
                return ModbusResult.failure(type(e).__name__, (time.perf_counter() - t0) * 1000)


# ─── TCP-RTU client (Lantronix / ser2net / socat bridges) ──────────────


class TcpRtuModbusClient:
    """Modbus RTU framed over a raw TCP socket.

    The wire format is identical to RS-232 RTU; the only difference is
    that the bytes travel over TCP to a terminal server (Lantronix UDS,
    EDS, xDirect; Moxa NPort; Digi PortServer; ser2net; etc.) which
    drops them onto the physical serial port wired to the H-100. This is
    NOT Modbus/TCP — Modbus/TCP uses a different frame (MBAP header, no
    CRC) and a different default port (502). Lantronix raw-TCP mode is
    port 10001 by default.

    Reconnects opportunistically on the next read after a failure: TCP
    sockets can drop silently (Lantronix idle timeouts, switch reboots),
    and unlike a kernel serial port the file handle won't recover on
    its own.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        framer: str,
        timeout_s: float,
        connect_timeout_s: float,
        slave: int,
        retries: int,
        backoff_s: list[float],
    ):
        self.host = host
        self.port = port
        self.framer = framer
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.slave = slave
        self.retries = max(0, retries)
        self.backoff_s = list(backoff_s) or [0.25]
        self._client = None
        self._lock = asyncio.Lock()

    def _build_client(self):
        from pymodbus.client import AsyncModbusTcpClient  # type: ignore
        from pymodbus.framer import FramerType  # type: ignore

        framer = FramerType.RTU if self.framer == "rtu" else FramerType.SOCKET
        return AsyncModbusTcpClient(
            host=self.host,
            port=self.port,
            framer=framer,
            timeout=self.timeout_s,
        )

    async def connect(self) -> bool:
        self._client = self._build_client()
        try:
            ok = await asyncio.wait_for(self._client.connect(), timeout=self.connect_timeout_s)
        except asyncio.TimeoutError:
            log.error("Modbus TCP connect to %s:%d timed out after %.1fs",
                      self.host, self.port, self.connect_timeout_s)
            return False
        if ok:
            self._enable_keepalive()
            log.info("Modbus TCP-RTU connected: %s:%d slave=%d framer=%s",
                     self.host, self.port, self.slave, self.framer)
        else:
            log.error("Modbus TCP connect failed to %s:%d", self.host, self.port)
        return bool(ok)

    def _enable_keepalive(self) -> None:
        # pymodbus 3.x exposes the asyncio transport at client.ctx.transport.
        # Walk defensively so a future internal rename doesn't crash the
        # connection — keepalive is a hardening measure, not load-bearing.
        client = self._client
        if client is None:
            return
        transport = getattr(getattr(client, "ctx", None), "transport", None)
        if transport is None:
            transport = getattr(client, "transport", None)
        _apply_tcp_keepalive(transport, self.host, self.port)

    async def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _ensure_connected(self) -> bool:
        if self._client is None:
            return False
        if getattr(self._client, "connected", False):
            return True
        # Best-effort reconnect; the next call will retry if this fails.
        try:
            ok = bool(await asyncio.wait_for(
                self._client.connect(), timeout=self.connect_timeout_s,
            ))
        except Exception as e:  # noqa: BLE001  (covers asyncio.TimeoutError)
            # Preserve the underlying cause for diagnostics — collapsing
            # every reconnect failure to a bare "tcp_disconnected" upstream
            # hid DNS/refused/SSL distinctions on a flaky bridge.
            log.debug("reconnect to %s:%d failed: %s", self.host, self.port, e)
            return False
        if ok:
            # Re-apply keepalive on the freshly created socket.
            self._enable_keepalive()
        return ok

    async def _read_once(self, addr: int, count: int, fc: int):
        assert self._client is not None
        if fc == 3:
            rr = await self._client.read_holding_registers(address=addr, count=count, slave=self.slave)
        elif fc == 4:
            rr = await self._client.read_input_registers(address=addr, count=count, slave=self.slave)
        else:
            raise ValueError(f"unsupported read fc {fc}")
        return rr

    async def read(self, addr: int, count: int, fc: int = 3) -> ModbusResult:
        if self._client is None:
            return ModbusResult.failure("not_connected")
        attempts = self.retries + 1
        last_err = "unknown"
        t0 = time.perf_counter()
        for i in range(attempts):
            # Per-attempt lock (see SerialModbusClient.read for rationale)
            # — inter-attempt backoff runs outside so a queued control
            # write can pre-empt the retry chain on a degraded link.
            async with self._lock:
                if not await self._ensure_connected():
                    last_err = "tcp_disconnected"
                else:
                    try:
                        rr = await asyncio.wait_for(
                            self._read_once(addr, count, fc),
                            timeout=self.timeout_s + 0.2,
                        )
                        if rr is None:
                            last_err = "no_response"
                        elif rr.isError():
                            last_err = f"exc_{getattr(rr, 'exception_code', '?')}"
                        elif rr.registers is None or len(rr.registers) != count:
                            # Short/truncated frame (see SerialModbusClient
                            # for rationale) — fail so it triggers fan-out
                            # and counts against comms health rather than
                            # zero-extending the decode.
                            last_err = "short_read"
                        else:
                            return ModbusResult.success(rr.registers, (time.perf_counter() - t0) * 1000)
                    except asyncio.TimeoutError:
                        last_err = "timeout"
                    except Exception as e:  # noqa: BLE001
                        last_err = type(e).__name__
            if i < attempts - 1:
                backoff = self.backoff_s[min(i, len(self.backoff_s) - 1)]
                await asyncio.sleep(backoff)
        return ModbusResult.failure(last_err, (time.perf_counter() - t0) * 1000)

    async def write(
        self,
        addr: int,
        value: int | None = None,
        fc: int = 6,
        values: list[int] | None = None,
    ) -> ModbusResult:
        if self._client is None:
            return ModbusResult.failure("not_connected")
        words, err = _coerce_write_args(value, values, fc)
        if err is not None:
            return ModbusResult.failure(err)
        async with self._lock:
            t0 = time.perf_counter()
            if not await self._ensure_connected():
                return ModbusResult.failure("tcp_disconnected", (time.perf_counter() - t0) * 1000)
            try:
                if fc == 6:
                    rr = await asyncio.wait_for(
                        self._client.write_register(address=addr, value=words[0], slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                elif fc == 16:
                    rr = await asyncio.wait_for(
                        self._client.write_registers(address=addr, values=words, slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                else:
                    return ModbusResult.failure(f"unsupported_write_fc_{fc}")
                if rr is None or rr.isError():
                    return ModbusResult.failure(
                        f"write_failed_{getattr(rr, 'exception_code', '?')}",
                        (time.perf_counter() - t0) * 1000,
                    )
                return ModbusResult.success(words, (time.perf_counter() - t0) * 1000)
            except asyncio.TimeoutError:
                return ModbusResult.failure("timeout", (time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001
                return ModbusResult.failure(type(e).__name__, (time.perf_counter() - t0) * 1000)


# ─── Mock client ─────────────────────────────────────────────────────────


class MockModbusClient:
    """Synthetic Modbus slave so the service runs without hardware.

    State is driven by a small in-process state machine that responds to
    writes on the control registers (0x00A0..A3) by transitioning the
    engine state, mirroring the real H-100's behavior.
    """

    def __init__(self, regmap: RegisterMap):
        self.regmap = regmap
        self._regs: dict[int, int] = {}
        self._state = "stopped"
        self._state_started = time.monotonic()
        self._cool_until: float | None = None
        self._exercise_until: float | None = None
        self._alarm_active = 0
        self._connected = False
        self._lock = asyncio.Lock()
        self._inject_alarm: int | None = None

    async def connect(self) -> bool:
        self._connected = True
        log.info("Modbus MOCK client started (no real RS-485)")
        return True

    async def close(self) -> None:
        self._connected = False

    def _output_status_1_bits(self) -> int:
        """Synthesise the H-100's Output Status 1 bitfield from internal state."""
        bits = 0
        if self._state in ("running", "exercising", "cooling"):
            bits |= 0x2000  # Generator Running
        if self._state == "running":
            bits |= 0x0800  # Ready for Load
        if self._state == "stopped":
            bits |= 0x0100  # Stopped
            bits |= 0x0400  # Ready to Run
        if self._state == "alarm":
            bits |= 0x8000  # Common Alarm
            bits |= 0x0200  # Stopped in Alarm
        return bits

    def _output_status_7_bits(self) -> int:
        """Synthesise Output Status 7 from internal state."""
        bits = 0
        if self._state == "cranking":
            bits |= 0x1000  # Cranking
        if self._state == "cooling":
            bits |= 0x2000  # In Cool Down
        if self._state == "exercising":
            bits |= 0x0020  # Internal Exercise Active
        return bits

    def _advance(self) -> None:
        now = time.monotonic()
        # auto-transitions for realism
        if self._state == "cranking" and now - self._state_started > 4.0:
            self._set_state("running")
        if self._state == "cooling" and self._cool_until and now > self._cool_until:
            self._set_state("stopped")
            self._cool_until = None
        if self._state == "exercising" and self._exercise_until and now > self._exercise_until:
            self._set_state("cooling")
            self._cool_until = now + 6.0

        # inject test alarm if requested
        if self._inject_alarm is not None:
            self._alarm_active = self._inject_alarm
            self._set_state("alarm")
            self._inject_alarm = None

    def _set_state(self, s: str) -> None:
        if s != self._state:
            log.debug("mock state %s -> %s", self._state, s)
            self._state = s
            self._state_started = time.monotonic()

    def _synth_value(self, name: str) -> int:
        """Return the *raw* (pre-scale) register value for a name."""
        # Smooth jitter — matches the design's plausible curves.
        t = time.monotonic()
        wob = math.sin(t * 0.6) * 0.4 + math.sin(t * 0.17) * 0.3
        running = self._state in ("running", "exercising", "cooling")

        # Bitfield state/alarm registers
        if name == "output_status_1":
            return self._output_status_1_bits()
        if name == "output_status_7":
            return self._output_status_7_bits()
        if name == "input_status_1":
            return 0x8000  # Switch In Auto
        if name in ("output_status_2", "output_status_3", "output_status_4",
                    "output_status_5", "output_status_6", "output_status_8"):
            # Map injected alarm onto output_status_2 bit 0x1000 (Coolant Temp High Alarm)
            if name == "output_status_2" and self._alarm_active:
                return 0x1000
            return 0
        if name == "active_alarm_count":
            return 1 if self._alarm_active else 0
        if name == "engine_status_code":
            return 0  # H-100 status code (string is the canonical source)
        if name == "key_switch_state":
            return 0xFF00  # Auto
        if name == "quiettest_status":
            return 1 if self._state == "exercising" else 0
        if name == "rpm":
            base = 1800 if running else 0
            if self._state == "cranking":
                base = 400
            return max(0, int(base + wob * 8))
        if name == "oil_temp":
            return int((215 if running else 100) + wob * 2)
        if name == "oil_pressure":
            return int(max(0, (62 if running else 0) + wob * 1.5))
        if name == "coolant_temp":
            return int((188 if running else 95) + wob * 2)
        if name == "coolant_level":
            return 95
        if name == "throttle_position":
            return int(max(0, (50 if running else 0) + wob * 3))
        if name == "o2_sensor":
            return 21
        if name == "batt_charge_current":
            return int(max(0, 2 + wob * 0.5))
        if name == "battery_volts":
            # Scale 0.01: 1380 → 13.80 V running, 1260 → 12.60 V resting
            return int((1380 if running else 1260) + wob * 5)
        if name == "frequency":
            # Scale 0.1: 600 → 60.0 Hz
            return int((60.0 + wob * 0.05) * 10) if running else 0
        # Scale loaded-running synthetic output to the site's nameplate
        # rating so dev with a 350 kW config doesn't show 142 kW (which
        # would be 41 % load and look misleading). At 480 V 3-φ pf 0.95,
        # full-load amps ≈ rating_kw × 1.27.
        rated_kw = max(1, self.regmap.site.rating_kw)
        loaded_kw = int(rated_kw * 0.71)              # ~71% loaded
        loaded_amps = int(rated_kw * 0.90)            # ~71% of full-load amps
        if name == "total_kw":
            if self._state == "exercising":
                return max(0, int(6 + wob * 3))
            return max(0, int((loaded_kw if running else 0) + wob * 4))
        if name == "power_factor":
            # Scale 0.01: 95 → 0.95
            return 95 if running else 0
        if name == "gen_voltage_ab":
            return int(480 + wob * 1.2) if running else 0
        if name == "gen_voltage_bc":
            return int(481 + wob * 1.1) if running else 0
        if name == "gen_voltage_ca":
            return int(479 + wob * 1.0) if running else 0
        if name == "avg_voltage":
            return int(480 + wob * 1.0) if running else 0
        if name == "gen_current_a":
            base = 8 if self._state == "exercising" else (loaded_amps if running else 0)
            return max(0, int(base + wob * 3))
        if name == "gen_current_b":
            base = 7 if self._state == "exercising" else (loaded_amps - 4 if running else 0)
            return max(0, int(base + wob * 3))
        if name == "gen_current_c":
            base = 9 if self._state == "exercising" else (loaded_amps + 4 if running else 0)
            return max(0, int(base + wob * 3))
        if name == "avg_current":
            base = 8 if self._state == "exercising" else (loaded_amps if running else 0)
            return max(0, int(base + wob * 3))
        if name == "run_hours":
            # RAW register is tenths of an hour (scale 0.1) — emit ~184760 so the
            # decode yields a realistic ~18476.0 h and the /10 divider is actually
            # exercised (a raw value already in whole hours would mask it).
            return int(184760 + (time.monotonic() / 3.6) % 1000)
        if name == "fuel_level_pct":
            return 78
        return 0

    def _read_addr(self, addr: int) -> int:
        # Find the register def whose contiguous range contains addr.
        for r in self.regmap.registers:
            if r.addr <= addr < r.addr + r.words:
                val = self._synth_value(r.name)
                if r.words == 2:
                    # Big-endian: word 0 = high, word 1 = low
                    offset = addr - r.addr
                    return (val >> 16) & 0xFFFF if offset == 0 else val & 0xFFFF
                return val & 0xFFFF
        return 0

    def _apply_control_write(self, addr: int, words: list[int]) -> None:
        """Map a write at `addr` with these word values to a state transition."""
        # Match by (addr, values) against the regmap controls.
        match: ControlDef | None = None
        for c in self.regmap.controls.values():
            if c.addr != addr:
                continue
            if list(c.write_values) == words:
                match = c
                break
        if match is None:
            # Fallback: match by addr only (so unknown bit patterns at the start
            # register still produce a noticeable mock behaviour for debugging).
            match = next((c for c in self.regmap.controls.values() if c.addr == addr), None)
            if match is None:
                return
        log.info("mock control: %s (addr=0x%04X, words=%s)", match.name, addr, words)
        if match.name == "remote_start":
            self._set_state("cranking")
        elif match.name == "remote_stop":
            if self._state in ("running", "exercising", "cranking"):
                self._cool_until = time.monotonic() + 6.0
                self._set_state("cooling")
            else:
                self._set_state("stopped")
        elif match.name == "transfer":
            self._set_state("cranking")
        elif match.name == "exercise":
            self._exercise_until = time.monotonic() + 12.0
            self._set_state("exercising")
        elif match.name == "ack_alarm":
            self._alarm_active = 0
            if self._state == "alarm":
                self._set_state("stopped")

    async def read(self, addr: int, count: int, fc: int = 3) -> ModbusResult:
        if not self._connected:
            return ModbusResult.failure("not_connected")
        async with self._lock:
            self._advance()
            # Simulate a small read latency so degraded-comms tests work
            await asyncio.sleep(0.005 + random.random() * 0.01)
            words = [self._read_addr(addr + i) for i in range(count)]
            return ModbusResult.success(words, 12.0)

    async def write(
        self,
        addr: int,
        value: int | None = None,
        fc: int = 6,
        values: list[int] | None = None,
    ) -> ModbusResult:
        if not self._connected:
            return ModbusResult.failure("not_connected")
        words, err = _coerce_write_args(value, values, fc)
        if err is not None:
            return ModbusResult.failure(err)
        async with self._lock:
            await asyncio.sleep(0.01)
            self._apply_control_write(addr, words)
            for i, w in enumerate(words):
                self._regs[addr + i] = w
            return ModbusResult.success(words, 12.0)

    # ---- mock helpers (not part of the Protocol) ----
    def inject_alarm(self, code: int) -> None:
        self._inject_alarm = code

    def clear_alarm(self) -> None:
        self._alarm_active = 0
        if self._state == "alarm":
            self._set_state("stopped")
