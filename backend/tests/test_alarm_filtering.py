"""Alarm filtering tests — debounce + state-conditional suppression.

These exercise the two filters added to ``services/state.py`` and the
YAML loader. The behaviour contract:

  - Default (no fields) → alarms raise immediately, same as before.
  - ``min_poll_count: N`` → the bit must read set on N consecutive
    polls before the alarm raises. A flicker shorter than N polls
    never makes it to the event log.
  - ``suppress_in_states: [...]`` → while the engine is in any of
    those states, the alarm is hidden from the UI's active set and
    no raise event fires. An already-raised alarm becomes invisible
    when the state changes into a suppressed one, but the DB row
    remains so the alarm clears correctly when its underlying bit
    eventually goes low.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from genwatch.modbus.poller import CommsHealth, Reading
from genwatch.modbus.registers import _load_alarm_bit, load_register_map
from genwatch.services.state import EventBus, StateMachine


@pytest.fixture
def regmap():
    return load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.raise_alarm.return_value = True
    db.clear_alarm.return_value = True
    return db


@pytest.fixture
def sm(regmap, fake_db):
    return StateMachine(regmap, fake_db, EventBus())


def _values_for(state: str, **bits: int) -> dict:
    """Build a values dict that decodes to the requested engine state.

    Additional keyword args become register name → integer entries —
    used to flip specific alarm bits per test.
    """
    s1 = 0
    s7 = 0
    if state == "alarm":
        s1 |= 0x8000
    elif state == "cranking":
        s7 |= 0x1000
    elif state == "cooling":
        s7 |= 0x2000
    elif state == "exercising":
        s7 |= 0x0020
    elif state == "running":
        s1 |= 0x2000
    elif state == "stopped":
        s1 |= 0x0100
    out: dict[str, int] = {"output_status_1": s1, "output_status_7": s7}
    out.update(bits)
    return out


def _comms() -> CommsHealth:
    c = CommsHealth()
    c.state = "healthy"
    return c


# ─── YAML loader ──────────────────────────────────────────────────────────


def test_alarm_bit_defaults_when_filter_fields_absent():
    ab = _load_alarm_bit({
        "register": "output_status_1",
        "mask": 0x8000,
        "code": "TEST",
        "desc": "Test",
        "severity": "alarm",
    })
    assert ab.suppress_in_states == ()
    assert ab.min_poll_count == 1


def test_alarm_bit_parses_suppress_list():
    ab = _load_alarm_bit({
        "register": "output_status_7",
        "mask": 0x0200,
        "code": "VOLT_PHASE_ROTATION",
        "desc": "Check Voltage Phase Rotation",
        "severity": "warn",
        "suppress_in_states": ["cooling", "cranking", "stopped"],
        "min_poll_count": 3,
    })
    assert ab.suppress_in_states == ("cooling", "cranking", "stopped")
    assert ab.min_poll_count == 3


def test_alarm_bit_normalizes_case_and_whitespace():
    ab = _load_alarm_bit({
        "register": "x", "mask": 1, "code": "X", "desc": "", "severity": "warn",
        "suppress_in_states": ["  Cooling  ", "STOPPED", ""],
    })
    assert ab.suppress_in_states == ("cooling", "stopped")


def test_alarm_bit_accepts_scalar_suppress_value():
    ab = _load_alarm_bit({
        "register": "x", "mask": 1, "code": "X", "desc": "", "severity": "warn",
        "suppress_in_states": "cooling",
    })
    assert ab.suppress_in_states == ("cooling",)


def test_alarm_bit_invalid_min_poll_count_floors_to_one():
    ab = _load_alarm_bit({
        "register": "x", "mask": 1, "code": "X", "desc": "", "severity": "warn",
        "min_poll_count": 0,
    })
    assert ab.min_poll_count == 1
    ab2 = _load_alarm_bit({
        "register": "x", "mask": 1, "code": "X", "desc": "", "severity": "warn",
        "min_poll_count": "not a number",
    })
    assert ab2.min_poll_count == 1


def test_h100_yaml_applies_filters_to_target_alarms(regmap):
    """The on-disk YAML must have the filters we shipped — guards against
    accidental removal during future edits.
    """
    by_code = {ab.code: ab for ab in regmap.alarm_bits}

    assert by_code["VOLT_PHASE_ROTATION"].suppress_in_states == ("cooling", "cranking", "stopped")
    assert by_code["VOLT_PHASE_ROTATION"].min_poll_count == 3
    assert by_code["CURR_PHASE_ROTATION"].suppress_in_states == ("cooling", "cranking", "stopped")
    assert by_code["CURR_PHASE_ROTATION"].min_poll_count == 3
    assert by_code["FUEL_LEVEL_HIGH_ALARM"].min_poll_count == 20
    assert by_code["FUEL_LEVEL_HIGH_WARN"].min_poll_count == 20

    # And: every other alarm keeps the default (immediate, never suppressed).
    other = [
        ab for ab in regmap.alarm_bits
        if ab.code not in {
            "VOLT_PHASE_ROTATION", "CURR_PHASE_ROTATION",
            "FUEL_LEVEL_HIGH_ALARM", "FUEL_LEVEL_HIGH_WARN",
        }
    ]
    for ab in other:
        assert ab.suppress_in_states == (), f"{ab.code} unexpectedly suppressed"
        assert ab.min_poll_count == 1, f"{ab.code} unexpectedly debounced"


# ─── Debounce behaviour ───────────────────────────────────────────────────


def test_debounce_blocks_alarm_below_threshold(sm, regmap):
    """FUEL_LEVEL_HIGH_ALARM needs 20 consecutive polls before firing.
    19 polls of the bit set should not raise.
    """
    # The bit is mask 0x2000 in output_status_3 — but the state machine
    # only looks at *active* alarms via derive_active_alarms, which reads
    # named registers. We need output_status_3 in the values dict.
    for _ in range(19):
        sm.update(
            Reading(values=_values_for("running", output_status_3=0x2000)),
            _comms(),
        )
    assert "FUEL_LEVEL_HIGH_ALARM" not in sm.snap.active_alarms


def test_debounce_fires_alarm_at_threshold(sm, regmap, fake_db):
    """Twentieth consecutive poll fires."""
    for _ in range(20):
        sm.update(
            Reading(values=_values_for("running", output_status_3=0x2000)),
            _comms(),
        )
    assert "FUEL_LEVEL_HIGH_ALARM" in sm.snap.active_alarms
    fake_db.raise_alarm.assert_any_call(
        "FUEL_LEVEL_HIGH_ALARM", "Fuel Level High Alarm", "alarm", 0x2000
    )


def test_debounce_resets_when_bit_clears(sm, regmap):
    """A flicker shorter than the debounce window never fires, even
    if the bit later sets again."""
    # 15 polls with the bit set
    for _ in range(15):
        sm.update(
            Reading(values=_values_for("running", output_status_3=0x2000)),
            _comms(),
        )
    # Bit clears — counter must reset
    sm.update(
        Reading(values=_values_for("running", output_status_3=0)),
        _comms(),
    )
    # Re-set the bit; only 1 fresh poll has had it set
    sm.update(
        Reading(values=_values_for("running", output_status_3=0x2000)),
        _comms(),
    )
    assert "FUEL_LEVEL_HIGH_ALARM" not in sm.snap.active_alarms


def test_default_alarm_fires_immediately(sm, fake_db):
    """An alarm without ``min_poll_count`` keeps the original immediate
    behaviour — one poll with the bit set is enough.
    """
    sm.update(
        Reading(values=_values_for("running", output_status_1=0x2000 | 0x0010)),
        _comms(),
    )
    # OVERCRANK is mask 0x0010 in output_status_1, no debounce configured
    assert "OVERCRANK" in sm.snap.active_alarms


# ─── State-conditional suppression ────────────────────────────────────────


def test_phase_rotation_suppressed_in_cooling(sm, fake_db):
    """The bit is set during cool-down (AVR drop-out artifact) but
    should not appear as an active alarm or fire an event.
    """
    # Need to clear the debounce: 3 consecutive polls with the bit set
    # in cooling state.
    for _ in range(3):
        sm.update(
            Reading(values=_values_for("cooling", output_status_7=0x2000 | 0x0200)),
            _comms(),
        )
    assert "VOLT_PHASE_ROTATION" not in sm.snap.active_alarms
    raise_calls = [
        c for c in fake_db.raise_alarm.call_args_list
        if c.args and c.args[0] == "VOLT_PHASE_ROTATION"
    ]
    assert raise_calls == []


def test_phase_rotation_fires_in_running(sm, fake_db):
    """Same bit, different state — running is NOT suppressed."""
    for _ in range(3):
        sm.update(
            Reading(values=_values_for("running", output_status_7=0x0200)),
            _comms(),
        )
    assert "VOLT_PHASE_ROTATION" in sm.snap.active_alarms


def test_suppression_hides_previously_raised_alarm_on_state_change(sm, fake_db):
    """Alarm fires in running, then engine moves to cooling with the
    bit still set — the alarm leaves the displayed set, but no
    spurious "cleared" event fires because the bit is still set.
    """
    # Raise in running
    for _ in range(3):
        sm.update(
            Reading(values=_values_for("running", output_status_7=0x0200)),
            _comms(),
        )
    assert "VOLT_PHASE_ROTATION" in sm.snap.active_alarms
    fake_db.clear_alarm.reset_mock()

    # Move to cooling, bit still set
    emitted = sm.update(
        Reading(values=_values_for("cooling", output_status_7=0x2000 | 0x0200)),
        _comms(),
    )
    # No longer displayed
    assert "VOLT_PHASE_ROTATION" not in sm.snap.active_alarms
    # But no clear event was fired
    cleared_events = [e for e in emitted if e.get("type") == "alarm-cleared"]
    assert not any(e.get("code") == "VOLT_PHASE_ROTATION" for e in cleared_events)
    # And clear_alarm was not called for it
    pr_clears = [
        c for c in fake_db.clear_alarm.call_args_list
        if c.args and c.args[0] == "VOLT_PHASE_ROTATION"
    ]
    assert pr_clears == []


def test_clear_fires_when_bit_clears_during_suppression(sm, fake_db):
    """The whole point of tracking ``_raised_this_session`` separately:
    an alarm that became hidden by state suppression must still be
    cleared in the DB when the underlying bit eventually goes low.
    """
    # Raise in running
    for _ in range(3):
        sm.update(
            Reading(values=_values_for("running", output_status_7=0x0200)),
            _comms(),
        )
    assert "VOLT_PHASE_ROTATION" in sm._raised_this_session

    # Suppressed: cooling state, bit still set
    sm.update(
        Reading(values=_values_for("cooling", output_status_7=0x2000 | 0x0200)),
        _comms(),
    )
    # Bit clears (still in cooling)
    fake_db.clear_alarm.reset_mock()
    emitted = sm.update(
        Reading(values=_values_for("cooling", output_status_7=0x2000)),
        _comms(),
    )
    # Clear event fired + DB was cleared
    cleared = [e for e in emitted if e.get("type") == "alarm-cleared" and e.get("code") == "VOLT_PHASE_ROTATION"]
    assert len(cleared) == 1
    fake_db.clear_alarm.assert_any_call("VOLT_PHASE_ROTATION")
    assert "VOLT_PHASE_ROTATION" not in sm._raised_this_session


def test_suppressed_alarm_returns_when_state_changes_back(sm, fake_db):
    """Bit stays set, state goes running → cooling → running. The
    alarm should reappear in the displayed set when the engine
    returns to a non-suppressed state, even though it was hidden in
    between. Because raise_alarm is idempotent (returns False the
    second time), no duplicate "raised" event fires.
    """
    # Raise in running
    for _ in range(3):
        sm.update(
            Reading(values=_values_for("running", output_status_7=0x0200)),
            _comms(),
        )
    assert "VOLT_PHASE_ROTATION" in sm.snap.active_alarms

    # Cooling — hidden
    sm.update(
        Reading(values=_values_for("cooling", output_status_7=0x2000 | 0x0200)),
        _comms(),
    )
    assert "VOLT_PHASE_ROTATION" not in sm.snap.active_alarms

    # Make raise_alarm return False the second time (idempotent DB)
    fake_db.raise_alarm.return_value = False
    fake_db.raise_alarm.reset_mock()

    # Back to running — debounce counter is already past threshold
    emitted = sm.update(
        Reading(values=_values_for("running", output_status_7=0x0200)),
        _comms(),
    )
    assert "VOLT_PHASE_ROTATION" in sm.snap.active_alarms
    # raise_alarm was called (idempotent re-entry) but no event fired
    fake_db.raise_alarm.assert_any_call(
        "VOLT_PHASE_ROTATION", "Check Voltage Phase Rotation", "warn", 0x0200
    )
    raised_events = [
        e for e in emitted if e.get("type") == "alarm" and e.get("code") == "VOLT_PHASE_ROTATION"
    ]
    assert raised_events == []


def test_debounce_counter_keeps_ticking_during_suppression(sm, fake_db):
    """Suppression hides the alarm but doesn't reset the debounce
    counter — once we leave the suppressed state, an alarm that's
    been set long enough fires immediately rather than restarting
    its debounce window.
    """
    # Bit set in cooling for 10 polls (above the 3-poll threshold for
    # phase rotation, but suppressed so nothing fires).
    for _ in range(10):
        sm.update(
            Reading(values=_values_for("cooling", output_status_7=0x2000 | 0x0200)),
            _comms(),
        )
    assert "VOLT_PHASE_ROTATION" not in sm.snap.active_alarms

    # Move to running — single poll should fire because the counter
    # is already well past the debounce threshold.
    sm.update(
        Reading(values=_values_for("running", output_status_7=0x0200)),
        _comms(),
    )
    assert "VOLT_PHASE_ROTATION" in sm.snap.active_alarms


def test_no_clear_event_for_alarm_that_never_raised(sm, fake_db):
    """A bit that flickers briefly in a suppressed state shouldn't
    leave any DB residue.
    """
    # Brief bit assertion in cooling — counter ticks but state
    # suppression prevents raise.
    for _ in range(5):
        sm.update(
            Reading(values=_values_for("cooling", output_status_7=0x2000 | 0x0200)),
            _comms(),
        )
    # Bit clears
    emitted = sm.update(
        Reading(values=_values_for("cooling", output_status_7=0x2000)),
        _comms(),
    )
    cleared = [e for e in emitted if e.get("type") == "alarm-cleared"]
    assert not any(e.get("code") == "VOLT_PHASE_ROTATION" for e in cleared)
    pr_clear_calls = [
        c for c in fake_db.clear_alarm.call_args_list
        if c.args and c.args[0] == "VOLT_PHASE_ROTATION"
    ]
    assert pr_clear_calls == []


# ─── Regmap reload resets debounce counters (audit H4) ───────────────────


def test_apply_regmap_clears_alarm_debounce_counters(sm, regmap):
    """Hot-reloading the register map must reset per-alarm debounce
    counters. Scenario: operator accumulates two polls of a 3-poll
    debounce, then hot-reloads YAML to fix a different rule. The
    NEXT poll after reload must start counting from zero, not from
    two — otherwise a one-poll bit flicker fires the alarm
    immediately, bypassing the debounce that exists specifically to
    suppress transients.
    """
    # Two polls with the phase-rotation bit set (debounce is 3).
    for _ in range(2):
        sm.update(
            Reading(values=_values_for("running", output_status_7=0x0200)),
            _comms(),
        )
    assert sm._alarm_poll_counts.get("VOLT_PHASE_ROTATION") == 2
    assert "VOLT_PHASE_ROTATION" not in sm.snap.active_alarms

    # Hot-reload (same YAML for simplicity — the point is the side
    # effect of apply_regmap, not the new content).
    sm.apply_regmap(regmap)
    assert sm._alarm_poll_counts == {}, (
        "apply_regmap must clear debounce counters so a transient bit "
        "doesn't fire instantly after reload"
    )

    # One more poll of the same bit. With reset counters this becomes
    # poll 1 of 3, so the alarm must not yet be in active_alarms.
    sm.update(
        Reading(values=_values_for("running", output_status_7=0x0200)),
        _comms(),
    )
    assert sm._alarm_poll_counts.get("VOLT_PHASE_ROTATION") == 1
    assert "VOLT_PHASE_ROTATION" not in sm.snap.active_alarms


def test_apply_regmap_preserves_raised_this_session(sm, regmap, fake_db):
    """``_raised_this_session`` tracks codes we've called raise_alarm()
    on so the bit-goes-low cleanup can call clear_alarm(). Wiping it
    on reload would orphan ``alarms_active`` rows raised before the
    reload — nothing would ever clear them when the bit eventually
    goes low.
    """
    # Raise an alarm (3 polls → past debounce → raised)
    for _ in range(3):
        sm.update(
            Reading(values=_values_for("running", output_status_7=0x0200)),
            _comms(),
        )
    assert "VOLT_PHASE_ROTATION" in sm._raised_this_session
    assert "VOLT_PHASE_ROTATION" in sm.snap.active_alarms

    # Hot-reload.
    sm.apply_regmap(regmap)
    # The session set must survive so a future bit-low transition can
    # still clear the DB row.
    assert "VOLT_PHASE_ROTATION" in sm._raised_this_session

    # Verify the clear actually fires when the bit goes low after reload.
    fake_db.clear_alarm.reset_mock()
    sm.update(
        Reading(values=_values_for("running", output_status_7=0)),
        _comms(),
    )
    clear_calls = [
        c.args[0] for c in fake_db.clear_alarm.call_args_list
        if c.args
    ]
    assert "VOLT_PHASE_ROTATION" in clear_calls, (
        "raised-before-reload alarm was not cleared when its bit went low"
    )
    assert "VOLT_PHASE_ROTATION" not in sm._raised_this_session
