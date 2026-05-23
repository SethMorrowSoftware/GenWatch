"""Tests for the production-hardening behaviors.

Covers:
  - login rate-limiting (429 after burst)
  - events retention prune skips alarms/warns
  - sd_notify no-ops when NOTIFY_SOCKET is unset
  - config refuses auto-mock when device is missing (no silent fallback)
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from genwatch.config import load
from genwatch.db import Database
from genwatch.main import create_app
from genwatch.services import notify
from genwatch.services.auth import hash_password
from genwatch.services.ratelimit import RateLimiter


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    yield


@pytest.fixture
async def client(app_env):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)
            yield c


# ─── Rate limiter ─────────────────────────────────────────────────────────


def test_rate_limiter_allows_burst_then_blocks():
    rl = RateLimiter(capacity=3, refill_per_s=0.001)  # essentially no refill
    assert rl.check("a") is True
    assert rl.check("a") is True
    assert rl.check("a") is True
    assert rl.check("a") is False
    # different key gets its own bucket
    assert rl.check("b") is True


def test_rate_limiter_reset_restores_capacity():
    rl = RateLimiter(capacity=2, refill_per_s=0.001)
    rl.check("a")
    rl.check("a")
    assert rl.check("a") is False
    rl.reset("a")
    assert rl.check("a") is True


def test_rate_limiter_retry_after_reports_seconds():
    rl = RateLimiter(capacity=1, refill_per_s=0.1)  # 1 token per 10s
    rl.check("a")  # spend it
    assert rl.check("a") is False
    after = rl.retry_after_s("a")
    assert 1 <= after <= 11


async def test_login_returns_429_after_repeated_failures(client):
    last_status = None
    for _ in range(8):
        r = await client.post("/api/auth/login", json={"password": "WRONG"})
        last_status = r.status_code
        if last_status == 429:
            break
    assert last_status == 429, f"expected 429 after burst, got {last_status}"
    body = r.json()
    assert body["detail"]["code"] == "rate_limited"
    assert r.headers.get("Retry-After")


# ─── Events retention ─────────────────────────────────────────────────────


def test_prune_events_keeps_alarms_and_warns(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    old = time.time() - 365 * 86400
    # write rows directly so we control ts
    with db._writer() as c:
        c.executemany(
            "INSERT INTO events (ts, severity, type, message) VALUES (?, ?, ?, ?)",
            [
                (old, "info", "BOOT", "old info"),
                (old, "ok", "TRANSITION", "old ok"),
                (old, "warn", "COMMS", "old warn"),
                (old, "alarm", "ALARM", "old alarm"),
            ],
        )
    pruned = db.prune_events(time.time() - 30 * 86400)
    assert pruned == 2  # info + ok
    rows = db.read_events(limit=100)
    sevs = sorted(r["severity"] for r in rows)
    assert sevs == ["alarm", "warn"]


# ─── notify ───────────────────────────────────────────────────────────────


def test_notify_no_socket_is_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify.ready() is False
    assert notify.watchdog() is False
    assert notify.stopping() is False
    assert notify.watchdog_interval_s() is None


def test_notify_watchdog_interval_parses_usec(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "60000000")  # 60s
    assert notify.watchdog_interval_s() == pytest.approx(30.0)
    monkeypatch.setenv("WATCHDOG_USEC", "garbage")
    assert notify.watchdog_interval_s() is None


# ─── No silent mock fallback ──────────────────────────────────────────────


def test_config_does_not_auto_mock_when_device_missing(monkeypatch, tmp_path):
    """When the serial device is absent and mock isn't requested, the
    config layer must leave mock=False. Refusing-to-start is enforced
    in main.lifespan; here we just verify the config plumbing."""
    monkeypatch.delenv("GENWATCH_MOCK", raising=False)
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_SERIAL__DEVICE", "/dev/definitely-not-a-real-port")
    s = load(None)
    assert s.mock is False


# ─── Transport selection (serial vs tcp) ────────────────────────────────


def test_transport_defaults_to_tcp(monkeypatch, tmp_path):
    """The default transport is TCP — most deploys use a Lantronix bridge."""
    monkeypatch.delenv("GENWATCH_TRANSPORT", raising=False)
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    s = load(None)
    assert s.transport == "tcp"
    assert s.modbus_tcp.port == 10001
    assert s.modbus_tcp.framer == "rtu"


def test_transport_can_be_set_to_serial_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_TRANSPORT", "serial")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    s = load(None)
    assert s.transport == "serial"


def test_modbus_tcp_host_port_overridable_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__HOST", "10.20.30.40")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__PORT", "10008")
    s = load(None)
    assert s.modbus_tcp.host == "10.20.30.40"
    assert s.modbus_tcp.port == 10008


async def test_tcp_client_reports_failure_when_bridge_unreachable():
    """Reads must not raise — they return a ModbusResult with ok=False
    so the poller can surface a 'comms lost' state instead of crashing."""
    from genwatch.modbus.client import TcpRtuModbusClient

    # 127.0.0.1:1 — privileged port nobody's listening on
    c = TcpRtuModbusClient(
        host="127.0.0.1", port=1, framer="rtu",
        timeout_s=0.5, connect_timeout_s=0.5,
        slave=100, retries=0, backoff_s=[0.1],
    )
    ok = await c.connect()
    assert ok is False
    r = await c.read(0x0001, 1)
    assert r.ok is False
    assert r.error  # some error string is set
