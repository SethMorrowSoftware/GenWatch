"""StateMachine loadSource precedence with the ATS service attached.

When the ATS-Pi link is healthy, its `position` reading is the
authoritative loadSource (ICD §10). When degraded, GenWatch falls back
to the existing H-100-electrical derivation. These tests pin down the
exact precedence behaviour without breaking the existing fallback
suite in test_state_machine.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from genwatch.modbus.poller import CommsHealth, Reading
from genwatch.modbus.registers import load_register_map
from genwatch.services.ats import AtsService
from genwatch.services.state import EventBus, StateMachine

from tests.fixtures.mock_ats_pi import MockAtsPiStore

pytestmark = pytest.mark.asyncio


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def h100_regmap():
    return load_register_map(
        Path(__file__).parent.parent / "genwatch/registers/h100.yaml"
    )


@pytest.fixture
def ats_regmap():
    return load_register_map(
        Path(__file__).parent.parent / "genwatch/registers/ats_pi.yaml"
    )


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.raise_alarm.return_value = True
    db.clear_alarm.return_value = True
    return db


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def ats_store():
    return MockAtsPiStore()


def _h100_values(state: str, current: float = 0, kw: float = 0) -> dict:
    """Build a Reading-values dict that decodes to a given H-100 engine state."""
    s1 = 0
    s7 = 0
    if state == "running":
        s1 |= 0x2000
    elif state == "stopped":
        s1 |= 0x0100
    return {
        "output_status_1": s1,
        "output_status_7": s7,
        "avg_current": current,
        "total_kw": kw,
    }


def _healthy() -> CommsHealth:
    c = CommsHealth()
    c.state = "healthy"
    c.success_pct = 100.0
    return c


def _lost() -> CommsHealth:
    c = CommsHealth()
    c.state = "lost"
    c.success_pct = 0.0
    return c


# ─── Precedence: ATS-Pi authoritative wins ───────────────────────────────


async def test_ats_position_overrides_h100_derivation(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """The H-100 thinks load is on utility (engine stopped). But the
    ATS-Pi reports `position=generator`. ATS-Pi wins.
    """
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.set_position("generator")
    # Authoritative: complete a base poll with healthy comms
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert ats.is_authoritative()

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    # H-100 reading says engine stopped (would normally → utility)
    sm.update(Reading(values=_h100_values("stopped")), _healthy())

    assert sm.snap.load_source == "generator"


async def test_h100_loaded_running_overridden_by_ats_utility(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Reverse case: H-100 says engine running with full load (would
    derive 'generator'), but ATS-Pi reports load on utility — maybe
    the ATS is in test mode or has been manually thrown. ATS-Pi wins.
    """
    ats = AtsService(ats_regmap, fake_db, bus)
    # ATS-Pi reports utility (default)
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("running", current=200, kw=150)), _healthy())

    # ATS-Pi authoritative wins, even though H-100 telemetry says generator
    assert sm.snap.load_source == "utility"


# ─── Precedence: fallback to H-100 ───────────────────────────────────────


async def test_falls_back_when_ats_comms_lost(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """ATS-Pi previously healthy, now comms lost — fall back to H-100."""
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.set_position("generator")
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert ats.is_authoritative()

    # Now comms go lost — authority drops
    await ats.on_poll("prime", ats_store.as_reading("prime"), _lost())
    assert not ats.is_authoritative()

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("running", current=200, kw=150)), _healthy())

    # Fallback: H-100 telemetry says loaded → generator
    assert sm.snap.load_source == "generator"

    # Verify the fallback path was taken: stop the engine, ATS still
    # reporting generator (stale), but we should now derive utility
    # from H-100.
    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    assert sm.snap.load_source == "utility"


async def test_falls_back_when_ats_not_yet_done_base_poll(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Boot-time: ATS-Pi prime poll has happened but base hasn't —
    can't trust the position yet. Use H-100 fallback.
    """
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.set_position("generator")
    await ats.on_poll("prime", ats_store.as_reading("prime"), _healthy())
    assert not ats.is_authoritative()

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("running", current=200, kw=150)), _healthy())
    assert sm.snap.load_source == "generator"  # via H-100 derivation


async def test_falls_back_when_ats_position_unknown(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Even when authoritative, an explicit 'unknown' from the ATS-Pi
    triggers fallback — better to use our inference than to display
    'unknown' to the operator.
    """
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.position = "unknown"  # explicitly unknown
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert ats.is_authoritative()  # comms healthy + ICD checked

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    assert sm.snap.load_source == "utility"  # H-100 says utility (engine stopped)


async def test_no_ats_service_uses_h100_derivation(
    h100_regmap, fake_db, bus,
):
    """Sites without ATS-Pi (the default config) — behaviour identical
    to pre-ATS-Pi GenWatch. This is the key backwards-compat test.
    """
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=None)
    sm.update(Reading(values=_h100_values("running", current=200, kw=150)), _healthy())
    assert sm.snap.load_source == "generator"

    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    assert sm.snap.load_source == "utility"


# ─── ATS-Pi 'transferring' state ────────────────────────────────────────


async def test_transferring_state_propagates_to_load_source(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """When the ATS is mid-transfer, the operator sees 'transferring'
    rather than a misleading utility/generator value.
    """
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.position = "transferring"
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("running")), _healthy())
    assert sm.snap.load_source == "transferring"


# ─── ICD version mismatch ───────────────────────────────────────────────


async def test_icd_major_mismatch_falls_back(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """A future-major ATS-Pi can't be authoritative."""
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.icd_major = 99
    ats_store.set_position("generator")
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert not ats.is_authoritative()

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    # H-100 fallback wins
    assert sm.snap.load_source == "utility"


# ─── Event-emission de-duplication ──────────────────────────────────────


async def test_no_duplicate_load_source_event_when_ats_authoritative(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """When the ATS-Pi is the authoritative source, AtsService emits the
    `ats-position` event for a transition; StateMachine must NOT also
    emit a `load-source` event for the same physical change. Without
    this, the events feed would show two rows per real-world event.
    """
    ats = AtsService(ats_regmap, fake_db, bus)
    # Seed: ATS-Pi healthy, position=utility, authoritative
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert ats.is_authoritative()

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    assert sm.snap.load_source == "utility"

    # Now ATS-Pi observes a transition to generator. This emits
    # ats-position (and writes ATS_POSITION).
    fake_db.write_event.reset_mock()
    ats_store.set_position("generator")
    await ats.on_poll("prime", ats_store.as_reading("prime"), _healthy())

    # AtsService should have written its ATS_POSITION row
    ats_position_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_POSITION"
    ]
    assert len(ats_position_calls) == 1

    # Now the next H-100 poll comes in. StateMachine re-derives
    # load_source via precedence → generator. The snapshot updates but
    # NO load-source event should fire because ats is authoritative.
    fake_db.write_event.reset_mock()
    emitted = sm.update(
        Reading(values=_h100_values("running", current=200, kw=150)),
        _healthy(),
    )

    # snapshot updated
    assert sm.snap.load_source == "generator"
    # but no LOAD_SOURCE DB row written
    load_source_db_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "LOAD_SOURCE"
    ]
    assert load_source_db_calls == []
    # and no load-source bus event emitted
    bus_load_source = [e for e in emitted if e.get("type") == "load-source"]
    assert bus_load_source == []


async def test_load_source_event_still_emitted_when_ats_not_authoritative(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """The opposite case: when ATS-Pi is the fallback (degraded or absent),
    StateMachine must emit load-source events as before — otherwise sites
    relying purely on H-100 telemetry would lose their event log.
    """
    # No ats_service at all — pure H-100 fallback
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=None)
    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    fake_db.write_event.reset_mock()

    # Now H-100 detects a transition to loaded running
    emitted = sm.update(
        Reading(values=_h100_values("running", current=200, kw=150)),
        _healthy(),
    )
    assert sm.snap.load_source == "generator"

    load_source_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "LOAD_SOURCE"
    ]
    assert len(load_source_calls) == 1
    bus_load_source = [e for e in emitted if e.get("type") == "load-source"]
    assert len(bus_load_source) == 1
