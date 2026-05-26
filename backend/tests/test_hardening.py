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


# ─── Watchdog ping-decision logic (audit H1) ──────────────────────────────


def test_watchdog_pings_during_cold_start_grace():
    """Boot window: no prime poll has succeeded yet, but we're still
    within the cold-start grace. Ping so systemd doesn't kill us before
    the bridge has a chance to come up."""
    should_ping, reason = notify.should_ping_watchdog(
        mono_last_prime_good=None,
        service_start_mono=1000.0,
        now_mono=1010.0,           # 10 s into grace
        stale_after_s=10.0,
        cold_start_grace_s=300.0,
    )
    assert should_ping is True
    assert reason is None


def test_watchdog_withholds_when_cold_start_grace_exhausted():
    """Misconfigured host (wrong IP, etc.): the first prime poll never
    arrives. Past the grace, withhold pings so systemd restart-loops
    the unit instead of leaving it silently zombie."""
    should_ping, reason = notify.should_ping_watchdog(
        mono_last_prime_good=None,
        service_start_mono=1000.0,
        now_mono=1000.0 + 301.0,   # 1 s past 5 min grace
        stale_after_s=10.0,
        cold_start_grace_s=300.0,
    )
    assert should_ping is False
    assert reason is not None
    assert "300" in reason and "genwatch doctor" in reason


def test_watchdog_pings_in_steady_state():
    """Normal operation: prime poll succeeded recently, well within
    stale_after."""
    should_ping, reason = notify.should_ping_watchdog(
        mono_last_prime_good=1050.0,
        service_start_mono=1000.0,
        now_mono=1055.0,           # 5 s since last good prime
        stale_after_s=10.0,
        cold_start_grace_s=300.0,
    )
    assert should_ping is True
    assert reason is None


def test_watchdog_withholds_on_silent_poll_loop():
    """The deadlocked-poller case the watchdog was designed for: prime
    poll did succeed once but the loop has gone silent past stale_after."""
    should_ping, reason = notify.should_ping_watchdog(
        mono_last_prime_good=1000.0,
        service_start_mono=900.0,  # well past grace
        now_mono=1050.0,           # 50 s of silence
        stale_after_s=10.0,
        cold_start_grace_s=300.0,
    )
    assert should_ping is False
    assert reason is not None
    assert "50" in reason  # silence value reported


def test_watchdog_grace_default_is_five_minutes():
    """Pin the default to 5 minutes — explicit value the audit picked
    based on real bridge/network boot timings. A future bump should be
    a deliberate, reviewed change rather than a silent drift."""
    assert notify.WATCHDOG_COLD_START_GRACE_S == 300.0


# ─── No silent mock fallback ──────────────────────────────────────────────


def test_config_does_not_auto_mock_when_device_missing(monkeypatch, tmp_path):
    """When the serial device is absent and mock isn't requested, the
    config layer must leave mock=False — we never silently switch to
    fake data. The lifespan layer logs a clear error and starts in a
    comms-lost state; here we just verify the config plumbing."""
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


# ─── Poller heartbeat + batch-fallback ───────────────────────────────────


async def test_poller_stamps_prime_heartbeat_on_success(tmp_path):
    """The poller must record a monotonic timestamp on each successful
    prime poll. The systemd watchdog ticker uses this to decide whether
    to keep pinging — a hung loop without a fresh heartbeat must let
    systemd restart the unit."""
    import time
    from genwatch.modbus.client import MockModbusClient
    from genwatch.modbus.poller import Poller
    from genwatch.modbus.registers import load_register_map

    regmap = load_register_map("genwatch/registers/h100.yaml")
    client = MockModbusClient(regmap)
    await client.connect()

    async def cb(tier, reading, health):
        pass

    p = Poller(client, regmap, cb)
    assert p.health.last_prime_good_monotonic is None
    await p._poll_tier("prime", p._prime_batches)
    assert p.health.last_prime_good_monotonic is not None
    # Heartbeat is monotonic (not wall-clock) so NTP jumps can't fool the watchdog.
    assert p.health.last_prime_good_monotonic <= time.monotonic()


async def test_poller_falls_back_to_singles_when_batch_fails(tmp_path):
    """A failing block read must not blank out the registers it covers.
    The poller falls back to single-register reads so one bad address
    can't take out an entire telemetry tier."""
    from dataclasses import dataclass
    from genwatch.modbus.client import ModbusResult
    from genwatch.modbus.poller import Poller
    from genwatch.modbus.registers import load_register_map

    regmap = load_register_map("genwatch/registers/h100.yaml")

    @dataclass
    class FakeClient:
        # Fail every multi-register batch, succeed every single read.
        single_calls: int = 0

        async def connect(self):
            return True

        async def close(self):
            pass

        async def read(self, addr, count, fc=3):
            if count == 1:
                self.single_calls += 1
                return ModbusResult.success([0x1234], 1.0)
            return ModbusResult.failure("simulated_batch_failure", 1.0)

        async def write(self, *a, **kw):
            return ModbusResult.failure("not_used")

    fc = FakeClient()

    async def cb(tier, reading, health):
        pass

    p = Poller(fc, regmap, cb)
    await p._poll_tier("prime")
    # The fan-out must have happened — every register in the prime tier
    # has its single-read fallback exercised.
    assert fc.single_calls > 0
    # And the prime heartbeat is still stamped, since the fan-outs
    # recovered some data.
    assert p.health.last_prime_good_monotonic is not None


async def test_poller_skips_registers_whose_fanout_fails(tmp_path):
    """A register whose single-read fallback ALSO fails must be skipped,
    not decoded as a sentinel zero. Stamping 0 on coolant_temp or RPM
    can trip an out-of-range alarm comparator and corrupt the audit
    record. Preserves the last good value instead."""
    from dataclasses import dataclass, field
    from genwatch.modbus.client import ModbusResult
    from genwatch.modbus.poller import Poller
    from genwatch.modbus.registers import load_register_map

    regmap = load_register_map("genwatch/registers/h100.yaml")

    # Pick a known register on the prime tier so the test doesn't depend
    # on YAML reordering. output_status_1 is bitfield, single-word — easy
    # to target by exact address.
    target = regmap.by_name("output_status_1")
    assert target is not None

    @dataclass
    class FakeClient:
        # Batches always fail; singles fail ONLY for `target` and succeed
        # for every other address.
        single_calls: int = 0
        addrs_failed: list[int] = field(default_factory=list)

        async def connect(self):
            return True

        async def close(self):
            pass

        async def read(self, addr, count, fc=3):
            if count == 1:
                self.single_calls += 1
                if addr == target.addr:
                    self.addrs_failed.append(addr)
                    return ModbusResult.failure("simulated_addr_failure", 1.0)
                return ModbusResult.success([0x4321], 1.0)
            return ModbusResult.failure("simulated_batch_failure", 1.0)

        async def write(self, *a, **kw):
            return ModbusResult.failure("not_used")

    fc = FakeClient()

    async def cb(tier, reading, health):
        pass

    p = Poller(fc, regmap, cb)
    # Pre-seed a known-good value so we can verify it survives the bad
    # fan-out read.
    p.reading.values[target.name] = 0xABCD
    await p._poll_tier("prime")
    # The target's fan-out failed — we must NOT have overwritten its
    # value with 0 (or anything else from this cycle).
    assert p.reading.values[target.name] == 0xABCD, (
        "fan-out failure on a single register must preserve the last good "
        "value, not substitute a sentinel zero"
    )
    # Sanity: the target was actually attempted, and some other prime
    # register did get the success value, proving the cycle ran.
    assert fc.addrs_failed == [target.addr]
    other_prime_names = [r.name for r in regmap.tier("prime") if r.name != target.name]
    assert any(p.reading.values.get(n) == 0x4321 for n in other_prime_names), (
        "neighbouring registers in the same batch must still decode their "
        "successful fan-out reads"
    )


async def test_poller_apply_regmap_swaps_batches_and_cadence(tmp_path):
    """POST /api/registers/reload must update the live poller, not just
    app.state.regmap. Otherwise the operator edits the YAML, reloads,
    and the poller silently keeps reading the old addresses."""
    from copy import deepcopy
    from dataclasses import dataclass, field
    from genwatch.modbus.client import ModbusResult
    from genwatch.modbus.poller import Poller
    from genwatch.modbus.registers import RegisterDef, load_register_map

    regmap = load_register_map("genwatch/registers/h100.yaml")

    @dataclass
    class FakeClient:
        async def connect(self): return True
        async def close(self): pass
        async def read(self, addr, count, fc=3):
            return ModbusResult.success([0] * count, 1.0)
        async def write(self, *a, **kw):
            return ModbusResult.failure("not_used")

    async def cb(tier, reading, health):
        pass

    p = Poller(FakeClient(), regmap, cb)
    prime_before = len(p._prime_batches)
    base_before = len(p._base_batches)
    rate_before = p.health.rate_ms

    # Build a new map with the prime cadence changed and one extra prime
    # register to force a different batch count. deepcopy keeps the test
    # independent of YAML mutations elsewhere.
    new_regmap = deepcopy(regmap)
    new_regmap.prime_poll_ms = 750
    new_regmap.registers = list(new_regmap.registers) + [
        RegisterDef(
            name="_test_extra_prime",
            addr=0x0F00,
            fc=3,
            type="u16",
            tier="prime",
            group="State",
        )
    ]

    await p.apply_regmap(new_regmap)

    # Poller must now hold the new map and have re-derived batches.
    assert p.regmap is new_regmap
    assert p.health.rate_ms == 750
    assert any(start == 0x0F00 for start, _ in p._prime_batches), (
        "new prime register should have produced a new batch entry"
    )
    # Old batch count is allowed to change either way — we only assert
    # that the poller actually re-derived rather than keeping the old
    # tuples. (We can detect that via the new entry presence above.)
    _ = prime_before, base_before, rate_before  # silence unused


# ─── Modbus client ────────────────────────────────────────────────────────


def test_tcp_keepalive_applied_to_real_socket():
    """The keepalive helper must enable SO_KEEPALIVE and tune the Linux
    TCP_KEEP* timings on the underlying socket. Without this, a wedged
    Lantronix TCP connection (NAT timeout, switch reboot, bridge crash
    with no FIN/RST) is invisible until the application-level read
    timeout expires — and then often after a full retry budget."""
    import socket
    from genwatch.modbus.client import _apply_tcp_keepalive

    class FakeTransport:
        def __init__(self, sock):
            self._sock = sock
            self.queries: list[str] = []

        def get_extra_info(self, key):
            self.queries.append(key)
            return self._sock if key == "socket" else None

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _apply_tcp_keepalive(FakeTransport(sock), "192.0.2.1", 10001)
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        if hasattr(socket, "TCP_KEEPIDLE"):
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
    finally:
        sock.close()


def test_tcp_keepalive_tolerates_missing_socket():
    """Keepalive is a hardening measure, not load-bearing. If the
    transport doesn't expose a socket — future pymodbus refactor, mocked
    transport, etc. — the helper must not raise."""
    from genwatch.modbus.client import _apply_tcp_keepalive

    class NoSocketTransport:
        def get_extra_info(self, key):
            return None

    # Must not raise.
    _apply_tcp_keepalive(NoSocketTransport(), "192.0.2.1", 10001)
    _apply_tcp_keepalive(None, "192.0.2.1", 10001)


# ─── CLI: panel command ──────────────────────────────────────────────────


def test_panel_command_runs_against_mock(monkeypatch, tmp_path, capsys):
    """The `genwatch panel` CLI reads every register in the map and
    prints a decoded report. Smoke-test that it (a) actually runs end
    to end against the mock client, (b) decodes the engine state, and
    (c) renders the bitfield meaning inline so an operator can
    cross-check the panel LCD."""
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("t"))

    from genwatch.__main__ import _panel

    rc = _panel([])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Engine state:" in out
    assert "STOPPED" in out          # mock starts in stopped
    assert "matched: output_status_1 bit 0x0100" in out
    assert "Active alarms" in out
    assert "Cross-check against the H-100 LCD" in out
    # Bitfield rendering must show the engine_state hint inline.
    assert "state:stopped" in out


def test_panel_command_json_output_is_parseable(monkeypatch, tmp_path, capsys):
    """--json must produce valid JSON suitable for piping to jq."""
    import json
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("t"))

    from genwatch.__main__ import _panel

    rc = _panel(["--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    doc = json.loads(out)
    assert doc["engine_state"] == "stopped"
    assert doc["slave"] == 100
    assert any(r["name"] == "output_status_1" for r in doc["registers"])
    assert isinstance(doc["active_alarms"], list)


# ─── Session-cookie hardening ─────────────────────────────────────────────


def _parse_set_cookie(header: str) -> dict[str, str]:
    """Tiny Set-Cookie attribute parser — good enough for these tests.

    Returns a dict of lower-cased attribute names. Boolean flags map to
    the empty string. The cookie name/value lives under the 'name' /
    'value' keys.
    """
    parts = [p.strip() for p in header.split(";")]
    name, _, value = parts[0].partition("=")
    out = {"name": name, "value": value}
    for p in parts[1:]:
        if "=" in p:
            k, _, v = p.partition("=")
            out[k.strip().lower()] = v.strip()
        else:
            out[p.lower()] = ""
    return out


async def test_login_cookie_defaults_are_strict_samesite_and_httponly(client):
    """Default: SameSite=strict, HttpOnly, no Secure on plain HTTP."""
    r = await client.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    attrs = _parse_set_cookie(sc)
    assert attrs["name"] == "genwatch_session"
    assert attrs["value"], "cookie value must be present"
    assert "httponly" in attrs, "missing HttpOnly attribute"
    assert attrs.get("samesite", "").lower() == "strict", attrs
    assert "secure" not in attrs, "HTTP request must not get Secure cookie by default"
    assert attrs.get("path") == "/"


async def test_login_cookie_auto_secure_on_https_request(app_env):
    """Auto-detect: an HTTPS-scheme request gets Secure for free."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.1)
            r = await c.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    attrs = _parse_set_cookie(sc)
    assert "secure" in attrs, f"HTTPS request must get Secure; got {sc!r}"
    assert attrs.get("samesite", "").lower() == "strict"


async def test_cookie_secure_explicit_true_forces_secure_even_on_http(monkeypatch, tmp_path):
    """Config override: cookie_secure=true forces Secure on every response."""
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_AUTH__COOKIE_SECURE", "true")

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.1)
            r = await c.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200
    attrs = _parse_set_cookie(r.headers.get("set-cookie", ""))
    assert "secure" in attrs, "explicit cookie_secure=true must force Secure"


async def test_cookie_samesite_lax_override_applies(monkeypatch, tmp_path):
    """Operators who need cross-site nav with the session cookie can opt
    back into SameSite=lax."""
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_AUTH__COOKIE_SAMESITE", "lax")

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.1)
            r = await c.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200
    attrs = _parse_set_cookie(r.headers.get("set-cookie", ""))
    assert attrs.get("samesite", "").lower() == "lax"


def test_cookie_samesite_none_without_secure_rejected_at_config_load():
    """Browsers require Secure for SameSite=None; reject the invalid
    combo at startup rather than at runtime when the cookie would
    silently be discarded by the browser."""
    from pydantic import ValidationError
    from genwatch.config import AuthConfig

    with pytest.raises(ValidationError):
        AuthConfig(cookie_samesite="none", cookie_secure=False)
    # Allowed when paired with secure=True
    cfg = AuthConfig(cookie_samesite="none", cookie_secure=True)
    assert cfg.cookie_samesite == "none"


async def test_logout_clears_cookie_with_matching_attributes(client):
    """delete_cookie() must mirror set_cookie()'s SameSite / Secure /
    Path so the browser actually evicts the cookie. Missing attributes
    cause some browsers to leave a stale (but expired) cookie behind."""
    # Establish a session
    r = await client.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200
    # Logout and inspect the clearing Set-Cookie
    r = await client.post("/api/auth/logout")
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    attrs = _parse_set_cookie(sc)
    assert attrs["name"] == "genwatch_session"
    assert attrs.get("samesite", "").lower() == "strict"
    assert attrs.get("path") == "/"
    # Either Max-Age=0 or an Expires in the past — both are valid clears
    cleared = (
        attrs.get("max-age") == "0"
        or "expires" in attrs
    )
    assert cleared, f"logout did not clear the cookie: {sc!r}"


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
