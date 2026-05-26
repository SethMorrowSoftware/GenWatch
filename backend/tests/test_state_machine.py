"""Load-source derivation + transition emission tests.

Covers services/state.py's load_source classifier across the full
engine-state matrix, hysteresis behaviour at the load-detection
boundary, robustness to missing electrical telemetry, and the
event-emission contract on transitions.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from genwatch.modbus.poller import CommsHealth, Reading
from genwatch.modbus.registers import load_register_map
from genwatch.services.state import (
    LOAD_OFF_CURRENT_THRESHOLD,
    LOAD_OFF_KW_THRESHOLD,
    LOAD_ON_CURRENT_THRESHOLD,
    LOAD_ON_KW_THRESHOLD,
    EventBus,
    StateMachine,
)


@pytest.fixture
def regmap():
    return load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")


@pytest.fixture
def fake_db():
    """Mock Database that records write_event/raise_alarm/clear_alarm calls."""
    db = MagicMock()
    db.raise_alarm.return_value = True
    db.clear_alarm.return_value = True
    return db


@pytest.fixture
def sm(regmap, fake_db):
    return StateMachine(regmap, fake_db, EventBus())


def _values_for(state: str, current: float = 0, kw: float = 0) -> dict:
    """Build a `reading.values` dict that decodes to the requested
    engine state when fed through the H-100 register map's bitfield rules.
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
    return {
        "output_status_1": s1,
        "output_status_7": s7,
        "avg_current": current,
        "total_kw": kw,
    }


def _comms() -> CommsHealth:
    c = CommsHealth()
    c.state = "healthy"
    return c


# ─── Engine-state-only cases ─────────────────────────────────────────────


def test_stopped_engine_is_always_utility(sm):
    """A stopped engine cannot be supplying load. Period."""
    sm.update(Reading(values=_values_for("stopped")), _comms())
    assert sm.snap.load_source == "utility"


def test_cranking_engine_is_utility(sm):
    """Mid-crank, the gen hasn't come up — load stays on utility."""
    sm.update(Reading(values=_values_for("cranking")), _comms())
    assert sm.snap.load_source == "utility"


def test_cooling_engine_is_utility(sm):
    """Cooling means the ATS already retransferred — load is back on utility."""
    sm.update(Reading(values=_values_for("cooling")), _comms())
    assert sm.snap.load_source == "utility"


def test_exercising_engine_is_utility_even_when_current_reads_high(sm):
    """Quiet-test is by design unloaded. If current/kW readings somehow
    show load during exercise, engine state still wins — the test is a
    no-load condition by H-100 design and we trust the controller.
    """
    sm.update(
        Reading(values=_values_for("exercising", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "utility"


def test_unknown_engine_state_preserves_prior_load_source(sm):
    """A transient 'unknown' (e.g. between prime polls) shouldn't
    flicker the load badge.
    """
    # Seed to 'generator'
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"
    # Now go to unknown — should NOT reset to utility
    sm.update(Reading(values={"output_status_1": 0, "output_status_7": 0}), _comms())
    assert sm.snap.load_source == "generator"


# ─── Running + load detection ────────────────────────────────────────────


def test_running_with_clear_load_reports_generator(sm):
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"


def test_running_unloaded_reports_utility(sm):
    """Engine running but ATS not yet transferred — pre-transfer warm-up."""
    sm.update(
        Reading(values=_values_for("running", current=0, kw=0)),
        _comms(),
    )
    assert sm.snap.load_source == "utility"


def test_alarm_with_load_reports_generator(sm):
    """An alarm during loaded operation (warning-level) still has the
    load on the generator until the controller shuts down.
    """
    sm.update(
        Reading(values=_values_for("alarm", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"


def test_alarm_after_shutdown_reports_utility(sm):
    """Shutdown alarm → no current flowing → utility."""
    sm.update(
        Reading(values=_values_for("alarm", current=0, kw=0)),
        _comms(),
    )
    assert sm.snap.load_source == "utility"


# ─── Threshold + hysteresis behaviour ────────────────────────────────────


def test_requires_both_sensors_above_on_thresholds(sm):
    """Single-sensor false positive shouldn't trip a transfer event."""
    # Only kW above threshold (current below) — broken CT reading 0 while
    # somehow kW reads non-zero. Stay on utility.
    sm.update(
        Reading(values=_values_for("running", current=0, kw=10)),
        _comms(),
    )
    assert sm.snap.load_source == "utility"


def test_requires_both_sensors_above_on_thresholds_other_side(sm):
    """Single-sensor false positive on the other side stays on utility."""
    # Only current above (kW reads 0) — kW computation broken.
    sm.update(
        Reading(values=_values_for("running", current=200, kw=0)),
        _comms(),
    )
    assert sm.snap.load_source == "utility"


def test_hysteresis_no_flicker_between_on_and_off_thresholds(sm):
    """A load reading sitting between OFF and ON thresholds doesn't
    cause flicker — it just preserves whichever side we were on.
    """
    # Get to generator
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"
    # Now drop just below ON but above OFF — hysteresis keeps us on generator
    boundary_curr = (LOAD_ON_CURRENT_THRESHOLD + LOAD_OFF_CURRENT_THRESHOLD) / 2
    boundary_kw = (LOAD_ON_KW_THRESHOLD + LOAD_OFF_KW_THRESHOLD) / 2
    sm.update(
        Reading(values=_values_for("running", current=boundary_curr, kw=boundary_kw)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"


def test_retransfer_requires_both_sensors_below_off(sm):
    """One stuck-high sensor shouldn't falsely declare a retransfer."""
    # Get to generator
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"
    # kW drops to 0 but current still reads 200 (broken CT or genuine
    # capacitive load) — stay on generator until both agree.
    sm.update(
        Reading(values=_values_for("running", current=200, kw=0)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"


def test_clean_retransfer_when_both_sensors_drop(sm):
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"
    sm.update(
        Reading(values=_values_for("running", current=0, kw=0)),
        _comms(),
    )
    assert sm.snap.load_source == "utility"


# ─── Missing-readings robustness ─────────────────────────────────────────


def test_missing_electrical_readings_at_boot_defaults_to_utility(sm):
    """Before the first base-tier poll, current/kW aren't in the
    values dict. With engine state 'running' and no electrical proof,
    we default to utility — assuming 'generator' would emit a spurious
    transfer event on the next base poll showing no load.
    """
    sm.update(
        Reading(values={
            "output_status_1": 0x2000,  # Generator Running
            "output_status_7": 0,
            # avg_current and total_kw absent
        }),
        _comms(),
    )
    assert sm.snap.load_source == "utility"


def test_missing_electrical_readings_preserves_generator(sm):
    """If we were already on 'generator' and a poll drops the electrical
    keys, hold the previous classification rather than flipping.
    """
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source == "generator"
    # Subsequent poll: state bits only, no electrical
    sm.update(
        Reading(values={"output_status_1": 0x2000, "output_status_7": 0}),
        _comms(),
    )
    assert sm.snap.load_source == "generator"


# ─── Event emission contract ─────────────────────────────────────────────


def test_emits_bus_event_on_transition_to_generator(sm, fake_db):
    """utility → generator emits a 'load-source' event on the bus and
    writes a warn-severity DB event row.
    """
    sm.update(Reading(values=_values_for("stopped")), _comms())  # seed utility
    fake_db.write_event.reset_mock()

    emitted = sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    types = [e["type"] for e in emitted]
    assert "load-source" in types
    transition = next(e for e in emitted if e["type"] == "load-source")
    assert transition["from"] == "utility"
    assert transition["to"] == "generator"

    # DB event row — severity 'warn' because going to generator implies
    # something happened on the utility side (or an operator forced it).
    load_source_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "LOAD_SOURCE"
    ]
    assert len(load_source_calls) == 1
    assert load_source_calls[0].kwargs["severity"] == "warn"


def test_emits_bus_event_on_transition_to_utility(sm, fake_db):
    """generator → utility emits and writes an 'ok' severity event."""
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )  # seed generator
    fake_db.write_event.reset_mock()

    emitted = sm.update(
        Reading(values=_values_for("running", current=0, kw=0)),
        _comms(),
    )
    transition = next(e for e in emitted if e["type"] == "load-source")
    assert transition["from"] == "generator"
    assert transition["to"] == "utility"

    load_source_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "LOAD_SOURCE"
    ]
    assert len(load_source_calls) == 1
    assert load_source_calls[0].kwargs["severity"] == "ok"


def test_boot_unknown_to_utility_does_not_write_db_event(sm, fake_db):
    """Initial unknown → utility on first poll is just state firming,
    not an operational event. The bus still emits (the UI's load badge
    flips from "—" to "UTILITY") but no DB row is written.
    """
    fake_db.write_event.reset_mock()
    emitted = sm.update(Reading(values=_values_for("stopped")), _comms())

    # Bus event present (for UI immediacy)
    assert any(e["type"] == "load-source" for e in emitted)
    # But no LOAD_SOURCE event was written to the DB
    load_source_db_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "LOAD_SOURCE"
    ]
    assert load_source_db_calls == []


def test_no_event_when_no_transition(sm, fake_db):
    """Repeated polls with the same load classification don't spam."""
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    fake_db.write_event.reset_mock()

    emitted = sm.update(
        Reading(values=_values_for("running", current=210, kw=160)),
        _comms(),
    )
    assert not any(e["type"] == "load-source" for e in emitted)
    load_source_db_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "LOAD_SOURCE"
    ]
    assert load_source_db_calls == []


def test_load_source_started_at_updates_on_transition(sm):
    """time_in_load_source resets when the source changes."""
    import time as _time
    sm.update(Reading(values=_values_for("stopped")), _comms())
    t0 = sm.snap.load_source_started_at
    _time.sleep(0.01)
    sm.update(
        Reading(values=_values_for("running", current=200, kw=150)),
        _comms(),
    )
    assert sm.snap.load_source_started_at > t0
