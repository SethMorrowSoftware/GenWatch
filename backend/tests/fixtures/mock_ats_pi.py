"""Mock ATS-Pi for GenWatch development and testing.

Implements the ICD v1.0 read side as a programmable in-memory store,
plus an optional real Modbus TCP server (pymodbus) for end-to-end
integration testing without real hardware.

Two modes of use:

1. **Programmatic, in-process** — for unit tests of AtsService:

       store = MockAtsPiStore()
       store.set_position("generator")
       store.set_normal_available(False)
       reading = store.as_reading(tier="prime")
       await ats_service.on_poll("prime", reading, healthy_comms)

   This bypasses the network — fast, deterministic, no port conflicts.

2. **Standalone TCP server** — for manual integration testing of the
   full GenWatch stack against a "real" ATS-Pi:

       python -m tests.fixtures.mock_ats_pi --port 5020

   Then point GenWatch at `127.0.0.1:5020` via the `ats.host` / `ats.port`
   config fields. Drive state changes via the interactive CLI prompt.

Both modes share the same underlying MockAtsPiStore, so behaviour is
identical at the register level.

The mock implements all read registers from the ICD; write registers
arrive in Phase 3 and are stubbed here (writes are accepted and
mirrored to the read-back registers, but no internal logic acts on
them yet).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Literal

from genwatch.modbus.poller import Reading

log = logging.getLogger("mock_ats_pi")


# ICD register addresses (PDU offsets, hex) — mirrored from
# docs/integrations/ats-pi-icd.md §5.
ADDR_POSITION = 0x0000
ADDR_NORMAL_AVAIL = 0x0001
ADDR_EMERGENCY_AVAIL = 0x0002
ADDR_ENGINE_START_CALLING = 0x0003
ADDR_ATS_MODE = 0x0004
ADDR_FAULT_SUMMARY = 0x0005

ADDR_LAST_TRANSFER_TS = 0x0010    # u32 (2 words)
ADDR_LAST_RETRANSFER_TS = 0x0012  # u32
ADDR_UPTIME_S = 0x0014            # u32
ADDR_WALLCLOCK = 0x0016           # u32

ADDR_TRANSFER_COUNT_LIFETIME = 0x0020  # u32
ADDR_TRANSFER_COUNT_24H = 0x0022       # u32

ADDR_ICD_MAJOR = 0x0030
ADDR_ICD_MINOR = 0x0031
ADDR_FW_MAJOR = 0x0032
ADDR_FW_MINOR = 0x0033
ADDR_FW_PATCH = 0x0034
ADDR_UNIT_ID = 0x0035

ADDR_CMD_TEST_RB = 0x0040
ADDR_CMD_INHIBIT_RB = 0x0041
ADDR_CMD_FORCE_TRANSFER_RB = 0x0042
ADDR_CMD_BYPASS_DELAY_RB = 0x0043

ADDR_CMD_TEST = 0x0100
ADDR_CMD_INHIBIT = 0x0101
ADDR_CMD_FORCE_TRANSFER = 0x0102
ADDR_CMD_BYPASS_DELAY = 0x0103


# Position / mode enum values (ICD §5.1)
POS_UTILITY = 0
POS_GENERATOR = 1
POS_TRANSFERRING = 2
POS_UNKNOWN = 3

MODE_AUTO = 0
MODE_MANUAL = 1
MODE_TEST = 2
MODE_UNKNOWN = 3


_POSITION_TO_VALUE = {
    "utility": POS_UTILITY,
    "generator": POS_GENERATOR,
    "transferring": POS_TRANSFERRING,
    "unknown": POS_UNKNOWN,
}
_MODE_TO_VALUE = {
    "auto": MODE_AUTO,
    "manual": MODE_MANUAL,
    "test": MODE_TEST,
    "unknown": MODE_UNKNOWN,
}


@dataclass
class MockAtsPiStore:
    """Programmable in-memory state of a mock ATS-Pi.

    Use as a building block for unit tests (call `as_reading(tier)` to
    get a synthesized poll result) or wrap in `MockAtsPiServer` to
    expose over real Modbus TCP for integration tests.

    All state is independently mutable — tests drive transitions by
    calling the setter methods and re-polling.

    Defaults represent a healthy steady-state: load on utility, both
    sources available, AUTO mode, no faults.
    """

    # Core state
    position: str = "utility"
    normal_available: bool = True
    emergency_available: bool = True
    engine_start_calling: bool = False
    ats_mode: str = "auto"
    fault_bits: int = 0

    # Counters and timestamps
    last_transfer_to_gen_ts: int = 0
    last_retransfer_to_util_ts: int = 0
    transfer_count_lifetime: int = 0
    transfer_count_24h: int = 0

    # Identification (ICD v1.0, mock firmware 0.1.0, site SITE-23)
    icd_major: int = 1
    icd_minor: int = 0
    fw_major: int = 0
    fw_minor: int = 1
    fw_patch: int = 0
    unit_id: int = 23

    # Command read-back state (set by the Phase 3 command handlers in
    # the future; for now, writes update these directly)
    cmd_test_active: bool = False
    cmd_inhibit_active: bool = False
    cmd_force_transfer_active: bool = False
    cmd_bypass_delay_active: bool = False

    # Wall-clock offset (s) — tests can set this to a non-zero value to
    # simulate clock skew between the mock and GenWatch.
    wallclock_offset_s: float = 0.0

    # Boot time, used to compute uptime
    _boot_ts: float = field(default_factory=time.time)

    # ── Setters that emulate physical events ──────────────────────────

    def set_position(self, position: str) -> None:
        if position not in _POSITION_TO_VALUE:
            raise ValueError(f"unknown position: {position!r}")
        if position == "generator" and self.position != "generator":
            self.last_transfer_to_gen_ts = int(time.time())
            self.transfer_count_lifetime += 1
            self.transfer_count_24h += 1
        elif position == "utility" and self.position == "generator":
            self.last_retransfer_to_util_ts = int(time.time())
        self.position = position

    def set_normal_available(self, available: bool) -> None:
        self.normal_available = available
        # Simulate the ATS asserting engine-start when utility goes away
        if not available and self.engine_start_calling is False:
            self.engine_start_calling = True
        elif available:
            # ATS deasserts after a delay in real life; mock doesn't
            # simulate the delay
            self.engine_start_calling = False

    def set_emergency_available(self, available: bool) -> None:
        self.emergency_available = available

    def set_mode(self, mode: str) -> None:
        if mode not in _MODE_TO_VALUE:
            raise ValueError(f"unknown mode: {mode!r}")
        self.ats_mode = mode

    def set_fault_bit(self, mask: int, on: bool = True) -> None:
        if on:
            self.fault_bits |= mask
        else:
            self.fault_bits &= ~mask

    def reboot(self) -> None:
        """Simulate an ATS-Pi reboot — wipe in-memory state, preserve
        only the persistent counter per ICD §9.3.
        """
        preserved_count = self.transfer_count_lifetime
        for k, v in MockAtsPiStore().__dict__.items():
            if k.startswith("_"):
                continue
            setattr(self, k, v)
        self.transfer_count_lifetime = preserved_count
        self._boot_ts = time.time()

    # ── Computed values ───────────────────────────────────────────────

    @property
    def uptime_s(self) -> int:
        return max(0, int(time.time() - self._boot_ts))

    @property
    def wallclock(self) -> int:
        return int(time.time() + self.wallclock_offset_s)

    # ── Reading synthesis ─────────────────────────────────────────────

    def read_register(self, addr: int) -> int:
        """Return the 16-bit value at the given PDU address. Used by
        the Modbus TCP server. Multi-word values (u32) are split per
        ICD §3.1: high word at lower address, big-endian.
        """
        if addr == ADDR_POSITION:
            return _POSITION_TO_VALUE[self.position]
        if addr == ADDR_NORMAL_AVAIL:
            return int(self.normal_available)
        if addr == ADDR_EMERGENCY_AVAIL:
            return int(self.emergency_available)
        if addr == ADDR_ENGINE_START_CALLING:
            return int(self.engine_start_calling)
        if addr == ADDR_ATS_MODE:
            return _MODE_TO_VALUE[self.ats_mode]
        if addr == ADDR_FAULT_SUMMARY:
            return self.fault_bits & 0xFFFF

        # u32 timestamps and counters — big-endian word order
        if addr == ADDR_LAST_TRANSFER_TS:
            return (self.last_transfer_to_gen_ts >> 16) & 0xFFFF
        if addr == ADDR_LAST_TRANSFER_TS + 1:
            return self.last_transfer_to_gen_ts & 0xFFFF
        if addr == ADDR_LAST_RETRANSFER_TS:
            return (self.last_retransfer_to_util_ts >> 16) & 0xFFFF
        if addr == ADDR_LAST_RETRANSFER_TS + 1:
            return self.last_retransfer_to_util_ts & 0xFFFF
        if addr == ADDR_UPTIME_S:
            return (self.uptime_s >> 16) & 0xFFFF
        if addr == ADDR_UPTIME_S + 1:
            return self.uptime_s & 0xFFFF
        if addr == ADDR_WALLCLOCK:
            return (self.wallclock >> 16) & 0xFFFF
        if addr == ADDR_WALLCLOCK + 1:
            return self.wallclock & 0xFFFF
        if addr == ADDR_TRANSFER_COUNT_LIFETIME:
            return (self.transfer_count_lifetime >> 16) & 0xFFFF
        if addr == ADDR_TRANSFER_COUNT_LIFETIME + 1:
            return self.transfer_count_lifetime & 0xFFFF
        if addr == ADDR_TRANSFER_COUNT_24H:
            return (self.transfer_count_24h >> 16) & 0xFFFF
        if addr == ADDR_TRANSFER_COUNT_24H + 1:
            return self.transfer_count_24h & 0xFFFF

        # Identification (u16 each)
        if addr == ADDR_ICD_MAJOR:
            return self.icd_major
        if addr == ADDR_ICD_MINOR:
            return self.icd_minor
        if addr == ADDR_FW_MAJOR:
            return self.fw_major
        if addr == ADDR_FW_MINOR:
            return self.fw_minor
        if addr == ADDR_FW_PATCH:
            return self.fw_patch
        if addr == ADDR_UNIT_ID:
            return self.unit_id

        # Command read-back
        if addr == ADDR_CMD_TEST_RB:
            return int(self.cmd_test_active)
        if addr == ADDR_CMD_INHIBIT_RB:
            return int(self.cmd_inhibit_active)
        if addr == ADDR_CMD_FORCE_TRANSFER_RB:
            return int(self.cmd_force_transfer_active)
        if addr == ADDR_CMD_BYPASS_DELAY_RB:
            return int(self.cmd_bypass_delay_active)

        # RESERVED / unknown — return 0 per ICD §5 ("MUST return 0x0000")
        return 0

    def write_register(self, addr: int, value: int) -> bool:
        """Process a write per ICD §6. Returns True if accepted.

        Phase 1 stub — Phase 3 will add the actual pulse-timing and
        safety-auto-release logic. For now, we mirror the write into
        the read-back slot so GenWatch's tests can verify the round-trip.
        """
        if addr == ADDR_CMD_TEST and value == 0x0001:
            self.cmd_test_active = True
            return True
        if addr == ADDR_CMD_INHIBIT and value in (0x0000, 0x0001):
            self.cmd_inhibit_active = value == 0x0001
            return True
        if addr == ADDR_CMD_FORCE_TRANSFER and value in (0x0000, 0x0001):
            self.cmd_force_transfer_active = value == 0x0001
            return True
        if addr == ADDR_CMD_BYPASS_DELAY and value == 0x0001:
            self.cmd_bypass_delay_active = True
            return True
        return False

    def as_reading(self, tier: Literal["prime", "base"] = "prime") -> Reading:
        """Synthesize a Reading dict equivalent to what GenWatch's poller
        would produce after a tier-tier poll. Used by unit tests to
        drive AtsService directly without going through Modbus.
        """
        values: dict[str, float | int] = {}
        # Prime tier: core state + command read-back
        if tier in ("prime", "base"):
            values["position"] = _POSITION_TO_VALUE[self.position]
            values["normal_available"] = int(self.normal_available)
            values["emergency_available"] = int(self.emergency_available)
            values["engine_start_calling"] = int(self.engine_start_calling)
            values["ats_mode"] = _MODE_TO_VALUE[self.ats_mode]
            values["fault_summary"] = self.fault_bits
            values["cmd_test_active"] = int(self.cmd_test_active)
            values["cmd_inhibit_active"] = int(self.cmd_inhibit_active)
            values["cmd_force_transfer_active"] = int(self.cmd_force_transfer_active)
            values["cmd_bypass_delay_active"] = int(self.cmd_bypass_delay_active)
        # Base tier additions: timestamps, counters, identification
        if tier == "base":
            values["last_transfer_to_gen_ts"] = self.last_transfer_to_gen_ts
            values["last_retransfer_to_util_ts"] = self.last_retransfer_to_util_ts
            values["ats_pi_uptime_s"] = self.uptime_s
            values["ats_pi_wallclock"] = self.wallclock
            values["transfer_count_lifetime"] = self.transfer_count_lifetime
            values["transfer_count_24h"] = self.transfer_count_24h
            values["icd_version_major"] = self.icd_major
            values["icd_version_minor"] = self.icd_minor
            values["ats_pi_fw_major"] = self.fw_major
            values["ats_pi_fw_minor"] = self.fw_minor
            values["ats_pi_fw_patch"] = self.fw_patch
            values["ats_pi_unit_id"] = self.unit_id
        return Reading(values=values, ts=time.time())


# ─── Modbus TCP server wrapper (for integration testing) ─────────────────

# Imported lazily so unit tests don't pay the pymodbus import cost.


class MockAtsPiServer:
    """Runs MockAtsPiStore behind a real pymodbus TCP server, on the
    address/port given. Used for integration tests and manual end-to-
    end testing.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5020):
        self.host = host
        self.port = port
        self.store = MockAtsPiStore()
        self._task: asyncio.Task | None = None
        self._server = None

    async def start(self) -> None:
        from pymodbus.datastore import (
            ModbusSequentialDataBlock,
            ModbusServerContext,
            ModbusSlaveContext,
        )
        from pymodbus.server import StartAsyncTcpServer

        # Pre-populate the data block with the current store state.
        # We use a custom DataBlock subclass that consults the store
        # on every read, so updates to store state are reflected
        # immediately without re-initialization.
        store = self.store

        class _LiveDataBlock(ModbusSequentialDataBlock):
            def getValues(self, address, count=1):  # pymodbus signature  # noqa: N802
                # pymodbus addresses are 1-based for getValues; convert
                return [store.read_register(address - 1 + i) for i in range(count)]

            def setValues(self, address, values):  # noqa: N802
                # Write path — mirror into the store
                for i, v in enumerate(values):
                    store.write_register(address - 1 + i, int(v))

        # Pre-allocate the full 0x0000-0x010F range with zeros; the
        # _LiveDataBlock overrides getValues/setValues so the pre-alloc
        # values are irrelevant — they just need to exist for pymodbus
        # to accept the address range.
        block = _LiveDataBlock(0, [0] * 0x0200)
        slave = ModbusSlaveContext(hr=block, ir=block)
        context = ModbusServerContext(slaves={1: slave}, single=False)

        self._task = asyncio.create_task(
            StartAsyncTcpServer(
                context=context,
                address=(self.host, self.port),
            )
        )
        # Give the server a moment to bind
        await asyncio.sleep(0.1)
        log.info("Mock ATS-Pi server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


# ─── CLI ─────────────────────────────────────────────────────────────────


async def _interactive_cli(server: MockAtsPiServer) -> None:
    """Simple stdin-driven CLI for manual integration testing.

    Commands:
        position utility | generator | transferring
        normal on|off
        emergency on|off
        mode auto|manual|test
        fault <hex_mask> on|off
        reboot
        skew <seconds>
        help
        quit
    """
    print("Mock ATS-Pi interactive CLI. Type 'help' for commands.")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            await asyncio.sleep(0.1)
            continue
        cmd, *args = line.strip().split()
        if not cmd:
            continue
        store = server.store
        try:
            if cmd == "help":
                print(_interactive_cli.__doc__)
            elif cmd == "position" and args:
                store.set_position(args[0])
            elif cmd == "normal" and args:
                store.set_normal_available(args[0] in ("on", "true", "1"))
            elif cmd == "emergency" and args:
                store.set_emergency_available(args[0] in ("on", "true", "1"))
            elif cmd == "mode" and args:
                store.set_mode(args[0])
            elif cmd == "fault" and len(args) >= 2:
                mask = int(args[0], 0)
                on = args[1] in ("on", "true", "1")
                store.set_fault_bit(mask, on)
            elif cmd == "reboot":
                store.reboot()
            elif cmd == "skew" and args:
                store.wallclock_offset_s = float(args[0])
            elif cmd in ("quit", "exit"):
                break
            else:
                print(f"unknown command: {cmd!r}. Type 'help'.")
                continue
            print(
                f"  → pos={store.position} norm={store.normal_available} "
                f"emer={store.emergency_available} engstart={store.engine_start_calling} "
                f"mode={store.ats_mode} fault=0x{store.fault_bits:04x} "
                f"uptime={store.uptime_s}s"
            )
        except Exception as e:  # noqa: BLE001
            print(f"error: {e}")


async def _amain() -> None:
    ap = argparse.ArgumentParser(description="Mock ATS-Pi Modbus TCP server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument(
        "--no-cli", action="store_true",
        help="Run quietly without the interactive prompt (for CI)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = MockAtsPiServer(host=args.host, port=args.port)
    await server.start()
    try:
        if args.no_cli:
            while True:
                await asyncio.sleep(3600)
        else:
            await _interactive_cli(server)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
