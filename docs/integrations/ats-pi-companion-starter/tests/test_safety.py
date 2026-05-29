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
