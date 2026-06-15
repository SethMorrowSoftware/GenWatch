"""AtsControlService unit tests — ATS-Pi command write side (Phase 3).

Drives the service directly with a real (in-process) AtsService made
authoritative via MockAtsPiStore, a real ControlService for the shared
confirm-token store, and a fake ATS Modbus client that records writes.

Covers the plan §7.1 / ICD §6 acceptance criteria:
  - each command writes the expected ATS register/value
  - confirm-token flow (valid / invalid / single-use)
  - role gating (force-transfer is admin-only)
  - force-transfer healthy-utility override guard
  - comms-loss / non-authoritative link disables commands
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from genwatch.modbus.poller import CommsHealth
from genwatch.modbus.registers import load_register_map
from genwatch.services.ats import AtsService
from genwatch.services.ats_control import AtsControlService
from genwatch.services.control import ControlError, ControlService
from genwatch.services.state import EventBus

from tests.fixtures.mock_ats_pi import MockAtsPiStore

pytestmark = pytest.mark.asyncio


# ─── Helpers ─────────────────────────────────────────────────────────────


def healthy() -> CommsHealth:
    c = CommsHealth()
    c.state = "healthy"
    return c


def lost() -> CommsHealth:
    c = CommsHealth()
    c.state = "lost"
    return c


class FakeClient:
    """Records writes; returns a configurable ModbusResult-shaped object."""

    def __init__(self) -> None:
        self.writes: list[tuple] = []
        self.ok = True
        self.error: str | None = None

    async def write(self, addr, value=None, *, fc=6, values=None):
        self.writes.append((addr, value, fc, values))
        return SimpleNamespace(ok=self.ok, error=self.error)


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
async def authoritative_ats(regmap, fake_db, bus):
    """An AtsService driven to an authoritative steady state."""
    ats = AtsService(regmap, fake_db, bus, slack=None)
    store = MockAtsPiStore()  # defaults: utility, both sources avail, ICD 1.0, unit 23
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.is_authoritative() is True
    return ats, store


@pytest.fixture
def control(regmap, fake_db):
    # ControlService only needs db + lock for the token store here.
    return ControlService(regmap, FakeClient(), fake_db, MagicMock(), slack=None)


@pytest.fixture
def ats_control(regmap, fake_db, authoritative_ats, control):
    ats, _store = authoritative_ats
    client = FakeClient()
    svc = AtsControlService(regmap, client, fake_db, ats, control, slack=None)
    return svc, client, control


async def _token(control: ControlService, operator: str = "op") -> str:
    tok = await control.issue_token(operator)
    return tok.token


# ─── Each command writes the expected register ──────────────────────────


async def test_test_command_pulses_test_register(ats_control):
    svc, client, control = ats_control
    res = await svc.execute("test", token=await _token(control), operator="op", role="operator")
    assert res["ok"] is True
    assert res["register"] == "ats_test"
    # ats_test: addr 0x0100, fc6, value 0x0001
    assert client.writes == [(0x0100, 0x0001, 6, None)]


async def test_bypass_delay_pulses_register(ats_control):
    svc, client, control = ats_control
    res = await svc.execute("bypass_delay", token=await _token(control), operator="op", role="operator")
    assert res["ok"] is True
    assert client.writes == [(0x0103, 0x0001, 6, None)]


async def test_inhibit_assert_then_release(ats_control):
    svc, client, control = ats_control
    await svc.execute("inhibit", token=await _token(control), operator="op", role="operator", assert_=True)
    await svc.execute("inhibit", token=await _token(control), operator="op", role="operator", assert_=False)
    # assert -> ats_inhibit_assert 0x0101=1 ; release -> ats_inhibit_release 0x0101=0
    assert client.writes == [(0x0101, 0x0001, 6, None), (0x0101, 0x0000, 6, None)]


# ─── Role gating ─────────────────────────────────────────────────────────


async def test_force_transfer_requires_admin(ats_control):
    svc, client, control = ats_control
    with pytest.raises(ControlError) as ei:
        await svc.execute(
            "force_transfer", token=await _token(control), operator="op", role="operator",
            assert_=True, override=True,
        )
    assert ei.value.code == "forbidden"
    assert ei.value.http_status == 403
    assert client.writes == []  # nothing written


async def test_admin_can_force_transfer_with_override(ats_control):
    svc, client, control = ats_control
    res = await svc.execute(
        "force_transfer", token=await _token(control, "admin"), operator="admin", role="admin",
        assert_=True, override=True,
    )
    assert res["ok"] is True
    assert client.writes == [(0x0102, 0x0001, 6, None)]


# ─── Force-transfer healthy-utility override guard ───────────────────────


async def test_force_transfer_blocked_when_utility_available(ats_control):
    svc, client, control = ats_control
    # default store has normal_available True
    with pytest.raises(ControlError) as ei:
        await svc.execute(
            "force_transfer", token=await _token(control), operator="admin", role="admin",
            assert_=True, override=False,
        )
    assert ei.value.code == "override_required"
    assert ei.value.http_status == 409
    assert client.writes == []


async def test_force_transfer_allowed_when_utility_unavailable(ats_control, authoritative_ats):
    svc, client, control = ats_control
    ats, store = authoritative_ats
    store.set_normal_available(False)
    await ats.on_poll("prime", store.as_reading("prime"), healthy())
    res = await svc.execute(
        "force_transfer", token=await _token(control, "admin"), operator="admin", role="admin",
        assert_=True, override=False,
    )
    assert res["ok"] is True
    assert client.writes == [(0x0102, 0x0001, 6, None)]


async def test_force_transfer_release_needs_no_override(ats_control):
    svc, client, control = ats_control
    # Releasing (assert_=False) is always allowed regardless of utility.
    res = await svc.execute(
        "force_transfer", token=await _token(control, "admin"), operator="admin", role="admin",
        assert_=False,
    )
    assert res["ok"] is True
    assert client.writes == [(0x0102, 0x0000, 6, None)]


# ─── Authority / comms gating ────────────────────────────────────────────


async def test_comms_lost_returns_502(regmap, fake_db, bus, control):
    ats = AtsService(regmap, fake_db, bus, slack=None)
    store = MockAtsPiStore()
    await ats.on_poll("base", store.as_reading("base"), lost())
    svc = AtsControlService(regmap, FakeClient(), fake_db, ats, control, slack=None)
    with pytest.raises(ControlError) as ei:
        await svc.execute("test", token=await _token(control), operator="op", role="operator")
    assert ei.value.code == "ats_comms_lost"
    assert ei.value.http_status == 502


async def test_non_authoritative_returns_409(regmap, fake_db, bus, control):
    # ICD major mismatch -> not authoritative, but comms healthy.
    ats = AtsService(regmap, fake_db, bus, slack=None)
    store = MockAtsPiStore()
    store.icd_major = 2
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.is_authoritative() is False
    svc = AtsControlService(regmap, FakeClient(), fake_db, ats, control, slack=None)
    with pytest.raises(ControlError) as ei:
        await svc.execute("test", token=await _token(control), operator="op", role="operator")
    assert ei.value.code == "ats_not_authoritative"
    assert ei.value.http_status == 409


# ─── Confirm-token discipline ────────────────────────────────────────────


async def test_invalid_token_rejected(ats_control):
    svc, client, _control = ats_control
    with pytest.raises(ControlError) as ei:
        await svc.execute("test", token="DEADBEEF", operator="op", role="operator")
    assert ei.value.code == "token_invalid"
    assert client.writes == []


async def test_token_is_single_use(ats_control):
    svc, client, control = ats_control
    tok = await _token(control)
    await svc.execute("test", token=tok, operator="op", role="operator")
    with pytest.raises(ControlError) as ei:
        await svc.execute("bypass_delay", token=tok, operator="op", role="operator")
    assert ei.value.code == "token_invalid"
    # only the first command wrote
    assert client.writes == [(0x0100, 0x0001, 6, None)]


async def test_modbus_write_failure_surfaces_502(ats_control):
    svc, client, control = ats_control
    client.ok = False
    client.error = "timeout"
    with pytest.raises(ControlError) as ei:
        await svc.execute("test", token=await _token(control), operator="op", role="operator")
    assert ei.value.code == "ats_modbus_failed"
    assert ei.value.http_status == 502


async def test_unknown_command_rejected(ats_control):
    svc, _client, control = ats_control
    with pytest.raises(ControlError) as ei:
        await svc.execute("detonate", token=await _token(control), operator="op", role="operator")
    assert ei.value.code == "unknown_command"


# ─── Fault gating: blind/faulted ATS-Pi refuses asserts, allows releases ──


async def test_input_fault_blocks_assert(regmap, fake_db, bus, control):
    """An assert is refused while the ATS-Pi reports INPUT_FAULT (blind
    sense) even though the Modbus link is healthy — INPUT_FAULT drops
    authority, so it surfaces as a 409 and nothing is written (audit
    CRITICAL: 'reachable but blind')."""
    ats = AtsService(regmap, fake_db, bus, slack=None)
    store = MockAtsPiStore()
    store.set_fault_bit(0x0001)  # INPUT_FAULT
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert not ats.is_authoritative()
    client = FakeClient()
    svc = AtsControlService(regmap, client, fake_db, ats, control, slack=None)
    with pytest.raises(ControlError) as ei:
        await svc.execute("test", token=await _token(control), operator="op", role="operator")
    assert ei.value.http_status == 409
    assert client.writes == []


async def test_output_fault_blocks_assert_but_allows_release(regmap, fake_db, bus, control):
    """OUTPUT_FAULT keeps the link authoritative (position still trusted) but
    blocks a command ASSERT (a relay is misbehaving). A maintained RELEASE
    must still go through — releasing is the fail-safe direction."""
    ats = AtsService(regmap, fake_db, bus, slack=None)
    store = MockAtsPiStore()
    store.set_fault_bit(0x0002)  # OUTPUT_FAULT
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert ats.is_authoritative()
    client = FakeClient()
    svc = AtsControlService(regmap, client, fake_db, ats, control, slack=None)

    with pytest.raises(ControlError) as ei:
        await svc.execute("inhibit", token=await _token(control), operator="op", role="operator", assert_=True)
    assert ei.value.code == "ats_fault"
    assert ei.value.http_status == 409
    assert client.writes == []

    res = await svc.execute("inhibit", token=await _token(control), operator="op", role="operator", assert_=False)
    assert res["ok"] is True
    assert client.writes == [(0x0101, 0x0000, 6, None)]


async def test_release_allowed_when_not_authoritative(regmap, fake_db, bus, control):
    """A maintained-command RELEASE is permitted even when the link is
    non-authoritative (here: ICD-major mismatch). Backing a command out is
    the safe direction and must never be blocked."""
    ats = AtsService(regmap, fake_db, bus, slack=None)
    store = MockAtsPiStore()
    store.icd_major = 2  # non-authoritative, comms healthy
    await ats.on_poll("base", store.as_reading("base"), healthy())
    assert not ats.is_authoritative()
    client = FakeClient()
    svc = AtsControlService(regmap, client, fake_db, ats, control, slack=None)
    res = await svc.execute(
        "force_transfer", token=await _token(control, "admin"),
        operator="admin", role="admin", assert_=False,
    )
    assert res["ok"] is True
    assert client.writes == [(0x0102, 0x0000, 6, None)]


# ─── Command-endpoint rate limiting ──────────────────────────────────────


async def test_command_endpoint_rate_limited(regmap, fake_db, authoritative_ats, control):
    """The ATS command endpoints are per-operator rate-limited so a client
    can't loop token->command pairs and flap a maintained relay faster than
    an operator could react. The confirm-token flow blocks blind replay; this
    is the additional flood guard at the API layer."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from genwatch.api.ats import _run
    from genwatch.services.ratelimit import RateLimiter

    ats, _store = authoritative_ats
    client = FakeClient()
    svc = AtsControlService(regmap, client, fake_db, ats, control, slack=None)
    limiter = RateLimiter(capacity=2, refill_per_s=0.0)  # 2 allowed, then blocked
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(ats_control=svc, command_limiter=limiter, db=fake_db)
        )
    )
    p = SimpleNamespace(operator="op", role="operator")

    # First two pulses pass the limiter (fresh token each).
    await _run(request, "test", await _token(control), p)
    await _run(request, "test", await _token(control), p)
    # Third is rate-limited → 429 before consuming a token or writing.
    n_writes = len(client.writes)
    with pytest.raises(HTTPException) as ei:
        await _run(request, "test", await _token(control), p)
    assert ei.value.status_code == 429
    assert ei.value.detail["code"] == "rate_limited"
    assert len(client.writes) == n_writes  # nothing actuated
