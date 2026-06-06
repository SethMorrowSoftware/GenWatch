"""Modbus TCP server (pymodbus). Mounts a RegisterStore behind the
standard Modbus address space and serves reads/writes per the ICD.

The data block subclass routes ``getValues`` / ``setValues`` calls
into the RegisterStore, so the server is wholly stateless — all
state lives in the store.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

from .state import (
    ADDR_CMD_BYPASS_DELAY,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_TEST,
    RegisterStore,
)

log = logging.getLogger("atspi.server")

# Modbus function codes that write into the holding-register space.
_WRITE_FUNCTION_CODES = frozenset({6, 16})  # FC06 single, FC16 multiple

# The only addresses a client may write, per ICD §6.1. Everything else in
# the map is read-only state/identification or RESERVED, and a write to it
# MUST be rejected (ICD §3.2 / §6.1) rather than silently swallowed.
_WRITABLE_ADDRS = frozenset({
    ADDR_CMD_TEST,            # 0x0100
    ADDR_CMD_INHIBIT,         # 0x0101
    ADDR_CMD_FORCE_TRANSFER,  # 0x0102
    ADDR_CMD_BYPASS_DELAY,    # 0x0103
})


class AtsSlaveContext(ModbusSlaveContext):
    """Slave context that enforces the ICD §6 write map at the wire level.

    pymodbus 3.7 only lets a datastore reject a request through
    ``validate()``, which the protocol layer turns into Modbus exception
    **0x02** (ILLEGAL_DATA_ADDRESS). There is no datastore hook to emit
    0x03 (illegal data value) or 0x04 (server device failure) for a write
    — ``WriteSingleRegisterRequest.update_datastore`` only checks
    ``validate()`` and the 0..0xFFFF value bound, then commits. So every
    rejected write necessarily surfaces as 0x02.

    That is fine for the consumer: GenWatch treats *any* write exception
    as "command rejected" (see GenWatch ``services/ats_control.py`` — a
    failed ``client.write`` raises a 502 regardless of exception code), so
    the specific code does not change its behaviour.

    Without this override the default ``ModbusSlaveContext`` accepts every
    in-range write as success and relies on the data block to store it —
    which silently swallows writes to read-only and RESERVED addresses,
    the exact opposite of the ICD §3.2 / §6.1 contract.

    Only writes are constrained here. Reads are deferred to the base
    implementation so RESERVED reads still return 0 per ICD §5 instead of
    faulting.
    """

    def validate(self, fc_as_hex, address, count=1):  # noqa: N802 (pymodbus interface)
        if fc_as_hex in _WRITE_FUNCTION_CODES:
            # ``address`` is the on-the-wire PDU address (0-based); the
            # command registers live at 0x0100-0x0103. A FC16 write spans
            # [address, address+count), so every word must be writable.
            targets = range(address, address + count)
            if not all(a in _WRITABLE_ADDRS for a in targets):
                log.warning(
                    "rejecting write fc=%d addr=0x%04X count=%d — outside ICD §6 "
                    "command registers (Modbus exception 0x02)",
                    fc_as_hex, address, count,
                )
                return False
        return super().validate(fc_as_hex, address, count)


def _make_data_block(store: RegisterStore, on_read: Callable[[], None] | None):
    """Build a pymodbus data block that proxies all access to the
    RegisterStore. The on_read callback (if any) fires after every
    successful read — used by the safety watchdog to track liveness.
    """

    class LiveDataBlock(ModbusSequentialDataBlock):
        def getValues(self, address, count=1):  # noqa: N802 (pymodbus interface)
            # pymodbus passes 1-based addresses
            if on_read is not None:
                on_read()
            return [store.read_register(address - 1 + i) for i in range(count)]

        def setValues(self, address, values):  # noqa: N802
            # AtsSlaveContext.validate() has already rejected writes to
            # non-command addresses (→ Modbus 0x02). A write that reaches
            # here is to a valid command register, but the *value* may
            # still be undefined (e.g. 0x0007 to cmd_inhibit). pymodbus 3.7
            # gives us no way to fail the response at this point, so an
            # undefined value cannot raise 0x03; write_register declines it
            # (returns False) so no relay is driven, and we log it for
            # diagnostics rather than letting it pass unnoticed.
            for i, v in enumerate(values):
                addr = address - 1 + i
                if not store.write_register(addr, int(v)):
                    log.warning(
                        "ignoring write addr=0x%04X value=0x%04X — not a defined "
                        "command value (ICD §6.1); no output driven",
                        addr, int(v) & 0xFFFF,
                    )

    # Allocate enough address space for the ICD's register layout
    # (0x0000-0x010F + spare). Values are unused — overridden by
    # getValues/setValues.
    return LiveDataBlock(0, [0] * 0x0200)


async def start_server(
    host: str,
    port: int,
    unit_id: int,
    store: RegisterStore,
    on_read: Callable[[], None] | None = None,
) -> asyncio.Task:
    """Start the Modbus TCP server as a background task. Returns the
    task handle so the caller can cancel it during shutdown.
    """
    block = _make_data_block(store, on_read)
    # AtsSlaveContext (not the stock ModbusSlaveContext) so writes outside
    # the ICD §6 command registers are rejected with a Modbus exception
    # instead of being silently accepted.
    slave = AtsSlaveContext(hr=block, ir=block)
    context = ModbusServerContext(slaves={unit_id: slave}, single=False)

    async def _serve():
        try:
            await StartAsyncTcpServer(context=context, address=(host, port))
        except asyncio.CancelledError:
            log.info("Modbus server cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Modbus server crashed: %s", e)
            raise

    log.info("Modbus TCP server starting on %s:%d (unit_id=%d)", host, port, unit_id)
    task = asyncio.create_task(_serve(), name="modbus-server")
    # Give the server a moment to bind
    await asyncio.sleep(0.1)
    return task
