"""Advantech ADAM-6060 driver — production I/O backend.

Implements the IODriver contract (see io_driver.py) against an
ADAM-6060 (6 digital inputs + 6 relay outputs) over Modbus TCP using
pymodbus's AsyncModbusTcpClient.

Channel mapping (docs/HARDWARE.md §3) — DO NOT change without re-checking
the field wiring:

  Inputs (DI):
    DI 0 → Load Disconnect contact  (momentary pulse during a transfer)
    DI 1 → Aux 14AA  "On Normal"     position
    DI 2 → Aux 14BA  "On Emergency"  position
    DI 3 → 18RX RL6  Normal source available
    DI 4 → 18RX RL5  Emergency source available
    DI 5 → Engine-start sense        (ATS asserting start to the H-100)

  Outputs (DO relays):
    DO 0 → Test (momentary, ≥500 ms pulse)          ICD cmd_test
    DO 1 → Maintained Transfer to Emergency          ICD cmd_force_transfer
    DO 2 → Inhibit Transfer                          ICD cmd_inhibit
    DO 3 → Bypass Transfer Time Delay (pulse)        ICD cmd_bypass_delay
    DO 4, DO 5 → spare — NEVER driven (terminals 14-16 are factory-use)

Modbus map (ADAM-6000 user manual; **verify against your firmware
revision** per docs/HARDWARE.md §6). The DI/DO states are exposed as
packed bitfields in holding registers; the relays are written per
channel as coils. All addresses are module constants so a firmware
quirk can be corrected in one place.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .io_driver import InputSnapshot, OutputState

log = logging.getLogger("atspi.io_adam")

# ── ADAM-6060 Modbus addresses (PDU offsets) ──────────────────────────────
ADDR_PACKED_DI = 0x0000   # FC03 holding register: bits 0..5 = DI 0..5
ADDR_PACKED_DO = 0x0001   # FC03 holding register: bits 0..5 = DO 0..5 (read-back)
ADDR_DO_COIL_BASE = 0x0010  # FC05 coil 0x0010+ch drives relay DO ch

# ── DI channel assignments (HARDWARE.md §3) ───────────────────────────────
DI_LOAD_DISCONNECT = 0
DI_ON_NORMAL = 1
DI_ON_EMERGENCY = 2
DI_NORMAL_AVAIL = 3
DI_EMERGENCY_AVAIL = 4
DI_ENGINE_START = 5

# ── DO channel assignments (HARDWARE.md §3) ───────────────────────────────
DO_TEST = 0
DO_FORCE_TRANSFER = 1
DO_INHIBIT = 2
DO_BYPASS_DELAY = 3

# ── Fault-summary bits (ICD §5.1.1) — kept local to avoid a driver→store
# import; must match state.py.
FAULT_INPUT = 0x0001
FAULT_OUTPUT = 0x0002
FAULT_MODE_UNKNOWN = 0x0004

# Pulse durations are clamped to the ICD §6.1 range on the wire.
PULSE_MIN_MS = 500
PULSE_MAX_MS = 1500

# How long both position aux contacts may read low before we call it a
# stuck-contact INPUT_FAULT rather than a (legitimate) transfer transient.
DEFAULT_TRANSFER_DEBOUNCE_S = 3.0

# Reuse one packed read across the sampling loop's back-to-back
# read_inputs()+read_output_state() calls (they fire ~instantly apart).
_PACKED_CACHE_TTL_S = 0.05


class IOAdamDriver:
    """ADAM-6060 driver over Modbus TCP.

    Position derivation, fault detection, pulse timing, and relay
    read-back checks all live here; the only thing that must be confirmed
    against the physical unit during commissioning is the Modbus address
    map above (docs/HARDWARE.md §6).
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        *,
        assume_auto_mode: bool = True,
        transfer_debounce_s: float = DEFAULT_TRANSFER_DEBOUNCE_S,
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        # The ASCO Group 5 wiring exposes no mode contact, so the ATS
        # mode can't be sensed. An unattended ATS is in AUTO; reporting
        # 'auto' avoids a permanent nuisance MODE_UNKNOWN alarm. Set
        # False to honour the ICD's "report unknown when undeterminable"
        # literally (raises ATS_PI_MODE_UNKNOWN in GenWatch).
        self._assume_auto_mode = assume_auto_mode
        self._transfer_debounce_s = transfer_debounce_s

        self._client = None
        # Commanded maintained-output state, for the OUTPUT_FAULT read-back
        # check (commanded high but reading low → stuck relay).
        self._cmd_inhibit = False
        self._cmd_force = False
        # Tracks how long both position aux contacts have read low.
        self._both_low_since: float | None = None
        # (monotonic_ts, di_packed, do_packed) short-lived read cache.
        self._packed_cache: tuple[float, int, int] | None = None
        # Pulsed-output self-release timers.
        self._test_release_task: asyncio.Task | None = None
        self._bypass_release_task: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def _new_client(self):
        from pymodbus.client import AsyncModbusTcpClient  # lazy import
        return AsyncModbusTcpClient(host=self.host, port=self.port)

    async def connect(self) -> bool:
        self._client = self._new_client()
        try:
            ok = bool(await self._client.connect())
        except Exception as e:  # noqa: BLE001
            log.error("ADAM-6060 connect to %s:%d errored: %s", self.host, self.port, e)
            return False
        if ok:
            log.info("ADAM-6060 connected at %s:%d (unit=%d)", self.host, self.port, self.unit_id)
        else:
            log.error("ADAM-6060 connect failed at %s:%d", self.host, self.port)
        return ok

    async def close(self) -> None:
        for t in (self._test_release_task, self._bypass_release_task):
            if t is not None:
                t.cancel()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _ensure_connected(self) -> None:
        if self._client is None:
            self._client = self._new_client()
        if getattr(self._client, "connected", False):
            return
        # Best-effort reconnect; raise if it doesn't take so the sampling
        # loop sets a fault bit (per the IODriver contract).
        ok = bool(await self._client.connect())
        if not ok:
            raise OSError(f"ADAM-6060 not connected ({self.host}:{self.port})")

    # ── reads ─────────────────────────────────────────────────────────────

    async def _read_packed(self) -> tuple[int, int]:
        """Return (di_packed, do_packed). Cached briefly so the sampling
        loop's read_inputs()+read_output_state() pair is one round-trip."""
        now = time.monotonic()
        cache = self._packed_cache
        if cache is not None and (now - cache[0]) < _PACKED_CACHE_TTL_S:
            return cache[1], cache[2]
        await self._ensure_connected()
        rr = await self._client.read_holding_registers(
            address=ADDR_PACKED_DI, count=2, slave=self.unit_id
        )
        if rr.isError() or rr.registers is None or len(rr.registers) < 2:
            raise OSError(f"ADAM-6060 read failed: {rr}")
        di, do = rr.registers[0] & 0xFFFF, rr.registers[1] & 0xFFFF
        self._packed_cache = (now, di, do)
        return di, do

    def _derive_position(self, di: int, now: float) -> tuple[str, int]:
        load_disc = bool(di & (1 << DI_LOAD_DISCONNECT))
        on_normal = bool(di & (1 << DI_ON_NORMAL))
        on_emerg = bool(di & (1 << DI_ON_EMERGENCY))

        if load_disc:
            self._both_low_since = None
            return "transferring", 0
        if on_normal and not on_emerg:
            self._both_low_since = None
            return "utility", 0
        if on_emerg and not on_normal:
            self._both_low_since = None
            return "generator", 0
        if on_normal and on_emerg:
            # Both aux contacts closed simultaneously is physically
            # impossible — a stuck/miswired contact.
            self._both_low_since = None
            return "unknown", FAULT_INPUT
        # Neither aux closed: a brief transient mid-transfer, or a stuck
        # contact if it persists. Debounce before calling it a fault.
        if self._both_low_since is None:
            self._both_low_since = now
        if (now - self._both_low_since) <= self._transfer_debounce_s:
            return "transferring", 0
        return "unknown", FAULT_INPUT

    def _output_fault(self, do: int) -> bool:
        for ch, commanded in ((DO_INHIBIT, self._cmd_inhibit),
                              (DO_FORCE_TRANSFER, self._cmd_force)):
            if commanded and not (do & (1 << ch)):
                return True
        return False

    async def read_inputs(self) -> InputSnapshot:
        di, do = await self._read_packed()
        now = time.monotonic()
        position, fault = self._derive_position(di, now)
        if self._output_fault(do):
            fault |= FAULT_OUTPUT
        if self._assume_auto_mode:
            mode = "auto"
        else:
            mode = "unknown"
            fault |= FAULT_MODE_UNKNOWN
        return InputSnapshot(
            position=position,
            normal_available=bool(di & (1 << DI_NORMAL_AVAIL)),
            emergency_available=bool(di & (1 << DI_EMERGENCY_AVAIL)),
            engine_start_calling=bool(di & (1 << DI_ENGINE_START)),
            ats_mode=mode,
            fault_bits=fault,
        )

    async def read_output_state(self) -> OutputState:
        _di, do = await self._read_packed()
        return OutputState(
            test_active=bool(do & (1 << DO_TEST)),
            force_transfer_active=bool(do & (1 << DO_FORCE_TRANSFER)),
            inhibit_active=bool(do & (1 << DO_INHIBIT)),
            bypass_delay_active=bool(do & (1 << DO_BYPASS_DELAY)),
        )

    # ── writes ────────────────────────────────────────────────────────────

    async def _write_do(self, ch: int, value: bool) -> None:
        await self._ensure_connected()
        rr = await self._client.write_coil(
            address=ADDR_DO_COIL_BASE + ch, value=bool(value), slave=self.unit_id
        )
        if rr.isError():
            raise OSError(f"ADAM-6060 write DO{ch}={value} failed: {rr}")
        # Invalidate the read cache so the next read reflects the new state.
        self._packed_cache = None

    async def drive_outputs(
        self,
        *,
        test_pulse_ms: int | None = None,
        inhibit: bool | None = None,
        force_transfer: bool | None = None,
        bypass_delay_pulse_ms: int | None = None,
    ) -> None:
        # Maintained outputs first (so a force-release lands promptly),
        # then the self-clearing pulses.
        if inhibit is not None:
            self._cmd_inhibit = bool(inhibit)
            await self._write_do(DO_INHIBIT, self._cmd_inhibit)
            log.info("ADAM: inhibit %s", "ASSERT" if self._cmd_inhibit else "RELEASE")
        if force_transfer is not None:
            self._cmd_force = bool(force_transfer)
            await self._write_do(DO_FORCE_TRANSFER, self._cmd_force)
            log.info("ADAM: force_transfer %s", "ASSERT" if self._cmd_force else "RELEASE")
        if test_pulse_ms is not None:
            await self._pulse(DO_TEST, "test", test_pulse_ms)
        if bypass_delay_pulse_ms is not None:
            await self._pulse(DO_BYPASS_DELAY, "bypass", bypass_delay_pulse_ms)

    async def _pulse(self, ch: int, which: str, duration_ms: int) -> None:
        ms = max(PULSE_MIN_MS, min(PULSE_MAX_MS, int(duration_ms)))
        await self._write_do(ch, True)
        attr = "_test_release_task" if which == "test" else "_bypass_release_task"
        old = getattr(self, attr)
        if old is not None:
            old.cancel()
        setattr(self, attr, asyncio.create_task(self._release(ch, which, ms)))
        log.info("ADAM: pulsing %s (DO%d) for %d ms", which, ch, ms)

    async def _release(self, ch: int, which: str, after_ms: int) -> None:
        try:
            await asyncio.sleep(after_ms / 1000.0)
            await self._write_do(ch, False)
            log.info("ADAM: pulsed %s (DO%d) released", which, ch)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("ADAM: pulse release for %s failed: %s", which, e)
