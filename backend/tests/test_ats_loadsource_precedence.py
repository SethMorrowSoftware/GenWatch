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


# ─── Position-tainting faults drop authority (reachable-but-blind) ───────


async def test_input_fault_drops_authority_and_falls_back(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Hybrid 'reachable but blind': the Modbus TCP link is healthy but the
    ATS-Pi reports INPUT_FAULT (its serial sense to the Group 5 dropped) while
    still serving a STALE last-good position. GenWatch must drop authority and
    fall back to the H-100 derivation rather than display/act on the frozen
    value (audit CRITICAL)."""
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.set_position("generator")          # last-good before going blind
    ats_store.set_fault_bit(0x0001)              # ATS_PI_INPUT_FAULT
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert ats.snap.comms.state == "healthy"
    assert "ATS_PI_INPUT_FAULT" in ats.snap.fault_codes
    assert not ats.is_authoritative()

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    # H-100 fallback (engine stopped → utility), NOT the stale 'generator'.
    assert sm.snap.load_source == "utility"


async def test_calibration_fault_drops_authority(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """CALIBRATION (both position-sense signals asserted — physically
    impossible) also drops authority."""
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.set_position("generator")
    ats_store.set_fault_bit(0x0008)              # ATS_PI_CALIBRATION
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert not ats.is_authoritative()


async def test_output_fault_alone_keeps_position_authority(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """OUTPUT_FAULT is a command-relay read-back problem, not a position-sense
    problem — it must NOT drop position authority (the displayed loadSource is
    still trustworthy). It only blocks command *asserts* (covered in
    test_ats_control.test_output_fault_blocks_assert_but_allows_release)."""
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.set_position("generator")
    ats_store.set_fault_bit(0x0002)              # ATS_PI_OUTPUT_FAULT
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert "ATS_PI_OUTPUT_FAULT" in ats.snap.fault_codes
    assert ats.is_authoritative()

    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    sm.update(Reading(values=_h100_values("stopped")), _healthy())
    assert sm.snap.load_source == "generator"    # ATS position still wins


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


# ─── Load-source disagreement cross-check (audit C3) ─────────────────────


def _disagree_count(fake_db) -> int:
    """How many ATS_LOADSOURCE_DISAGREE raises did we see in the mock DB?"""
    return sum(
        1 for c in fake_db.raise_alarm.call_args_list
        if c.args and c.args[0] == "ATS_LOADSOURCE_DISAGREE"
    )


def _disagree_clear_count(fake_db) -> int:
    return sum(
        1 for c in fake_db.clear_alarm.call_args_list
        if c.args and c.args[0] == "ATS_LOADSOURCE_DISAGREE"
    )


async def _seed_authoritative_ats(
    ats_regmap, fake_db, bus, ats_store, position: str
):
    """Build an AtsService that's authoritative + reporting `position`."""
    ats = AtsService(ats_regmap, fake_db, bus)
    ats_store.set_position(position)
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert ats.is_authoritative()
    assert ats.snap.position == position
    return ats


async def test_disagree_ats_utility_h100_loaded_raises_after_debounce(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """ATS aux contact stuck on utility while the generator is actually
    carrying 150 kW — the classic stuck-contact failure that's invisible
    from either side alone. Must raise ATS_LOADSOURCE_DISAGREE after the
    debounce window."""
    ats = await _seed_authoritative_ats(ats_regmap, fake_db, bus, ats_store, "utility")
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)

    fake_db.raise_alarm.reset_mock()
    loaded = Reading(values=_h100_values("running", current=200, kw=150))

    # Polls 1 and 2: counting, no alarm yet
    sm.update(loaded, _healthy())
    sm.update(loaded, _healthy())
    assert _disagree_count(fake_db) == 0, (
        "alarm raised too early — debounce must require ≥3 consecutive polls"
    )
    assert "ATS_LOADSOURCE_DISAGREE" not in sm.snap.active_alarms

    # Poll 3: threshold reached, alarm fires
    emitted = sm.update(loaded, _healthy())
    assert _disagree_count(fake_db) == 1
    assert "ATS_LOADSOURCE_DISAGREE" in sm.snap.active_alarms
    bus_alarms = [
        e for e in emitted
        if e.get("type") == "alarm" and e.get("code") == "ATS_LOADSOURCE_DISAGREE"
    ]
    assert len(bus_alarms) == 1, "should emit a single bus alarm on raise"
    assert bus_alarms[0]["severity"] == "warn"
    assert "UTILITY" in bus_alarms[0]["desc"]
    assert "150" in bus_alarms[0]["desc"]  # kW shown in the description


async def test_disagree_persists_in_active_alarms_across_polls(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """After raise, subsequent polls (with the disagreement still active)
    must keep the code in active_alarms — the H-100 alarm pipeline
    overwrites that set every poll based on H-100 bits, so the derived
    code has to be re-layered."""
    ats = await _seed_authoritative_ats(ats_regmap, fake_db, bus, ats_store, "utility")
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    loaded = Reading(values=_h100_values("running", current=200, kw=150))

    for _ in range(3):
        sm.update(loaded, _healthy())
    assert "ATS_LOADSOURCE_DISAGREE" in sm.snap.active_alarms

    # 5 more polls — disagreement still active, alarm should not be
    # re-raised but membership in active_alarms must persist.
    fake_db.raise_alarm.reset_mock()
    for _ in range(5):
        sm.update(loaded, _healthy())
        assert "ATS_LOADSOURCE_DISAGREE" in sm.snap.active_alarms
    assert _disagree_count(fake_db) == 0, "alarm must be raise-once until cleared"


async def test_disagree_ats_generator_h100_zero_output_raises(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Reverse direction: ATS reports load on generator but the H-100
    isn't producing any output AND utility is also reported unavailable
    — so the building should have no power. Indicates a stuck aux
    contact (claiming generator when nothing is energized) or a broken
    H-100 output sensor. The utility-also-unavailable gate is required
    to avoid false-positives during the normal ASCO retransfer-delay
    window (see test_disagree_skips_gen_arm_when_utility_available)."""
    ats = await _seed_authoritative_ats(
        ats_regmap, fake_db, bus, ats_store, "generator"
    )
    ats_store.set_normal_available(False)
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)

    fake_db.raise_alarm.reset_mock()
    no_output = Reading(values=_h100_values("running", current=0.0, kw=0.0))
    for _ in range(3):
        sm.update(no_output, _healthy())
    assert _disagree_count(fake_db) == 1
    bus_msg = fake_db.raise_alarm.call_args.args[1]
    assert "GENERATOR" in bus_msg


async def test_disagree_skips_gen_arm_when_utility_available(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """ATS=generator + H-100 zero-output is normal during the ASCO
    retransfer-delay window: utility just restored, ATS still on
    generator waiting for its retransfer timer, building load briefly
    at zero. With normal_available=True we must NOT raise the
    disagreement alarm — otherwise it fires on every legitimate
    utility-restore cycle and operators learn to ignore it before the
    real failure mode (stuck-aux-during-actual-outage) ever fires."""
    ats = await _seed_authoritative_ats(
        ats_regmap, fake_db, bus, ats_store, "generator"
    )
    # Mock default already has normal_available=True but be explicit.
    ats_store.set_normal_available(True)
    await ats.on_poll("base", ats_store.as_reading("base"), _healthy())
    assert ats.snap.normal_available is True
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)

    fake_db.raise_alarm.reset_mock()
    no_output = Reading(values=_h100_values("running", current=0.0, kw=0.0))
    # Run well past the debounce window — would have raised the alarm
    # 7 times with the pre-fix logic.
    for _ in range(10):
        sm.update(no_output, _healthy())
    assert _disagree_count(fake_db) == 0
    assert "ATS_LOADSOURCE_DISAGREE" not in sm.snap.active_alarms


async def test_disagree_clears_when_signals_agree(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Once raised, the alarm must clear (and emit alarm-cleared) when
    the next poll observes agreement — either because the ATS contact
    came unstuck or the actual load shifted."""
    ats = await _seed_authoritative_ats(
        ats_regmap, fake_db, bus, ats_store, "utility"
    )
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    loaded = Reading(values=_h100_values("running", current=200, kw=150))

    for _ in range(3):
        sm.update(loaded, _healthy())
    assert "ATS_LOADSOURCE_DISAGREE" in sm.snap.active_alarms

    # ATS contact comes unstuck — now reports generator. Same H-100
    # reading is now consistent.
    fake_db.clear_alarm.reset_mock()
    ats_store.set_position("generator")
    await ats.on_poll("prime", ats_store.as_reading("prime"), _healthy())
    emitted = sm.update(loaded, _healthy())

    assert _disagree_clear_count(fake_db) == 1
    assert "ATS_LOADSOURCE_DISAGREE" not in sm.snap.active_alarms
    cleared = [
        e for e in emitted
        if e.get("type") == "alarm-cleared" and e.get("code") == "ATS_LOADSOURCE_DISAGREE"
    ]
    assert len(cleared) == 1


async def test_disagree_does_not_raise_for_transient_mismatch(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Two polls of disagreement followed by an agreeing poll resets the
    debounce counter — normal transfer windows can briefly disagree by
    design and must not trigger a spurious alarm."""
    ats = await _seed_authoritative_ats(
        ats_regmap, fake_db, bus, ats_store, "utility"
    )
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    loaded = Reading(values=_h100_values("running", current=200, kw=150))
    no_load = Reading(values=_h100_values("running", current=0.0, kw=0.0))

    fake_db.raise_alarm.reset_mock()
    # Two disagreement polls
    sm.update(loaded, _healthy())
    sm.update(loaded, _healthy())
    # Then an agreement (ATS=utility + H-100 no output)
    sm.update(no_load, _healthy())
    # Now back to disagreement, but counter has been reset
    sm.update(loaded, _healthy())
    sm.update(loaded, _healthy())
    assert _disagree_count(fake_db) == 0, (
        "agreement poll between disagreements must reset the debounce counter"
    )
    # Third consecutive disagreement after reset triggers the alarm
    sm.update(loaded, _healthy())
    assert _disagree_count(fake_db) == 1


async def test_disagree_clears_when_ats_loses_authority(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Comms dropping mid-disagreement should clear the alarm — we no
    longer have the signal to evaluate the condition, and the
    loadSource has fallen back to H-100 inference anyway."""
    ats = await _seed_authoritative_ats(
        ats_regmap, fake_db, bus, ats_store, "utility"
    )
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    loaded = Reading(values=_h100_values("running", current=200, kw=150))
    for _ in range(3):
        sm.update(loaded, _healthy())
    assert "ATS_LOADSOURCE_DISAGREE" in sm.snap.active_alarms

    # ATS comms drop — authority lost
    await ats.on_poll("prime", ats_store.as_reading("prime"), _lost())
    assert not ats.is_authoritative()

    fake_db.clear_alarm.reset_mock()
    sm.update(loaded, _healthy())
    assert _disagree_clear_count(fake_db) == 1
    assert "ATS_LOADSOURCE_DISAGREE" not in sm.snap.active_alarms


async def test_disagree_skips_when_engine_not_running(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """Engine stopped → no comparison is meaningful (the H-100 output
    must be zero by physics). Alarm must not fire even if the ATS
    reports something nonsensical."""
    ats = await _seed_authoritative_ats(
        ats_regmap, fake_db, bus, ats_store, "generator"
    )
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    stopped = Reading(values=_h100_values("stopped"))

    fake_db.raise_alarm.reset_mock()
    for _ in range(5):
        sm.update(stopped, _healthy())
    assert _disagree_count(fake_db) == 0


async def test_disagree_skips_when_ats_transferring(
    h100_regmap, ats_regmap, fake_db, bus, ats_store,
):
    """ATS reports `transferring` — a sub-2-second intermediate state
    where the H-100 might still be carrying load even though the contact
    is mid-switch. Must not raise an alarm during this window."""
    ats = await _seed_authoritative_ats(
        ats_regmap, fake_db, bus, ats_store, "transferring"
    )
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=ats)
    loaded = Reading(values=_h100_values("running", current=200, kw=150))

    fake_db.raise_alarm.reset_mock()
    for _ in range(5):
        sm.update(loaded, _healthy())
    assert _disagree_count(fake_db) == 0


async def test_disagree_skips_when_no_ats_service(
    h100_regmap, fake_db, bus,
):
    """Sites without an ATS-Pi don't have the comparison surface — the
    check must be a complete no-op (backward-compat with single-link
    H-100-only deployments)."""
    sm = StateMachine(h100_regmap, fake_db, bus, ats_service=None)
    loaded = Reading(values=_h100_values("running", current=200, kw=150))
    fake_db.raise_alarm.reset_mock()
    for _ in range(5):
        sm.update(loaded, _healthy())
    assert _disagree_count(fake_db) == 0
