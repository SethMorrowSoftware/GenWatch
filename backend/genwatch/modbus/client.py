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
import time
from dataclasses import dataclass
from typing import Protocol

from .registers import RegisterMap

log = logging.getLogger("genwatch.modbus.client")


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
    async def write(self, addr: int, value: int, fc: int = 6) -> ModbusResult: ...


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
        async with self._lock:
            t0 = time.perf_counter()
            for i in range(attempts):
                try:
                    rr = await asyncio.wait_for(
                        self._read_once(addr, count, fc),
                        timeout=self.timeout_s + 0.2,
                    )
                    if rr is None:
                        last_err = "no_response"
                    elif rr.isError():
                        last_err = f"exc_{getattr(rr, 'exception_code', '?')}"
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

    async def write(self, addr: int, value: int, fc: int = 6) -> ModbusResult:
        if self._client is None:
            return ModbusResult.failure("not_connected")
        async with self._lock:
            t0 = time.perf_counter()
            try:
                if fc == 6:
                    rr = await asyncio.wait_for(
                        self._client.write_register(address=addr, value=value, slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                elif fc == 16:
                    rr = await asyncio.wait_for(
                        self._client.write_registers(address=addr, values=[value], slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                else:
                    return ModbusResult.failure(f"unsupported_write_fc_{fc}")
                if rr is None or rr.isError():
                    return ModbusResult.failure(
                        f"write_failed_{getattr(rr, 'exception_code', '?')}",
                        (time.perf_counter() - t0) * 1000,
                    )
                return ModbusResult.success([value], (time.perf_counter() - t0) * 1000)
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
            log.info("Modbus TCP-RTU connected: %s:%d slave=%d framer=%s",
                     self.host, self.port, self.slave, self.framer)
        else:
            log.error("Modbus TCP connect failed to %s:%d", self.host, self.port)
        return bool(ok)

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
            return bool(await asyncio.wait_for(
                self._client.connect(), timeout=self.connect_timeout_s,
            ))
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return False

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
        async with self._lock:
            t0 = time.perf_counter()
            for i in range(attempts):
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

    async def write(self, addr: int, value: int, fc: int = 6) -> ModbusResult:
        if self._client is None:
            return ModbusResult.failure("not_connected")
        async with self._lock:
            t0 = time.perf_counter()
            if not await self._ensure_connected():
                return ModbusResult.failure("tcp_disconnected", (time.perf_counter() - t0) * 1000)
            try:
                if fc == 6:
                    rr = await asyncio.wait_for(
                        self._client.write_register(address=addr, value=value, slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                elif fc == 16:
                    rr = await asyncio.wait_for(
                        self._client.write_registers(address=addr, values=[value], slave=self.slave),
                        timeout=self.timeout_s + 0.2,
                    )
                else:
                    return ModbusResult.failure(f"unsupported_write_fc_{fc}")
                if rr is None or rr.isError():
                    return ModbusResult.failure(
                        f"write_failed_{getattr(rr, 'exception_code', '?')}",
                        (time.perf_counter() - t0) * 1000,
                    )
                return ModbusResult.success([value], (time.perf_counter() - t0) * 1000)
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

    def _state_to_enum(self) -> int:
        # invert engine_state_map: pick the lowest raw value that maps to our state.
        for raw, name in sorted(self.regmap.engine_state_map.items()):
            if name == self._state:
                return raw
        return 0

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

        if name == "engine_state":
            return self._state_to_enum()
        if name == "alarm_state":
            return self._alarm_active
        if name == "switch_state":
            return 0b11 if self._state == "running" else 0b01
        if name == "rpm":
            base = 1800 if running else 0
            if self._state == "cranking":
                base = 400
            return max(0, int(base + wob * 8))
        if name == "oil_pressure":
            return int(max(0, (62 if running else 0) + wob * 1.5))
        if name == "coolant_temp":
            return int((188 if running else 95) + wob * 2)
        if name == "battery_volts":
            return int(((139 if running else 126) + wob * 0.5) * 1)  # raw, scale 0.1
        if name == "frequency":
            return int((60.0 + wob * 0.05) * 10) if running else 0
        if name == "total_kw":
            if self._state == "exercising":
                return max(0, int(6 + wob * 3))
            return max(0, int((142 if running else 0) + wob * 4))
        if name == "gen_voltage_ab":
            return int(480 + wob * 1.2) if running else 0
        if name == "gen_voltage_bc":
            return int(481 + wob * 1.1) if running else 0
        if name == "gen_voltage_ca":
            return int(479 + wob * 1.0) if running else 0
        if name == "gen_current_a":
            base = 8 if self._state == "exercising" else (172 if running else 0)
            return max(0, int(base + wob * 3))
        if name == "gen_current_b":
            base = 7 if self._state == "exercising" else (168 if running else 0)
            return max(0, int(base + wob * 3))
        if name == "gen_current_c":
            base = 9 if self._state == "exercising" else (176 if running else 0)
            return max(0, int(base + wob * 3))
        if name == "run_hours":
            # static-ish counter that ticks while running
            return int(18476 + (time.monotonic() / 36) % 100)
        if name == "start_count":
            return 142
        if name == "fuel_level_pct":
            return 78
        return 0

    def _read_addr(self, addr: int) -> int:
        # Find the register def whose contiguous range contains addr.
        for r in self.regmap.registers:
            if r.addr <= addr < r.addr + r.words:
                val = self._synth_value(r.name)
                if r.words == 2:
                    # word 0 = high, word 1 = low
                    offset = addr - r.addr
                    return (val >> 16) & 0xFFFF if offset == 0 else val & 0xFFFF
                return val & 0xFFFF
        return 0

    async def read(self, addr: int, count: int, fc: int = 3) -> ModbusResult:
        if not self._connected:
            return ModbusResult.failure("not_connected")
        async with self._lock:
            self._advance()
            # Simulate a small read latency so degraded-comms tests work
            await asyncio.sleep(0.005 + random.random() * 0.01)
            words = [self._read_addr(addr + i) for i in range(count)]
            return ModbusResult.success(words, 12.0)

    async def write(self, addr: int, value: int, fc: int = 6) -> ModbusResult:
        if not self._connected:
            return ModbusResult.failure("not_connected")
        async with self._lock:
            await asyncio.sleep(0.01)
            # Map control address back to its name
            ctl = next((c for c in self.regmap.controls.values() if c.addr == addr), None)
            if ctl is None:
                self._regs[addr] = value
                return ModbusResult.success([value], 12.0)
            log.info("mock control: %s=%d", ctl.name, value)
            if ctl.name == "remote_start":
                self._set_state("cranking")
            elif ctl.name == "remote_stop":
                self._cool_until = time.monotonic() + 6.0
                self._set_state("cooling")
            elif ctl.name == "exercise":
                self._exercise_until = time.monotonic() + 12.0
                self._set_state("exercising")
            elif ctl.name == "transfer":
                self._cool_until = time.monotonic() + 6.0
                self._set_state("cooling")
            return ModbusResult.success([value], 12.0)

    # ---- mock helpers (not part of the Protocol) ----
    def inject_alarm(self, code: int) -> None:
        self._inject_alarm = code

    def clear_alarm(self) -> None:
        self._alarm_active = 0
        if self._state == "alarm":
            self._set_state("stopped")
