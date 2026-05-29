"""Write-path (Phase C) tests: a GenWatch command write drives the relays
via the I/O driver, and the read-back registers reflect the *driven*
state (ICD §5.5), not the written value."""
from __future__ import annotations

import asyncio

import pytest

from atspi.__main__ import _command_loop
from atspi.io_mock import IOMockDriver
from atspi.state import (
    ADDR_CMD_INHIBIT,
    ADDR_CMD_INHIBIT_RB,
    ADDR_CMD_TEST,
    ADDR_CMD_TEST_RB,
    RegisterStore,
)

pytestmark = pytest.mark.asyncio


async def test_write_enqueues_without_setting_readback():
    store = RegisterStore(unit_id=1)
    # A recognized command write is accepted and queued…
    assert store.write_register(ADDR_CMD_INHIBIT, 1) is True
    # …but the read-back is NOT set from the written value — it reflects
    # the actual driven state, which only updates after the driver runs.
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
    assert store.drain_pending_commands() == [("inhibit", True)]
    # Draining is one-shot.
    assert store.drain_pending_commands() == []


async def test_unrecognized_write_returns_false():
    store = RegisterStore(unit_id=1)
    assert store.write_register(0x00FF, 1) is False
    assert store.write_register(ADDR_CMD_INHIBIT, 7) is False  # bad value


async def test_command_round_trip_drives_and_reads_back():
    store = RegisterStore(unit_id=1)
    driver = IOMockDriver()
    await driver.connect()
    # GenWatch asserts inhibit; dispatch drives the relay; the sampling
    # loop reads the driven state back into the store.
    store.write_register(ADDR_CMD_INHIBIT, 1)
    for name, arg in store.drain_pending_commands():
        if name == "inhibit":
            await driver.drive_outputs(inhibit=bool(arg))
    store.apply_output_state(await driver.read_output_state())
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1


async def test_command_loop_dispatches_inhibit_and_test():
    store = RegisterStore(unit_id=1)
    driver = IOMockDriver()
    await driver.connect()
    task = asyncio.create_task(_command_loop(driver, store))
    try:
        store.write_register(ADDR_CMD_INHIBIT, 1)
        store.write_register(ADDR_CMD_TEST, 1)
        await asyncio.sleep(0.2)  # let the 50 ms dispatch loop run
        out = await driver.read_output_state()
        assert out.inhibit_active is True
        assert out.test_active is True  # pulse still high (≥500 ms)
        # read-back via the store after a sampling cycle
        store.apply_output_state(out)
        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1
        assert store.read_register(ADDR_CMD_TEST_RB) == 1
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await driver.close()
