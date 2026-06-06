"""Write-validation tests for the Modbus server (ICD §6.1).

The ATS-Pi must reject any write outside the four command registers
(0x0100-0x0103) rather than silently accepting it. pymodbus 3.7 can only
reject a write through ``validate()`` — which the protocol turns into
Modbus exception 0x02 — so that is the code a rejected write carries.
GenWatch treats any write exception as "command rejected", so 0x02 vs the
ICD-preferred 0x03/0x04 makes no difference to the consumer.

Two layers of coverage:
  - ``AtsSlaveContext.validate`` directly (fast, deterministic) — locks in
    exactly which addresses are writable.
  - a real client→server round-trip — proves the wire actually carries an
    exception for a bad write and success for a valid one.
"""
from __future__ import annotations

import socket

import pytest

from atspi.server import AtsSlaveContext, _make_data_block, start_server
from atspi.state import (
    ADDR_CMD_BYPASS_DELAY,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_TEST,
    RegisterStore,
)

# FC codes
FC_READ_HOLDING = 3
FC_READ_INPUT = 4
FC_WRITE_SINGLE = 6
FC_WRITE_MULTIPLE = 16

# Modbus exception code for an out-of-range address.
EXC_ILLEGAL_ADDRESS = 0x02


def _context() -> AtsSlaveContext:
    store = RegisterStore(unit_id=23)
    block = _make_data_block(store, None)
    return AtsSlaveContext(hr=block, ir=block)


# ─── validate() logic ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "addr",
    [ADDR_CMD_TEST, ADDR_CMD_INHIBIT, ADDR_CMD_FORCE_TRANSFER, ADDR_CMD_BYPASS_DELAY],
)
def test_single_write_to_command_register_is_valid(addr):
    ctx = _context()
    assert ctx.validate(FC_WRITE_SINGLE, addr, 1)


@pytest.mark.parametrize(
    "addr",
    [
        0x0000,  # position — read-only core state
        0x0035,  # unit_id — read-only identification
        0x0040,  # cmd_test read-back — read-only mirror
        0x0104,  # first RESERVED command address
        0x010F,  # last RESERVED command address
        0x0050,  # RESERVED read space
    ],
)
def test_single_write_outside_command_registers_is_rejected(addr):
    ctx = _context()
    assert not ctx.validate(FC_WRITE_SINGLE, addr, 1)


def test_multi_write_spanning_into_reserved_is_rejected():
    ctx = _context()
    # 0x0100-0x0103 are writable but a 5-word write reaches 0x0104 (reserved).
    assert not ctx.validate(FC_WRITE_MULTIPLE, ADDR_CMD_TEST, 5)


def test_multi_write_within_command_block_is_valid():
    ctx = _context()
    # 0x0100, 0x0101, 0x0102 are all writable.
    assert ctx.validate(FC_WRITE_MULTIPLE, ADDR_CMD_TEST, 3)


@pytest.mark.parametrize("fc", [FC_READ_HOLDING, FC_READ_INPUT])
@pytest.mark.parametrize("addr", [0x0000, 0x0035, 0x0040, 0x0050])
def test_reads_are_never_constrained_by_write_validation(fc, addr):
    """Reads — including RESERVED addresses, which ICD §5 says return 0 —
    must not be blocked by the write-only address gate."""
    ctx = _context()
    assert ctx.validate(fc, addr, 1)


# ─── real wire round-trip ──────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_round_trip_rejects_bad_writes_accepts_valid_ones():
    from pymodbus.client import AsyncModbusTcpClient

    store = RegisterStore(unit_id=23)
    port = _free_port()
    task = await start_server(host="127.0.0.1", port=port, unit_id=1, store=store)
    try:
        client = AsyncModbusTcpClient(host="127.0.0.1", port=port)
        await client.connect()
        try:
            # Reads work, including the reserved-reads-as-0 contract.
            rr = await client.read_holding_registers(address=0x0000, count=6, slave=1)
            assert not rr.isError()
            rr = await client.read_holding_registers(address=0x0050, count=1, slave=1)
            assert not rr.isError() and rr.registers == [0]

            # A valid command write succeeds and is queued for dispatch.
            wr = await client.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
            assert not wr.isError()

            # Writes to reserved / read-only addresses are rejected with 0x02.
            for addr in (0x0104, 0x0000, ADDR_CMD_TEST + 0x40):  # reserved, read-only, read-back
                wr = await client.write_register(address=addr, value=1, slave=1)
                assert wr.isError(), f"write to 0x{addr:04X} should be rejected"
                assert wr.exception_code == EXC_ILLEGAL_ADDRESS

            # Only the one valid write should have been queued.
            assert store.drain_pending_commands() == [("inhibit", True)]
        finally:
            client.close()
    finally:
        task.cancel()
        try:
            await task
        except Exception:
            pass
