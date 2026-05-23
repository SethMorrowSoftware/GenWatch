"""Engine state machine + alarm tracker.

Translates raw register values into semantic state names and emits
transition events. Centralizes "what's the current state of the world"
so the API and WebSocket all read from one place.

The H-100 doesn't have a single integer "engine state" register.
State and alarms are derived from bitfield registers (output_status_1
through output_status_8) per the rules in registers/h100.yaml. The
RegisterMap exposes `derive_engine_state` and `derive_active_alarms`;
this module just diffs them across polls and emits events on change.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from ..modbus.poller import CommsHealth, Reading
from ..modbus.registers import RegisterMap

log = logging.getLogger("genwatch.state")


@dataclass
class StateSnapshot:
    engine_state: str = "unknown"
    state_started_at: float = field(default_factory=time.time)
    active_alarms: set[str] = field(default_factory=set)  # alarm codes
    last_reading: Reading = field(default_factory=Reading)
    comms: CommsHealth = field(default_factory=CommsHealth)

    @property
    def time_in_state_s(self) -> int:
        return int(time.time() - self.state_started_at)

    @property
    def alarm_raw(self) -> int:
        """Legacy field: non-zero iff any alarm is active."""
        return 1 if self.active_alarms else 0


class StateMachine:
    """Maintains the snapshot, raises/clears alarms, emits events."""

    def __init__(self, regmap: RegisterMap, db: Database, bus: "EventBus"):
        self.regmap = regmap
        self.db = db
        self.bus = bus
        self.snap = StateSnapshot()

    def update(self, reading: Reading, comms: CommsHealth) -> list[dict[str, Any]]:
        """Apply a new poll result. Returns the list of events emitted."""
        emitted: list[dict[str, Any]] = []

        # Engine state — derived from bitfield rules.
        new_state = self.regmap.derive_engine_state(reading.values)
        # Don't downgrade to 'unknown' if we already had a real state and
        # the prime registers just haven't been refreshed this tick.
        if new_state == "unknown" and self.snap.engine_state != "unknown":
            new_state = self.snap.engine_state
        if new_state != self.snap.engine_state:
            old = self.snap.engine_state
            self.snap.engine_state = new_state
            self.snap.state_started_at = time.time()
            emitted.append({
                "type": "transition",
                "from": old,
                "to": new_state,
                "ts": time.time(),
            })
            self.db.write_event(
                severity="ok",
                type_="TRANSITION",
                message=f"Engine state: {old} → {new_state}",
                meta=None,
            )
            log.info("Engine state transition: %s -> %s", old, new_state)

        # Alarms — diff the active set against last poll.
        active_now = self.regmap.derive_active_alarms(reading.values)
        active_now_codes = {ab.code for ab in active_now}
        prev_codes = self.snap.active_alarms

        # New alarms
        for ab in active_now:
            if ab.code in prev_codes:
                continue
            raised = self.db.raise_alarm(ab.code, ab.desc, ab.severity, ab.mask)
            if raised:
                self.db.write_event(
                    severity=ab.severity,
                    type_="ALARM",
                    message=f"Alarm raised — {ab.desc}",
                    meta=f"code {ab.code}",
                )
                emitted.append({
                    "type": "alarm",
                    "code": ab.code,
                    "desc": ab.desc,
                    "severity": ab.severity,
                    "ts": time.time(),
                })
                log.warning("Alarm raised: %s %s", ab.code, ab.desc)

        # Cleared alarms
        cleared_codes = prev_codes - active_now_codes
        for code in cleared_codes:
            ab = next((x for x in self.regmap.alarm_bits if x.code == code), None)
            desc = ab.desc if ab else code
            cleared = self.db.clear_alarm(code)
            if cleared:
                self.db.write_event(
                    severity="ok",
                    type_="ALARM",
                    message=f"Alarm cleared — {desc}",
                    meta=f"code {code}",
                )
                emitted.append({
                    "type": "alarm-cleared",
                    "code": code,
                    "desc": desc,
                    "ts": time.time(),
                })
                log.info("Alarm cleared: %s", code)

        self.snap.active_alarms = active_now_codes

        # Comms transition logging + event
        if comms.state != self.snap.comms.state:
            old_comms = self.snap.comms.state
            self.db.write_event(
                severity="warn" if comms.state != "healthy" else "ok",
                type_="COMMS",
                message=f"Comms {comms.state} · {comms.success_pct:.1f}% success",
                meta=None,
            )
            emitted.append({
                "type": "comms",
                "from": old_comms,
                "to": comms.state,
                "successPct": comms.success_pct,
                "ts": time.time(),
            })
        self.snap.comms = comms
        self.snap.last_reading = reading
        return emitted


class EventBus:
    """In-process publish/subscribe for WebSocket fan-out."""

    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, message: dict) -> None:
        dead: list[asyncio.Queue] = []
        for q in self._subs:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer — drop them rather than block the poller.
                dead.append(q)
        for q in dead:
            self._subs.discard(q)
