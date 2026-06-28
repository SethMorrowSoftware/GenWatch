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

# How many *consecutive* prime polls must show a load-source disagreement
# between the ATS-Pi reading and the H-100 electrical output before we
# raise the ATS_LOADSOURCE_DISAGREE warning alarm. At the default
# prime cadence (1.5 s) this is ~4.5 s — long enough to ride through a
# legitimate utility↔generator transfer window where the ATS position
# and the H-100 kW/A briefly disagree by design, short enough that a
# real stuck-aux-contact failure surfaces before an operator could act
# on the wrong loadSource.
LOADSOURCE_DISAGREE_DEBOUNCE_POLLS = 3
LOADSOURCE_DISAGREE_ALARM_CODE = "ATS_LOADSOURCE_DISAGREE"

# Numeric range alarms (H-5). Only evaluated while the engine is producing —
# coolant/oil/rpm/etc. ranges are meaningless at rest. Debounced so a single
# noisy sample can't raise.
NUMERIC_ALARM_STATES = {"running", "exercising"}
NUMERIC_ALARM_MIN_POLLS = 3
NUMERIC_ALARM_CODE_PREFIX = "RANGE_"


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
        # ATS-vs-H-100 load-source disagreement detector state.
        # ``_count`` increments on every poll that observes a
        # disagreement and resets on an agreeing poll; the alarm is
        # only raised once ``count >= LOADSOURCE_DISAGREE_DEBOUNCE_POLLS``
        # to ride through normal transfer transients.
        # ``_raised`` mirrors db state so we know whether to emit a
        # clear event when the condition resolves (or when the ATS-Pi
        # loses authority and the comparison becomes meaningless).
        self._loadsource_disagree_count: int = 0
        self._loadsource_disagree_raised: bool = False
        # Numeric range-alarm state (H-5). Mirrors the bit-alarm debounce:
        # per-code consecutive out-of-range poll counts, and the codes we've
        # raised so we know to fire the clear. Active only when the register
        # map enables numeric alarms (default off).
        self._numeric_poll_counts: dict[str, int] = {}
        self._numeric_raised: dict[str, str] = {}  # code -> severity

    def apply_regmap(self, new_regmap: RegisterMap) -> None:
        """Swap in a freshly-loaded register map (POST /api/registers/reload).

        The state machine just looks up register values by name and reads
        the YAML's engine_state_bits / alarm_bits / panel_mode_bits rules.
        Swapping the reference is atomic under the GIL, and the next
        update() call will use the new rules.

        Reset the debounce counters but NOT the raised-this-session set.

        - ``_alarm_poll_counts`` must reset, otherwise an operator
          iterating on YAML (remove a rule, fix a bit position, re-add
          it on the next save) could see the alarm fire instantly on
          the first poll after the second reload because the counter
          from before the first reload was still accumulating —
          bypassing the ``min_poll_count`` debounce that exists
          specifically to ride out transient bit flickers.

        - ``_raised_this_session`` must be preserved. It tracks codes
          we've called ``db.raise_alarm()`` on so we know to call
          ``db.clear_alarm()`` when the underlying bit eventually goes
          low. Wiping it would leak ``alarms_active`` rows for alarms
          raised before the reload — the bit-goes-low cleanup loop in
          update() iterates this set, so anything not in it never
          gets cleared from the DB.
        """
        self.regmap = new_regmap
        self._alarm_poll_counts.clear()

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

        # Snap the regmap reference up front so every derivation in this
        # update uses one consistent rule set. Without this, an
        # apply_regmap() that lands mid-update could leave engine_state
        # derived against the new map while the alarm_bits loop below
        # still iterates the old map's rules — producing a one-tick
        # phantom raise/clear of an alarm whose code just got renamed.
        # The reference swap in apply_regmap is atomic under the GIL,
        # so capturing it once gives us a coherent view.
        regmap = self.regmap

        # Engine state — derived from bitfield rules.
        new_state = regmap.derive_engine_state(reading.values)
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
        raw_active = regmap.derive_active_alarms(reading.values)
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
            ab = next((x for x in regmap.alarm_bits if x.code == code), None)
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

        # Numeric range alarms (H-5) — optional software backstop derived from
        # the per-register warn_range/alarm_range bands. Off by default; the
        # bands must be field-verified before enabling (numeric_alarms_enabled
        # in h100.yaml). Layered onto active_alarms like the disagree alarm.
        if regmap.numeric_alarms_enabled:
            self._check_numeric_ranges(regmap, reading, emitted)
        if self._numeric_raised:
            self.snap.active_alarms = self.snap.active_alarms | set(self._numeric_raised)

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
        self.snap.panel_mode = regmap.derive_panel_mode(reading.values)

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

        # Cross-check ATS-reported position against H-100 electrical
        # output. Catches the "stuck ATS aux contact" failure that's
        # silent from either side alone — ATS says utility while the
        # generator is delivering 200 kW, or ATS says generator while
        # the engine output is essentially zero.
        self._check_loadsource_disagreement(reading.values, emitted)

        # Layer the derived disagreement alarm onto active_alarms. The
        # H-100 alarm pipeline above overwrote this set with only the
        # bit-derived codes; we re-add the derived code if currently
        # raised so the UI's active-alarms widget shows it across polls,
        # and ensure it's evicted on clear.
        if self._loadsource_disagree_raised:
            self.snap.active_alarms = self.snap.active_alarms | {LOADSOURCE_DISAGREE_ALARM_CODE}
        else:
            self.snap.active_alarms = self.snap.active_alarms - {LOADSOURCE_DISAGREE_ALARM_CODE}

        self.snap.comms = comms
        self.snap.last_reading = reading
        return emitted

    # ─── Load-source disagreement (ATS vs H-100 cross-check) ──────────

    def _check_numeric_ranges(
        self, regmap: RegisterMap, reading: Reading, emitted: list[dict[str, Any]]
    ) -> None:
        """Raise/clear warn|alarm events from per-register warn_range/
        alarm_range bands (H-5). Opt-in software backstop on top of the
        H-100's own status bits.

        Only evaluated while the engine is producing (NUMERIC_ALARM_STATES) and
        only on FRESH, non-None decoded values — a stale/evicted register
        (values.get → None) is skipped so an eviction can't masquerade as an
        out-of-range reading. Debounced NUMERIC_ALARM_MIN_POLLS consecutive
        polls, mirroring the bit-alarm path. A value back in range clears.
        """
        producing = self.snap.engine_state in NUMERIC_ALARM_STATES

        def severity_for(reg) -> tuple[str, str] | None:
            """Return (severity, band_str) if out of range, else None."""
            val = reading.values.get(reg.name)
            if val is None:
                return None
            if reg.alarm_range is not None:
                lo, hi = reg.alarm_range
                if val < lo or val > hi:
                    return "alarm", f"{lo}–{hi}"
            if reg.warn_range is not None:
                lo, hi = reg.warn_range
                if val < lo or val > hi:
                    return "warn", f"{lo}–{hi}"
            return None

        active_now: dict[str, tuple[str, str, object]] = {}  # code -> (sev, reg, val)
        if producing:
            for reg in regmap.registers:
                if reg.warn_range is None and reg.alarm_range is None:
                    continue
                hit = severity_for(reg)
                if hit is None:
                    continue
                code = f"{NUMERIC_ALARM_CODE_PREFIX}{reg.name.upper()}"
                active_now[code] = (hit[0], reg, hit[1])

        # Debounce counters off the raw out-of-range observation.
        for code in active_now:
            self._numeric_poll_counts[code] = self._numeric_poll_counts.get(code, 0) + 1
        for code in list(self._numeric_poll_counts):
            if code not in active_now:
                del self._numeric_poll_counts[code]

        # Raise: out of range for long enough and not already raised.
        for code, (sev, reg, band) in active_now.items():
            if self._numeric_poll_counts.get(code, 0) < NUMERIC_ALARM_MIN_POLLS:
                continue
            if code in self._numeric_raised:
                continue
            val = reading.values.get(reg.name)
            desc = f"{reg.name} out of range ({val}{(' ' + reg.unit) if reg.unit else ''}, allowed {band})"
            self._numeric_raised[code] = sev
            raised = self.db.raise_alarm(code, desc, sev, 0)
            if raised:
                self.db.write_event(
                    severity=sev, type_="ALARM",
                    message=f"Alarm raised — {desc}", meta=f"code {code}",
                )
                emitted.append({
                    "type": "alarm", "code": code, "desc": desc,
                    "severity": sev, "ts": time.time(),
                })
                log.warning("Numeric alarm raised: %s %s", code, desc)

        # Clear: previously raised but now back in range (or engine no longer
        # producing, so the band no longer applies).
        for code in list(self._numeric_raised):
            if code in active_now:
                continue
            self._numeric_raised.pop(code, None)
            cleared = self.db.clear_alarm(code)
            if cleared:
                self.db.write_event(
                    severity="ok", type_="ALARM",
                    message=f"Alarm cleared — {code}", meta=f"code {code}",
                )
                emitted.append({
                    "type": "alarm-cleared", "code": code, "desc": code,
                    "ts": time.time(),
                })
                log.info("Numeric alarm cleared: %s", code)

    def _check_loadsource_disagreement(
        self, values: dict, emitted: list[dict[str, Any]]
    ) -> None:
        """Compare the ATS-Pi-reported position against the H-100's own
        electrical output and surface a persistent mismatch as a
        warn-severity alarm.

        Only runs when the ATS-Pi is authoritative — there's no
        cross-check to perform when we're already deriving loadSource
        from H-100 telemetry alone (the values would tautologically
        agree). Only meaningful while the engine could plausibly be
        carrying load (states ``running`` / ``alarm``); other states
        clear the alarm because the comparison can't be evaluated.

        Hysteresis matches ``_derive_load_source_from_telemetry`` —
        same LOAD_ON / LOAD_OFF thresholds — so we never raise an
        alarm at a kW/A level we'd otherwise classify as load-on-gen
        ourselves. Debounce avoids false positives during the normal
        transfer window where ATS position changes a poll or two before
        the kW reading catches up.
        """
        # Authority gate. When ATS is not authoritative the loadSource
        # we display IS the H-100-derived value; a "disagreement"
        # against itself is undefined, and any alarm previously raised
        # is no longer evaluable — clear it.
        if self.ats is None or not self.ats.is_authoritative():
            if self._loadsource_disagree_raised:
                self._emit_disagree_clear(
                    "ATS-Pi no longer authoritative", emitted
                )
            self._loadsource_disagree_count = 0
            return

        # Engine state gate. Only `running` and `alarm` are states in
        # which the generator could be delivering load. `cranking` and
        # `cooling` are transient (load is on utility by design);
        # `exercising` is by design unloaded; `stopped` is trivially
        # zero output.
        if self.snap.engine_state not in ("running", "alarm"):
            if self._loadsource_disagree_raised:
                self._emit_disagree_clear(
                    f"engine state is {self.snap.engine_state}", emitted
                )
            self._loadsource_disagree_count = 0
            return

        ats_pos = self.ats.snap.position
        # 'transferring' is a few-hundred-ms intermediate while the ATS
        # is between contacts — no comparison is meaningful. 'unknown'
        # means the ATS-Pi itself can't tell. Hold the counter steady.
        if ats_pos not in ("utility", "generator"):
            return

        current = values.get("avg_current")
        kw = values.get("total_kw")
        if current is None or kw is None:
            # No electrical telemetry yet (first base poll hasn't
            # landed). Hold counter steady rather than reset — we don't
            # want a slow base tier to wipe out accumulated evidence
            # from previous polls.
            return

        current_val = float(current)
        kw_val = float(kw)

        disagreement_msg: str | None = None
        if (
            ats_pos == "utility"
            and current_val >= LOAD_ON_CURRENT_THRESHOLD
            and kw_val >= LOAD_ON_KW_THRESHOLD
        ):
            disagreement_msg = (
                f"ATS reports UTILITY but H-100 is delivering "
                f"{kw_val:.1f} kW / {current_val:.0f} A. "
                "Likely a stuck ATS aux contact or miswired position sense."
            )
        elif (
            ats_pos == "generator"
            and current_val < LOAD_OFF_CURRENT_THRESHOLD
            and kw_val < LOAD_OFF_KW_THRESHOLD
            # Only treat ats=generator + zero-output as a fault when
            # utility is ALSO unavailable. With utility present this is
            # the normal ASCO retransfer-delay window: utility restored,
            # ATS is still on `generator` waiting for its retransfer
            # timer to expire, building load may briefly drop to zero.
            # Without this gate the alarm fires on every legitimate
            # utility-restore cycle. The failure mode worth catching —
            # a stuck aux contact telling us we're on generator while
            # the building actually has no power — is only diagnosable
            # when utility is also reported as gone (normal_available
            # is False).
            and self.ats.snap.normal_available is False
        ):
            disagreement_msg = (
                f"ATS reports GENERATOR with utility UNAVAILABLE but "
                f"H-100 output is {kw_val:.1f} kW / {current_val:.1f} A. "
                "Likely a stuck ATS aux contact (claiming generator "
                "while the building actually has no power) or a broken "
                "kW/A sensor on the H-100."
            )

        if disagreement_msg is not None:
            self._loadsource_disagree_count += 1
            if (
                self._loadsource_disagree_count >= LOADSOURCE_DISAGREE_DEBOUNCE_POLLS
                and not self._loadsource_disagree_raised
            ):
                self._emit_disagree_raise(disagreement_msg, emitted)
        else:
            # Agreement on this poll — clear any prior raise and reset
            # the debounce counter.
            if self._loadsource_disagree_raised:
                self._emit_disagree_clear(
                    "ATS and H-100 now agree on load source", emitted
                )
            self._loadsource_disagree_count = 0

    def _emit_disagree_raise(
        self, message: str, emitted: list[dict[str, Any]]
    ) -> None:
        """Raise the disagreement alarm: DB row + event-log row + bus event."""
        raised = self.db.raise_alarm(
            LOADSOURCE_DISAGREE_ALARM_CODE, message, "warn", 0
        )
        self._loadsource_disagree_raised = True
        if raised:
            self.db.write_event(
                severity="warn",
                type_="ALARM",
                message=f"Alarm raised — {message}",
                meta=f"code {LOADSOURCE_DISAGREE_ALARM_CODE}",
            )
            emitted.append({
                "type": "alarm",
                "code": LOADSOURCE_DISAGREE_ALARM_CODE,
                "desc": message,
                "severity": "warn",
                "ts": time.time(),
            })
            log.warning("Load-source disagreement raised: %s", message)
        # active_alarms set membership is synced at the end of update()
        # so it survives the H-100 alarm pipeline's overwrite each poll.

    def _emit_disagree_clear(
        self, reason: str, emitted: list[dict[str, Any]]
    ) -> None:
        """Clear the disagreement alarm: DB row + event-log row + bus event."""
        cleared = self.db.clear_alarm(LOADSOURCE_DISAGREE_ALARM_CODE)
        self._loadsource_disagree_raised = False
        if cleared:
            self.db.write_event(
                severity="ok",
                type_="ALARM",
                message=f"Alarm cleared — load-source disagreement ({reason})",
                meta=f"code {LOADSOURCE_DISAGREE_ALARM_CODE}",
            )
            emitted.append({
                "type": "alarm-cleared",
                "code": LOADSOURCE_DISAGREE_ALARM_CODE,
                "desc": f"load-source disagreement cleared ({reason})",
                "ts": time.time(),
            })
            log.info("Load-source disagreement cleared (%s)", reason)
        # active_alarms set membership is synced at the end of update().


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
