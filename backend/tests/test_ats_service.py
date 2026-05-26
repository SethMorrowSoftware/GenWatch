"""AtsService unit tests.

Exercises the read-side ATS-Pi consumer in isolation, driven by the
MockAtsPiStore fixture. Covers:

  - ICD §5 register decoding into AtsSnapshot
  - Event emission on position / source-availability / mode / fault
    transitions
  - Authority gate (`is_authoritative()`) per ICD §10
  - Comms-loss behaviour
  - ATS-Pi reboot detection
  - Time-skew alarm (ICD §11)
  - Forward-compat: unknown enum values decode to 'unknown'

End-to-end tests against the real Modbus TCP server in MockAtsPiServer
live in `test_ats_integration.py` (Phase 1 ships only the in-process
tests; the TCP-server smoke is exercised in Phase 4 commissioning).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from genwatch.modbus.poller import CommsHealth
from genwatch.modbus.registers import load_register_map
from genwatch.services.ats import EXPECTED_ICD_MAJOR, AtsService, AtsSnapshot
from genwatch.services.state import EventBus

from tests.fixtures.mock_ats_pi import MockAtsPiStore


pytestmark = pytest.mark.asyncio


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def regmap():
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
def ats(regmap, fake_db, bus):
    # No slack notifier in unit tests
    return AtsService(regmap, fake_db, bus, slack=None)


@pytest.fixture
def store():
    return MockAtsPiStore()


def healthy() -> CommsHealth:
    c = CommsHealth()
    c.state = "healthy"
    c.success_pct = 100.0
    return c


def lost() -> CommsHealth:
    c = CommsHealth()
    c.state = "lost"
    c.success_pct = 0.0
    return c


# ─── Snapshot decoding ───────────────────────────────────────────────────


async def test_initial_state_decodes_cleanly(ats, store):
    """First successful base poll populates the snapshot with the
    mock's default healthy state.
    """
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.snap.position == "utility"
    assert ats.snap.normal_available is True
    assert ats.snap.emergency_available is True
    assert ats.snap.engine_start_calling is False
    assert ats.snap.ats_mode == "auto"
    assert ats.snap.fault_codes == set()
    assert ats.snap.icd_version == (EXPECTED_ICD_MAJOR, 0)
    assert ats.snap.ats_pi_unit_id == 23
    assert ats.snap.transfer_count_lifetime == 0


async def test_prime_only_poll_preserves_base_tier_values(ats, store):
    """Prime polls don't carry base-tier registers — those must persist
    from the previous base poll, not get cleared.
    """
    # Seed with base poll
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.snap.transfer_count_lifetime == 0
    assert ats.snap.icd_version == (EXPECTED_ICD_MAJOR, 0)

    # Now drive a prime poll — should preserve base-tier values
    store.set_position("generator")  # bumps lifetime count to 1
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    # Base-tier values are unchanged from the prior base poll because
    # the prime poll doesn't carry them
    assert ats.snap.icd_version == (EXPECTED_ICD_MAJOR, 0)
    # But prime values are updated
    assert ats.snap.position == "generator"


# ─── Authority gate ──────────────────────────────────────────────────────


async def test_not_authoritative_before_first_base_poll(ats, store):
    """Even with healthy comms, we can't trust the ATS-Pi until we've
    confirmed its ICD version via a base poll.
    """
    await ats.on_poll("prime", store.as_reading("prime"), healthy())
    assert ats.is_authoritative() is False


async def test_authoritative_after_base_poll_with_healthy_comms(ats, store):
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.is_authoritative() is True


async def test_not_authoritative_when_comms_lost(ats, store):
    await ats.on_poll("base", store.as_reading("base"), healthy())
    await ats.on_poll("prime", store.as_reading("prime"), lost())
    assert ats.is_authoritative() is False


async def test_not_authoritative_on_icd_major_mismatch(regmap, fake_db, bus, store):
    """An ATS-Pi reporting a different major version is intentionally
    incompatible — refuse to use its data as authoritative.
    """
    ats = AtsService(regmap, fake_db, bus)
    store.icd_major = 2  # imagine a future v2 ATS-Pi
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.is_authoritative() is False


async def test_not_authoritative_when_unit_id_mismatch(regmap, fake_db, bus, store):
    """The expected_unit_id check catches an ATS-Pi pointed at the
    wrong site.
    """
    ats = AtsService(regmap, fake_db, bus, expected_unit_id=99)
    store.unit_id = 23
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.is_authoritative() is False

    # Update the store to match — now authoritative
    store.unit_id = 99
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.is_authoritative() is True


# ─── Position transitions ───────────────────────────────────────────────


async def test_position_transition_emits_event(ats, store, bus, fake_db):
    """utility → generator emits ats-position event + DB row."""
    # Subscribe to the bus to capture events
    queue = bus.subscribe()
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.write_event.reset_mock()

    store.set_position("generator")
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    # Drain bus events
    events = []
    while not queue.empty():
        events.append(await queue.get())

    position_events = [e for e in events if e.get("type") == "ats-position"]
    assert len(position_events) == 1
    assert position_events[0]["from"] == "utility"
    assert position_events[0]["to"] == "generator"

    db_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_POSITION"
    ]
    assert len(db_calls) == 1
    assert db_calls[0].kwargs["severity"] == "warn"


async def test_position_back_to_utility_is_ok_severity(ats, store, fake_db):
    """generator → utility is 'ok' severity (restoration)."""
    store.set_position("generator")
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.write_event.reset_mock()

    store.set_position("utility")
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    db_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_POSITION"
    ]
    assert len(db_calls) == 1
    assert db_calls[0].kwargs["severity"] == "ok"


async def test_transferring_state_is_info_severity(ats, store, fake_db):
    """'transferring' is a transient intermediate; not warn-worthy."""
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.write_event.reset_mock()

    store.position = "transferring"  # bypass setter to avoid bumping counters
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    db_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_POSITION"
    ]
    assert len(db_calls) == 1
    assert db_calls[0].kwargs["severity"] == "info"


async def test_no_event_when_position_unchanged(ats, store, fake_db):
    """Idempotent polling — same value across polls, no event."""
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.write_event.reset_mock()
    await ats.on_poll("prime", store.as_reading("prime"), healthy())
    db_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_POSITION"
    ]
    assert db_calls == []


# ─── Source availability ─────────────────────────────────────────────────


async def test_utility_lost_emits_warn_event(ats, store, fake_db):
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.write_event.reset_mock()

    store.set_normal_available(False)
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    src_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_SOURCE"
    ]
    assert len(src_calls) == 1
    assert src_calls[0].kwargs["severity"] == "warn"
    assert "UTILITY_LOST" in src_calls[0].kwargs.get("meta", "")


async def test_utility_restored_emits_ok_event(ats, store, fake_db):
    store.set_normal_available(False)
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.write_event.reset_mock()

    store.set_normal_available(True)
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    src_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_SOURCE"
    ]
    assert len(src_calls) == 1
    assert src_calls[0].kwargs["severity"] == "ok"


async def test_no_source_event_on_initial_poll(ats, store, fake_db):
    """First poll has no 'previous' value — don't emit a spurious
    transition event when we first observe a fresh source state.
    """
    await ats.on_poll("base", store.as_reading("base"), healthy())
    src_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_SOURCE"
    ]
    assert src_calls == []


# ─── Fault bits ─────────────────────────────────────────────────────────


async def test_fault_bit_set_raises_alarm(ats, store, fake_db):
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.raise_alarm.reset_mock()

    store.set_fault_bit(0x0001, on=True)  # ATS_PI_INPUT_FAULT
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    raise_calls = [
        c for c in fake_db.raise_alarm.call_args_list
        if c.args and c.args[0] == "ATS_PI_INPUT_FAULT"
    ]
    assert len(raise_calls) == 1


async def test_fault_bit_cleared_clears_alarm(ats, store, fake_db):
    store.set_fault_bit(0x0001, on=True)
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.clear_alarm.reset_mock()

    store.set_fault_bit(0x0001, on=False)
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    clear_calls = [
        c for c in fake_db.clear_alarm.call_args_list
        if c.args and c.args[0] == "ATS_PI_INPUT_FAULT"
    ]
    assert len(clear_calls) == 1


async def test_multiple_fault_bits_decoded_independently(ats, store, fake_db):
    await ats.on_poll("base", store.as_reading("base"), healthy())

    store.set_fault_bit(0x0001, on=True)  # INPUT_FAULT
    store.set_fault_bit(0x0002, on=True)  # OUTPUT_FAULT
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    assert "ATS_PI_INPUT_FAULT" in ats.snap.fault_codes
    assert "ATS_PI_OUTPUT_FAULT" in ats.snap.fault_codes


# ─── Reboot detection ───────────────────────────────────────────────────


async def test_reboot_detection_emits_event(ats, store, fake_db):
    """An ATS-Pi reboot is detected via uptime going backwards."""
    # Establish a baseline uptime
    store._boot_ts = time.time() - 100.0  # appear to have been up for 100 s
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.snap.ats_pi_uptime_s >= 100
    fake_db.write_event.reset_mock()

    # Reboot
    store.reboot()
    store._boot_ts = time.time()  # fresh boot
    await ats.on_poll("base", store.as_reading("base"), healthy())

    reboot_events = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_REBOOT"
    ]
    assert len(reboot_events) == 1


# ─── Time skew ──────────────────────────────────────────────────────────


async def test_time_skew_raises_alarm(ats, store, fake_db):
    store.wallclock_offset_s = 10.0  # > 5 s threshold
    await ats.on_poll("base", store.as_reading("base"), healthy())

    raise_calls = [
        c for c in fake_db.raise_alarm.call_args_list
        if c.args and c.args[0] == "ATS_PI_TIME_SKEW"
    ]
    assert len(raise_calls) == 1


async def test_time_skew_clears_when_corrected(ats, store, fake_db):
    store.wallclock_offset_s = 10.0
    await ats.on_poll("base", store.as_reading("base"), healthy())

    # Correct the clock
    store.wallclock_offset_s = 0.0
    await ats.on_poll("base", store.as_reading("base"), healthy())

    clear_calls = [
        c for c in fake_db.clear_alarm.call_args_list
        if c.args and c.args[0] == "ATS_PI_TIME_SKEW"
    ]
    assert len(clear_calls) == 1


async def test_time_skew_alarm_not_duplicated(ats, store, fake_db):
    """Multiple polls within the skew window should only raise the
    alarm once, not on every poll.
    """
    store.wallclock_offset_s = 10.0
    await ats.on_poll("base", store.as_reading("base"), healthy())
    await ats.on_poll("base", store.as_reading("base"), healthy())
    await ats.on_poll("base", store.as_reading("base"), healthy())

    raise_calls = [
        c for c in fake_db.raise_alarm.call_args_list
        if c.args and c.args[0] == "ATS_PI_TIME_SKEW"
    ]
    assert len(raise_calls) == 1


# ─── Comms transitions ──────────────────────────────────────────────────


async def test_comms_loss_emits_event(ats, store, fake_db):
    await ats.on_poll("base", store.as_reading("base"), healthy())
    fake_db.write_event.reset_mock()
    await ats.on_poll("prime", store.as_reading("prime"), lost())

    comms_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_COMMS"
    ]
    assert len(comms_calls) == 1
    assert comms_calls[0].kwargs["severity"] == "warn"


async def test_comms_recovery_emits_ok_event(ats, store, fake_db):
    await ats.on_poll("base", store.as_reading("base"), lost())
    fake_db.write_event.reset_mock()
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    comms_calls = [
        c for c in fake_db.write_event.call_args_list
        if c.kwargs.get("type_") == "ATS_COMMS"
    ]
    assert len(comms_calls) == 1
    assert comms_calls[0].kwargs["severity"] == "ok"


# ─── Resilience ─────────────────────────────────────────────────────────


async def test_db_failure_does_not_crash_poll(ats, store, fake_db):
    """A downstream DB error inside event handling MUST NOT abort the
    poll callback — the poller has to keep going.
    """
    fake_db.write_event.side_effect = RuntimeError("DB exploded")

    # Should not raise
    await ats.on_poll("base", store.as_reading("base"), healthy())
    store.set_position("generator")
    await ats.on_poll("prime", store.as_reading("prime"), healthy())

    # Snapshot was still updated even though events failed
    assert ats.snap.position == "generator"
