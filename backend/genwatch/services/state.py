"""Engine state machine + alarm tracker.

Translates raw register values into semantic state names and emits
transition events. Centralizes "what's the current state of the world"
so the API and WebSocket all read from one place.
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
    alarm_raw: int = 0
    last_reading: Reading = field(default_factory=Reading)
    comms: CommsHealth = field(default_factory=CommsHealth)

    @property
    def time_in_state_s(self) -> int:
        return int(time.time() - self.state_started_at)


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

        # Engine state
        raw_state = reading.get("engine_state")
        new_state = self.regmap.engine_state_for(raw_state) if raw_state is not None else self.snap.engine_state
        if new_state != self.snap.engine_state:
            old = self.snap.engine_state
            self.snap.engine_state = new_state
            self.snap.state_started_at = time.time()
            evt = {
                "type": "transition",
                "from": old,
                "to": new_state,
                "ts": time.time(),
            }
            emitted.append(evt)
            self.db.write_event(
                severity="ok",
                type_="TRANSITION",
                message=f"Engine state: {old} → {new_state}",
                meta=None,
            )
            log.info("Engine state transition: %s -> %s", old, new_state)

        # Alarms
        raw_alarm = int(reading.get("alarm_state") or 0)
        prev = self.snap.alarm_raw
        self.snap.alarm_raw = raw_alarm

        if raw_alarm != prev:
            if raw_alarm and not prev:
                ac = self.regmap.alarm_for(raw_alarm)
                if ac is not None:
                    raised = self.db.raise_alarm(ac.code, ac.desc, ac.severity, raw_alarm)
                    if raised:
                        self.db.write_event(
                            severity=ac.severity,
                            type_="ALARM",
                            message=f"Alarm raised — {ac.desc}",
                            meta=f"code {ac.code}",
                        )
                        emitted.append(
                            {
                                "type": "alarm",
                                "code": ac.code,
                                "desc": ac.desc,
                                "severity": ac.severity,
                                "ts": time.time(),
                            }
                        )
                        log.warning("Alarm raised: %s %s", ac.code, ac.desc)
                else:
                    # Unknown alarm code — still log it.
                    code = f"0x{raw_alarm:02x}"
                    self.db.raise_alarm(code, "Unknown alarm", "alarm", raw_alarm)
                    self.db.write_event("alarm", "ALARM", f"Unknown alarm code {code}", None)
                    emitted.append({"type": "alarm", "code": code, "desc": "Unknown alarm", "severity": "alarm", "ts": time.time()})
            elif prev and not raw_alarm:
                # alarm cleared
                ac = self.regmap.alarm_for(prev)
                if ac is not None:
                    cleared = self.db.clear_alarm(ac.code)
                    if cleared:
                        self.db.write_event(
                            severity="ok",
                            type_="ALARM",
                            message=f"Alarm cleared — {ac.desc}",
                            meta=f"code {ac.code}",
                        )
                        emitted.append({
                            "type": "alarm-cleared",
                            "code": ac.code,
                            "desc": ac.desc,
                            "ts": time.time(),
                        })
                        log.info("Alarm cleared: %s", ac.code)

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
