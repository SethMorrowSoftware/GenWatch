"""Safety watchdog (ICD §8.3) tests — the critical comms-loss auto-release.

Per SPEC §5 these use a REAL timer (short, monkeypatched timeout), not a
mocked clock, so the guarantee is exercised against wall time.
"""
from __future__ import annotations

import asyncio

import pytest

from atspi import safety
from atspi.io_mock import IOMockDriver
from atspi.state import RegisterStore

pytestmark = pytest.mark.asyncio

ADDR_CMD_INHIBIT_RB = 0x0041
ADDR_CMD_FORCE_RB = 0x0042


async def test_watchdog_releases_maintained_commands_on_timeout(monkeypatch):
    monkeypatch.setattr(safety, "TIMEOUT_S", 0.5)
    monkeypatch.setattr(safety, "CHECK_INTERVAL_S", 0.05)

    store = RegisterStore(unit_id=1)
    driver = IOMockDriver()
    await driver.connect()

    # Operator asserts inhibit + force-transfer; read-back confirms.
    await driver.drive_outputs(inhibit=True, force_transfer=True)
    store.apply_output_state(await driver.read_output_state())
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1
    assert store.read_register(ADDR_CMD_FORCE_RB) == 1

    wd = safety.SafetyWatchdog(store, driver)
    wd.note_modbus_read()  # comms alive at t0
    task = asyncio.create_task(wd.run())
    try:
        # Never note another read → comms goes silent → auto-release.
        await asyncio.sleep(0.8)  # > TIMEOUT_S
        out = await driver.read_output_state()
        assert out.inhibit_active is False, "inhibit must auto-release on comms loss"
        assert out.force_transfer_active is False, "force-transfer must auto-release"
        # The store read-back reflects the release immediately too.
        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
        assert store.read_register(ADDR_CMD_FORCE_RB) == 0
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_watchdog_does_not_release_while_comms_alive(monkeypatch):
    monkeypatch.setattr(safety, "TIMEOUT_S", 0.3)
    monkeypatch.setattr(safety, "CHECK_INTERVAL_S", 0.05)

    store = RegisterStore(unit_id=1)
    driver = IOMockDriver()
    await driver.connect()
    await driver.drive_outputs(inhibit=True)

    wd = safety.SafetyWatchdog(store, driver)
    task = asyncio.create_task(wd.run())
    try:
        # Keep comms alive past the timeout — must NOT release.
        for _ in range(10):
            wd.note_modbus_read()
            await asyncio.sleep(0.05)
        out = await driver.read_output_state()
        assert out.inhibit_active is True
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class _FlakyDriver(IOMockDriver):
    """Mock whose reset_outputs fails the first N attempts — models the
    ADAM being unreachable at exactly the moment a release is decided."""

    def __init__(self, fail_first_n: int):
        super().__init__()
        self.fail_remaining = fail_first_n
        self.reset_calls = 0

    async def reset_outputs(self) -> None:
        self.reset_calls += 1
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise OSError("ADAM unreachable")
        await super().reset_outputs()


async def test_release_retries_until_driver_recovers(monkeypatch):
    """A failed physical release MUST be retried until it lands — the
    old behavior latched 'released' on the first attempt even when the
    drive raised, leaving the relay closed with no retry."""
    monkeypatch.setattr(safety, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(safety, "CHECK_INTERVAL_S", 0.05)

    store = RegisterStore(unit_id=1)
    driver = _FlakyDriver(fail_first_n=3)
    await driver.connect()
    await driver.drive_outputs(force_transfer=True)

    wd = safety.SafetyWatchdog(store, driver)
    wd.note_modbus_read()
    task = asyncio.create_task(wd.run())
    try:
        # Comms go silent; timeout fires; first attempts fail; the
        # watchdog must keep retrying until the driver recovers.
        await asyncio.sleep(0.7)
        assert driver.reset_calls >= 4, "must retry past the failing attempts"
        out = await driver.read_output_state()
        assert out.force_transfer_active is False, (
            "force-transfer must physically release once the I/O recovers"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_release_pending_survives_comms_recovery(monkeypatch):
    """If comms recover while the physical release is still failing, the
    release is carried to completion anyway — deterministic outcome
    rather than a race between recovery and release."""
    monkeypatch.setattr(safety, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(safety, "CHECK_INTERVAL_S", 0.05)

    store = RegisterStore(unit_id=1)
    driver = _FlakyDriver(fail_first_n=5)
    await driver.connect()
    await driver.drive_outputs(inhibit=True)

    wd = safety.SafetyWatchdog(store, driver)
    wd.note_modbus_read()
    task = asyncio.create_task(wd.run())
    try:
        await asyncio.sleep(0.3)  # timeout fires; release pending + failing
        # Comms come back while the release hasn't landed.
        for _ in range(8):
            wd.note_modbus_read()
            await asyncio.sleep(0.05)
        out = await driver.read_output_state()
        assert out.inhibit_active is False, (
            "a decided release must complete even if comms recover mid-retry"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_boot_reset_releases_stale_outputs_with_comms_alive(monkeypatch):
    """ICD §9.3 — request_release at boot drives relays open that a
    previous process left asserted, even though comms are healthy (so
    the §8.3 timeout never fires)."""
    monkeypatch.setattr(safety, "TIMEOUT_S", 30.0)
    monkeypatch.setattr(safety, "CHECK_INTERVAL_S", 0.05)

    store = RegisterStore(unit_id=1)
    driver = IOMockDriver()
    await driver.connect()
    # Simulate hardware state inherited from a crashed previous process.
    await driver.drive_outputs(inhibit=True, force_transfer=True)

    wd = safety.SafetyWatchdog(store, driver)
    wd.request_release("boot reset (ICD §9.3)")
    task = asyncio.create_task(wd.run())
    try:
        for _ in range(4):
            wd.note_modbus_read()  # GenWatch polling normally
            await asyncio.sleep(0.05)
        out = await driver.read_output_state()
        assert out.inhibit_active is False
        assert out.force_transfer_active is False
        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
        assert store.read_register(ADDR_CMD_FORCE_RB) == 0
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
