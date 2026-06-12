"""IOAdamDriver logic tests.

Drives the ADAM-6060 driver against a fake pymodbus client that models
the module's packed-register reads and per-coil writes. The Modbus
address map is a commissioning detail (verified on real hardware); what
we test here is the logic that matters: DI-bit decode, position
derivation + fault detection, relay drive, pulse self-clear, and the
output read-back (stuck-relay) check.
"""
from __future__ import annotations

import asyncio

import pytest

from atspi import io_adam
from atspi.io_adam import (
    DI_EMERGENCY_AVAIL,
    DI_ENGINE_START,
    DI_LOAD_DISCONNECT,
    DI_NORMAL_AVAIL,
    DI_ON_EMERGENCY,
    DI_ON_NORMAL,
    DO_FORCE_TRANSFER,
    DO_INHIBIT,
    DO_TEST,
    FAULT_INPUT,
    FAULT_OUTPUT,
    IOAdamDriver,
)

pytestmark = pytest.mark.asyncio


class _FakeRR:
    def __init__(self, registers=None, error=False):
        self.registers = registers
        self._err = error

    def isError(self):
        return self._err


class FakeAdam:
    """Models the ADAM-6060: packed DI/DO holding regs + per-coil DO writes."""

    def __init__(self, di: int = 0, do: int = 0):
        self.connected = True
        self.di = di
        self.do = do
        self.coil_writes: list[tuple[int, bool]] = []

    async def connect(self):
        self.connected = True
        return True

    def close(self):
        self.connected = False

    async def read_holding_registers(self, address, count, slave):
        if address == io_adam.ADDR_PACKED_DI and count == 2:
            return _FakeRR([self.di & 0xFFFF, self.do & 0xFFFF])
        return _FakeRR(error=True)

    async def write_coil(self, address, value, slave):
        ch = address - io_adam.ADDR_DO_COIL_BASE
        self.coil_writes.append((ch, bool(value)))
        if value:
            self.do |= (1 << ch)
        else:
            self.do &= ~(1 << ch)
        return _FakeRR()


def _bits(*channels: int) -> int:
    v = 0
    for c in channels:
        v |= (1 << c)
    return v


def _driver(fake: FakeAdam, **kw) -> IOAdamDriver:
    d = IOAdamDriver(host="x", port=502, unit_id=1, **kw)
    d._client = fake
    return d


# ── position + availability decode ───────────────────────────────────────


async def test_position_utility():
    fake = FakeAdam(di=_bits(DI_ON_NORMAL, DI_NORMAL_AVAIL, DI_EMERGENCY_AVAIL))
    d = _driver(fake)
    snap = await d.read_inputs()
    assert snap.position == "utility"
    assert snap.normal_available is True
    assert snap.emergency_available is True
    assert snap.engine_start_calling is False
    assert snap.fault_bits == 0


async def test_position_generator_with_engine_start():
    fake = FakeAdam(di=_bits(DI_ON_EMERGENCY, DI_EMERGENCY_AVAIL, DI_ENGINE_START))
    d = _driver(fake)
    snap = await d.read_inputs()
    assert snap.position == "generator"
    assert snap.normal_available is False
    assert snap.engine_start_calling is True
    assert snap.fault_bits == 0


async def test_position_transferring_on_load_disconnect():
    fake = FakeAdam(di=_bits(DI_LOAD_DISCONNECT, DI_NORMAL_AVAIL, DI_EMERGENCY_AVAIL))
    d = _driver(fake)
    snap = await d.read_inputs()
    assert snap.position == "transferring"
    assert snap.fault_bits == 0


async def test_both_aux_high_is_input_fault():
    fake = FakeAdam(di=_bits(DI_ON_NORMAL, DI_ON_EMERGENCY))
    d = _driver(fake)
    snap = await d.read_inputs()
    assert snap.position == "unknown"
    assert snap.fault_bits & FAULT_INPUT


async def test_both_aux_low_transient_then_fault():
    # No aux contact closed, no load-disconnect: a transfer transient at
    # first, a stuck-contact fault if it persists past the debounce.
    fake = FakeAdam(di=_bits(DI_NORMAL_AVAIL, DI_EMERGENCY_AVAIL))
    d = _driver(fake, transfer_debounce_s=0.0)
    snap1 = await d.read_inputs()
    assert snap1.position == "transferring"  # within the (zero) debounce window
    assert not (snap1.fault_bits & FAULT_INPUT)
    await asyncio.sleep(0.02)
    snap2 = await d.read_inputs()
    assert snap2.position == "unknown"
    assert snap2.fault_bits & FAULT_INPUT


async def test_both_aux_low_stays_transferring_within_debounce():
    fake = FakeAdam(di=_bits(DI_NORMAL_AVAIL, DI_EMERGENCY_AVAIL))
    d = _driver(fake, transfer_debounce_s=5.0)
    snap = await d.read_inputs()
    assert snap.position == "transferring"
    assert not (snap.fault_bits & FAULT_INPUT)


# ── relay drive + read-back ───────────────────────────────────────────────


async def test_drive_inhibit_and_force_write_correct_coils():
    fake = FakeAdam(di=_bits(DI_ON_NORMAL))
    d = _driver(fake)
    await d.drive_outputs(inhibit=True)
    await d.drive_outputs(force_transfer=True)
    assert (DO_INHIBIT, True) in fake.coil_writes
    assert (DO_FORCE_TRANSFER, True) in fake.coil_writes
    out = await d.read_output_state()
    assert out.inhibit_active is True
    assert out.force_transfer_active is True
    # Release
    await d.drive_outputs(force_transfer=False)
    assert (DO_FORCE_TRANSFER, False) in fake.coil_writes
    out = await d.read_output_state()
    assert out.force_transfer_active is False


async def test_test_pulse_self_clears():
    fake = FakeAdam(di=_bits(DI_ON_NORMAL))
    d = _driver(fake)
    await d.drive_outputs(test_pulse_ms=500)
    out = await d.read_output_state()
    assert out.test_active is True
    # Pulse is clamped to ≥500 ms; wait it out and confirm self-release.
    await asyncio.sleep(0.6)
    out = await d.read_output_state()
    assert out.test_active is False
    assert (DO_TEST, True) in fake.coil_writes
    assert (DO_TEST, False) in fake.coil_writes


async def test_output_fault_on_stuck_relay():
    fake = FakeAdam(di=_bits(DI_ON_NORMAL))
    d = _driver(fake)
    await d.drive_outputs(inhibit=True)
    # Simulate the relay failing to actuate: read-back bit drops despite
    # the command staying asserted.
    fake.do &= ~(1 << DO_INHIBIT)
    snap = await d.read_inputs()
    assert snap.fault_bits & FAULT_OUTPUT


async def test_reset_outputs_opens_all_command_relays():
    """Boot/auto-release reset: every command relay driven open, even
    state inherited from a previous process (fresh driver instance with
    no commanded flags set, but the hardware DO bits still high)."""
    fake = FakeAdam(
        di=_bits(DI_ON_NORMAL),
        do=_bits(DO_TEST, DO_FORCE_TRANSFER, DO_INHIBIT, io_adam.DO_BYPASS_DELAY),
    )
    d = _driver(fake)
    out = await d.read_output_state()
    assert out.force_transfer_active is True  # stale hardware state visible
    await d.reset_outputs()
    for ch in (DO_TEST, DO_FORCE_TRANSFER, DO_INHIBIT, io_adam.DO_BYPASS_DELAY):
        assert (ch, False) in fake.coil_writes
    out = await d.read_output_state()
    assert out.test_active is False
    assert out.force_transfer_active is False
    assert out.inhibit_active is False
    assert out.bypass_delay_active is False


async def test_reset_outputs_cancels_inflight_pulse():
    """A reset mid-pulse cancels the release timer — the relay is opened
    by the reset itself and the dead timer must not fire a late write."""
    fake = FakeAdam(di=_bits(DI_ON_NORMAL))
    d = _driver(fake)
    await d.drive_outputs(test_pulse_ms=500)
    assert (await d.read_output_state()).test_active is True
    await d.reset_outputs()
    writes_after_reset = len(fake.coil_writes)
    await asyncio.sleep(0.6)  # past the pulse duration
    assert len(fake.coil_writes) == writes_after_reset, (
        "cancelled pulse timer must not issue a late coil write"
    )
    assert (await d.read_output_state()).test_active is False


async def test_reset_outputs_raises_on_write_failure_for_retry():
    """A failed coil write must surface to the caller (the safety
    watchdog retries the whole reset) — never be swallowed as success."""
    fake = FakeAdam(di=_bits(DI_ON_NORMAL), do=_bits(DO_INHIBIT))

    async def _failing_write_coil(address, value, slave):
        return _FakeRR(error=True)

    fake.write_coil = _failing_write_coil
    d = _driver(fake)
    with pytest.raises(OSError):
        await d.reset_outputs()


async def test_assume_auto_mode_default_and_unknown_option():
    fake = FakeAdam(di=_bits(DI_ON_NORMAL))
    assert (await _driver(fake).read_inputs()).ats_mode == "auto"
    snap = await _driver(fake, assume_auto_mode=False).read_inputs()
    assert snap.ats_mode == "unknown"
    assert snap.fault_bits & io_adam.FAULT_MODE_UNKNOWN
