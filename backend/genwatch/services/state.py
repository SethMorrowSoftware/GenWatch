"""Engine state machine + alarm tracker.

Translates raw register values into semantic state names and emits
transition events. Centralizes "what's the current state of the world"
so the API and WebSocket all read from one place.

The H-100 doesn't have a single integer "engine state" register.
State and alarms are derived from bitfield registers (output_status_1
through output_status_8) per the rules in registers/h100.yaml. The
RegisterMap exposes `derive_engine_state` and `derive_active_alarms`;
this module just diffs them across polls and emits events on change.

LOAD SOURCE DERIVATION (utility vs generator)
---------------------------------------------
The H-100 is the *generator's* controller, not the ATS, so it has no
direct "switch position" register. We infer the load source from
engine state + generator output (kW and current). See
`_derive_load_source` for the rules. This gives us a reliable
"ON UTILITY / ON GENERATOR" indicator without any new hardware or
wiring into the ATS.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from typing import TYPE_CHECKING

from ..db import Database
from ..modbus.poller import CommsHealth, Reading
from ..modbus.registers import RegisterMap

if TYPE_CHECKING:
    from .ats import AtsService

log = logging.getLogger("genwatch.state")


# ─── Load-source detection thresholds ─────────────────────────────────────
# We declare "load on generator" only when both real power AND output
# current cross above their ON thresholds, and only declare "load
# returned to utility" when both drop below their OFF thresholds.
# Asymmetric thresholds give hysteresis so a load right at the boundary
# can't cause flicker; AND across both sensors guards against a single
# CT or kW-measurement fault biasing the result.
#
# Numbers picked for a typical Generac H-100 install:
#  - The H-100 reports total_kw and avg_current as INTEGERS on this
#    register map (no scale factor — see registers/h100.yaml), so the
#    floor is 0/1, not noisy fractional values.
#  - An unloaded but running generator typically shows 0 kW and a few
#    amps of excitation/measurement noise.
#  - Even a trivial backup load (one rack of equipment) is comfortably
#    above the ON thresholds, so we don't miss real transfers.
LOAD_ON_KW_THRESHOLD = 2.0          # ≥ 2 kW AND ≥ 5 A on the bus → "generator"
LOAD_ON_CURRENT_THRESHOLD = 5.0
LOAD_OFF_KW_THRESHOLD = 1.0         # < 1 kW AND < 2 A → "utility"
LOAD_OFF_CURRENT_THRESHOLD = 2.0


@dataclass
class StateSnapshot:
    engine_state: str = "unknown"
    state_started_at: float = field(default_factory=time.time)
    active_alarms: set[str] = field(default_factory=set)  # alarm codes
    last_reading: Reading = field(default_factory=Reading)
    comms: CommsHealth = field(default_factory=CommsHealth)
    # H-100 front-panel key-switch position: 'auto' | 'manual' | 'off' |
    # 'unknown'. The controller only honors remote start/stop/transfer
    # writes when this is 'auto'; the control service rejects with 409
    # otherwise so an operator clicking the UI button on a panel that's
    # been locally locked out doesn't get a silent no-op at the unit.
    panel_mode: str = "unknown"
    # Derived: which source is currently supplying the load. 'utility' |
    # 'generator' | 'unknown'. Derived from engine state + output kW/
    # current — see _derive_load_source. The H-100 has no direct ATS
    # position register; this inference is the closest we can get
    # without wiring into the transfer switch.
    load_source: str = "unknown"
    load_source_started_at: float = field(default_factory=time.time)

    @property
    def time_in_state_s(self) -> int:
        return int(time.time() - self.state_started_at)

    @property
    def time_in_load_source_s(self) -> int:
        return int(time.time() - self.load_source_started_at)

    @property
    def alarm_raw(self) -> int:
        """Legacy field: non-zero iff any alarm is active."""
        return 1 if self.active_alarms else 0


class StateMachine:
    """Maintains the snapshot, raises/clears alarms, emits events."""

    def __init__(
        self,
        regmap: RegisterMap,
        db: Database,
        bus: "EventBus",
        ats_service: "AtsService | None" = None,
    ):
        self.regmap = regmap
        self.db = db
        self.bus = bus
        # Optional ATS-Pi companion service. When present and reporting
        # healthy comms, its `position` is used as the authoritative
        # loadSource instead of the H-100-derived value (ICD §10).
        self.ats = ats_service
        self.snap = StateSnapshot()
        # Per-alarm-code count of how many *consecutive* polls have seen
        # the bit set. Drives the ``min_poll_count`` debounce filter.
        # Reset (entry deleted) the first time the bit reads low.
        self._alarm_poll_counts: dict[str, int] = {}
        # Alarm codes that we have called ``db.raise_alarm`` on during
        # this session. Tracked separately from ``snap.active_alarms``
        # because state-conditional suppression can hide an alarm from
        # the UI while the underlying H-100 bit is still asserting — we
        # still need to fire a clear event (and call ``db.clear_alarm``)
        # when the bit eventually goes low. Without this set, a bit
        # that goes set in 'running' and then clears while the engine
        # is 'cooling' would leak as a stale row in the DB.
        self._raised_this_session: set[str] = set()

    def apply_regmap(self, new_regmap: RegisterMap) -> None:
        """Swap in a freshly-loaded register map (POST /api/registers/reload).

        The state machine just looks up register values by name and reads
        the YAML's engine_state_bits / alarm_bits / panel_mode_bits rules.
        Swapping the reference is enough — Python attribute assignment is
        atomic under the GIL, and the next update() call will use the
        new rules. Cleared alarms whose codes no longer exist in the new
        map will be reaped naturally on the next diff.
        """
        self.regmap = new_regmap

    def _derive_load_source(self, values: dict, engine_state: str, prev: str) -> str:
        """Dispatcher: prefer the ATS-Pi's direct position reading when
        available, otherwise fall back to the H-100 electrical inference.

        Per ICD §10 the ATS-Pi reads the switch's actual position
        contacts, which is ground truth — better than any inference.
        We only fall back when ATS-Pi is unreachable, hasn't completed
        its first base poll (so we can't verify its ICD version), or
        reports `position == "unknown"` itself.

        The fallback (`_derive_load_source_from_telemetry`) is the
        original H-100-only logic; it is unchanged and remains covered
        by the existing test_state_machine.py suite.
        """
        if self.ats is not None and self.ats.is_authoritative():
            pos = self.ats.snap.position
            if pos != "unknown":
                return pos
        return self._derive_load_source_from_telemetry(values, engine_state, prev)

    def _derive_load_source_from_telemetry(self, values: dict, engine_state: str, prev: str) -> str:
        """Infer whether the active load is supplied by utility or generator.

        The H-100 doesn't expose ATS position directly. We combine two
        facts the controller does know:

          1. Engine state. Several states unambiguously mean the load is
             NOT on the generator — `stopped`, `cranking` (still coming
             up), `cooling` (retransfer already happened), `exercising`
             (quiet-test is always unloaded by design on the H-100).
          2. Generator output. When the engine is `running` or `alarm`,
             the generator is *capable* of carrying load. Non-zero
             current AND kW prove that the ATS has actually closed onto
             the generator side.

        Hysteresis (LOAD_ON_* vs LOAD_OFF_*) prevents flicker right at
        the detection boundary. AND across both sensors guards against
        a single CT/kW fault: a broken CT reading 0 won't falsely
        retransfer us back to 'utility', and a spurious high reading on
        one sensor can't falsely declare a transfer to generator.

        When both readings are absent (e.g. before the first base-tier
        poll has completed) we preserve the prior classification rather
        than flipping to a default. The poller's value dict is
        cumulative across polls, so this only matters at startup or
        during a sustained base-tier comms outage.
        """
        if engine_state == "unknown":
            # Engine state will firm up within a prime poll — don't
            # generate noise from a transient 'unknown' on the first tick.
            return prev

        # Engine state alone is sufficient for these — the generator
        # cannot be carrying load while it's stopped, starting, cooling
        # down, or running a (by-design unloaded) quiet test.
        if engine_state in ("stopped", "cranking", "cooling", "exercising"):
            return "utility"

        # `running` or `alarm` — verify via electrical readings.
        current = values.get("avg_current")
        kw = values.get("total_kw")

        if current is None and kw is None:
            # No electrical telemetry available yet. Hold the previous
            # assessment. At cold boot default to 'utility' rather than
            # 'unknown' so the UI shows a sane source rather than a
            # question mark for the ~15 s before the first base poll.
            return prev if prev != "unknown" else "utility"

        current_val = float(current if current is not None else 0)
        kw_val = float(kw if kw is not None else 0)

        if prev == "generator":
            # Stay on 'generator' until BOTH sensors clearly indicate
            # no-load. AND on the off-side: a single broken CT reading 0
            # while the gen actually carries load won't falsely declare
            # a retransfer.
            if current_val < LOAD_OFF_CURRENT_THRESHOLD and kw_val < LOAD_OFF_KW_THRESHOLD:
                return "utility"
            return "generator"

        # Currently 'utility' or 'unknown' — both sensors must agree
        # before we declare a transfer to the generator. AND on the
        # on-side: a spurious high reading on a single sensor can't
        # cause a false-positive transfer event.
        if (
            current_val >= LOAD_ON_CURRENT_THRESHOLD
            and kw_val >= LOAD_ON_KW_THRESHOLD
        ):
            return "generator"
        return "utility"

    @staticmethod
    def _load_source_event(old: str, new: str) -> tuple[str, str] | None:
        """Map a load-source transition to a (severity, message) for the DB
        events log, or None if the transition is too uninteresting to log.

        Boot-time 'unknown → utility' is suppressed — it's just initial
        firming and would otherwise leave an unhelpful row in the log
        every restart. All other transitions are logged; utility →
        generator is severity 'warn' (outage / forced transfer) and
        generator → utility is 'ok' (retransfer / restoration).
        """
        if old == "unknown" and new == "utility":
            return None
        if new == "generator":
            return ("warn", f"Load source → GENERATOR (was {old})")
        if new == "utility":
            return ("ok", f"Load source → UTILITY (was {old})")
        # new == "unknown" — sustained comms / readings gap; informational.
        return ("warn", f"Load source → UNKNOWN (was {old})")

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

        # Alarms — with two filters layered on top of the raw bits:
        #   1. ``min_poll_count`` debounce — the bit must stay set for
        #      N consecutive polls before we surface the alarm. Catches
        #      transient firmware flickers (e.g. a fuel-high blip while
        #      the day tank refills after an exercise).
        #   2. ``suppress_in_states`` state filter — the alarm is hidden
        #      from the UI / Slack while the engine is in a state where
        #      the underlying signal isn't meaningful (e.g. phase
        #      rotation during cool-down, when the AVR is dropping out).
        # Both filters preserve correctness: a real persistent alarm
        # still fires once the debounce elapses, and a state-suppressed
        # alarm still gets cleared in the DB when its bit goes low so
        # nothing leaks as a stale row.
        raw_active = self.regmap.derive_active_alarms(reading.values)
        raw_active_codes = {ab.code for ab in raw_active}

        # Maintain per-code debounce counters off the raw bits, not the
        # filtered set — state suppression shouldn't reset the counter.
        for ab in raw_active:
            self._alarm_poll_counts[ab.code] = self._alarm_poll_counts.get(ab.code, 0) + 1
        for code in list(self._alarm_poll_counts):
            if code not in raw_active_codes:
                del self._alarm_poll_counts[code]

        effective_active = [
            ab for ab in raw_active
            if self._alarm_poll_counts.get(ab.code, 0) >= ab.min_poll_count
            and self.snap.engine_state not in ab.suppress_in_states
        ]
        effective_codes = {ab.code for ab in effective_active}
        prev_displayed_codes = self.snap.active_alarms

        # Raise: alarms that are newly visible in the effective set.
        for ab in effective_active:
            if ab.code in prev_displayed_codes:
                continue
            raised = self.db.raise_alarm(ab.code, ab.desc, ab.severity, ab.mask)
            # Track that we've seen this alarm during the session even
            # if the DB already had a row for it (return value False) —
            # we still want to fire the clear when the bit eventually
            # goes low.
            self._raised_this_session.add(ab.code)
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

        # Clear: only when the underlying BIT goes low, not just when
        # the alarm leaves the displayed set because of state
        # suppression. This is the bit that distinguishes "alarm hidden
        # because we entered cool-down" (don't fire clear event) from
        # "alarm actually resolved" (fire clear event + clear DB row).
        for code in list(self._raised_this_session):
            if code in raw_active_codes:
                continue
            ab = next((x for x in self.regmap.alarm_bits if x.code == code), None)
            desc = ab.desc if ab else code
            cleared = self.db.clear_alarm(code)
            self._raised_this_session.discard(code)
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

        # Display set drives the UI's Active Alarms widget and is also
        # what the previous-tick diff uses for raise detection above.
        self.snap.active_alarms = effective_codes

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
        # Panel key-switch — derive from the YAML's panel_mode_bits rules.
        # Falls back to 'unknown' until the prime tier polls input_status_1.
        self.snap.panel_mode = self.regmap.derive_panel_mode(reading.values)

        # Load source — utility vs generator. Runs after engine state is
        # updated so cooling/cranking states immediately move us back to
        # 'utility' rather than waiting for the next base-tier poll's
        # current/kW readings.
        new_load_source = self._derive_load_source(
            reading.values, self.snap.engine_state, self.snap.load_source
        )
        if new_load_source != self.snap.load_source:
            old_source = self.snap.load_source
            self.snap.load_source = new_load_source
            self.snap.load_source_started_at = time.time()

            # When the ATS-Pi is the authoritative source of the load
            # source, it has already emitted an `ats-position` event
            # and written an ATS_POSITION row to the events log for the
            # same physical transition. Suppress the duplicate
            # `load-source` event here so the operator's events feed
            # shows one entry per real-world event, not two.
            ats_authoritative = (
                self.ats is not None and self.ats.is_authoritative()
            )
            if not ats_authoritative:
                emitted.append({
                    "type": "load-source",
                    "from": old_source,
                    "to": new_load_source,
                    "ts": time.time(),
                })
                db_event = self._load_source_event(old_source, new_load_source)
                if db_event is not None:
                    severity, message = db_event
                    self.db.write_event(
                        severity=severity,
                        type_="LOAD_SOURCE",
                        message=message,
                        meta=None,
                    )
                    log.info("Load source (telemetry): %s -> %s", old_source, new_load_source)
            else:
                log.debug(
                    "Load source updated from ATS-Pi authority: %s -> %s "
                    "(no duplicate event emitted)",
                    old_source, new_load_source,
                )

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
