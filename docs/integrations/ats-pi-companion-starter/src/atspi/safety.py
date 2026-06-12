"""Comms-loss safety watchdog (ICD §8.3) + boot reset (ICD §9.3).

If the ATS-Pi hasn't received a successful Modbus read from GenWatch
within the timeout window, automatically release any maintained
commands (inhibit, force-transfer). This is the critical safety rule
that prevents an operator's stale "force transfer" from leaving the
ATS in a manual state forever.

The watchdog runs as its own asyncio task. The Modbus server's
LiveDataBlock hook calls ``note_modbus_read()`` on every successful
read; the watchdog wakes every second and checks elapsed time.

Two hard rules about the physical release:

  - A release that has been decided is carried to completion. The
    drive can fail (the ADAM may be unreachable at exactly the moment
    everything else is going wrong), so the watchdog retries every
    check tick until the I/O layer confirms the outputs are open.
    "We tried once and logged it" is not a release.
  - ``request_release()`` is also the ICD §9.3 boot path: __main__
    calls it at startup so relays left asserted by a previous process
    (crash with force-transfer on, crash mid-pulse) are driven open as
    soon as the I/O layer is reachable. The ADAM's relays are external
    hardware — they hold state across an atspi restart.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .io_driver import IODriver
from .state import RegisterStore

log = logging.getLogger("atspi.safety")

# ICD §8.3 — 30 ± 5 s.
TIMEOUT_S = 30.0
CHECK_INTERVAL_S = 1.0

# Bounded retry logging: full traceback on the first failed release
# attempt, then one warn line every N attempts (~N seconds) so a long
# ADAM outage doesn't flood the journal at 1 Hz.
_RETRY_LOG_EVERY = 30


class SafetyWatchdog:
    """Auto-release maintained commands on Modbus comms timeout."""

    def __init__(self, store: RegisterStore, driver: IODriver):
        self._store = store
        self._driver = driver
        self._last_read_monotonic: float = time.monotonic()
        # Whether the timeout has fired for the current silence interval
        # (one release decision per timeout event, not per check tick).
        self._timeout_fired: bool = False
        # A decided release that hasn't physically landed yet. Retried
        # every tick until the driver confirms; survives comms recovery
        # so the outcome is deterministic rather than racy.
        self._release_pending: bool = False
        self._release_reason: str = ""
        self._release_attempts: int = 0

    def note_modbus_read(self) -> None:
        """Called by the Modbus server's data block on every successful
        read. Cheap and frequent; do nothing expensive here.
        """
        self._last_read_monotonic = time.monotonic()
        if self._timeout_fired and not self._release_pending:
            # Comms recovered AND the release physically completed — re-arm.
            log.info("comms recovered; watchdog re-armed")
            self._timeout_fired = False

    def request_release(self, reason: str) -> None:
        """Queue a release of ALL command outputs, retried until it lands.

        Used by __main__ for the ICD §9.3 boot reset and internally for
        the §8.3 comms-loss auto-release. Clears the store's read-back
        immediately; the physical drive happens on the next check tick.
        """
        self._store.release_maintained_commands()
        self._release_pending = True
        self._release_reason = reason
        self._release_attempts = 0
        log.info("output release requested: %s", reason)

    async def run(self) -> None:
        log.info(
            "safety watchdog running (timeout=%.1fs, check every %.1fs)",
            TIMEOUT_S, CHECK_INTERVAL_S,
        )
        while True:
            if self._release_pending:
                await self._attempt_release()
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)
            except asyncio.CancelledError:
                return
            elapsed = time.monotonic() - self._last_read_monotonic
            if elapsed > TIMEOUT_S and not self._timeout_fired:
                log.warning(
                    "Modbus comms silent for %.1fs (> %.1fs) — auto-releasing "
                    "maintained commands per ICD §8.3",
                    elapsed, TIMEOUT_S,
                )
                self._timeout_fired = True
                self.request_release("comms-loss auto-release (ICD §8.3)")
                await self._attempt_release()

    async def _attempt_release(self) -> None:
        self._release_attempts += 1
        try:
            await self._driver.reset_outputs()
        except Exception as e:  # noqa: BLE001
            if self._release_attempts == 1:
                log.exception(
                    "output release (%s) failed — will retry every %.0fs "
                    "until it lands: %s",
                    self._release_reason, CHECK_INTERVAL_S, e,
                )
            elif self._release_attempts % _RETRY_LOG_EVERY == 0:
                log.warning(
                    "output release (%s) still failing after %d attempts: %s",
                    self._release_reason, self._release_attempts, e,
                )
            return
        log.info(
            "outputs released (%s) after %d attempt(s)",
            self._release_reason, self._release_attempts,
        )
        self._release_pending = False
