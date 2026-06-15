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


# ─── `genwatch hash` CLI: stdin prompt (audit H2) ─────────────────────────


def test_hash_command_prompts_when_no_argv(monkeypatch, capsys):
    """No password on argv → prompt via getpass twice (with confirm)
    and emit a bcrypt hash. The plaintext must never appear in
    stdout/stderr (which would defeat the entire purpose) — only the
    hash."""
    import getpass

    from genwatch.__main__ import main
    from genwatch.services.auth import verify_password

    prompts: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return "correcthorsebatterystaple"

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.argv", ["genwatch", "hash"])

    rc = main()
    out = capsys.readouterr()

    assert rc == 0
    assert len(prompts) == 2, "must prompt twice (entry + confirm)"
    assert "Confirm" in prompts[1] or "confirm" in prompts[1].lower()
    # Plaintext must not leak into either stream
    assert "correcthorsebatterystaple" not in out.out
    assert "correcthorsebatterystaple" not in out.err
    # The hash that was printed must verify against the typed password
    hashed = out.out.strip()
    assert hashed.startswith("$2"), f"expected bcrypt hash, got {hashed!r}"
    assert verify_password("correcthorsebatterystaple", hashed)


def test_hash_command_rejects_mismatched_confirmation(monkeypatch, capsys):
    """Defense against a typo in the prompt — confirm must match
    entry. Empty match also rejected to avoid quietly hashing the
    empty string."""
    import getpass

    from genwatch.__main__ import main

    answers = iter(["typoed-pw", "different-pw"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.argv", ["genwatch", "hash"])

    rc = main()
    out = capsys.readouterr()
    assert rc == 1
    assert "do not match" in out.err
    # No hash printed on mismatch
    assert "$2" not in out.out


def test_hash_command_rejects_empty_password(monkeypatch, capsys):
    import getpass

    from genwatch.__main__ import main

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.argv", ["genwatch", "hash"])

    rc = main()
    out = capsys.readouterr()
    assert rc == 1
    assert "empty" in out.err


def test_hash_command_refuses_non_tty_without_argv(monkeypatch, capsys):
    """Piping a password into stdin (`echo pw | genwatch hash`) defeats
    the whole point — the plaintext just moves from argv to the calling
    shell's history. Refuse this case and point operators at the argv
    form (with its warning) for non-interactive use."""
    from genwatch.__main__ import main

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.argv", ["genwatch", "hash"])

    rc = main()
    out = capsys.readouterr()
    assert rc == 2
    assert "interactive terminal" in out.err
    assert "$2" not in out.out


def test_hash_command_argv_path_works_but_warns(monkeypatch, capsys):
    """The legacy argv path must still work (install scripts depend on
    it), but it must emit a stderr warning so an operator who didn't
    know about the safer form learns about it."""
    from genwatch.__main__ import main
    from genwatch.services.auth import verify_password

    monkeypatch.setattr("sys.argv", ["genwatch", "hash", "my-password"])
    rc = main()
    out = capsys.readouterr()
    assert rc == 0
    # Warning printed to stderr
    assert "shell history" in out.err or "ps aux" in out.err
    # But the hash IS produced
    hashed = out.out.strip()
    assert verify_password("my-password", hashed)


# ─── No silent mock fallback ──────────────────────────────────────────────


async def test_lifespan_refuses_empty_jwt_secret_in_production(monkeypatch, tmp_path):
    """In production (non-mock) the service must refuse to start with
    an empty jwt_secret. Silently minting an ephemeral one under
    Restart=always produces a unit that's "up" but logs every operator
    out on each restart — masking the real config problem and
    invalidating sessions repeatedly. Fail fast instead."""
    from genwatch.main import create_app

    monkeypatch.delenv("GENWATCH_MOCK", raising=False)  # non-mock
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "")  # empty
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", "$2b$12$" + "x" * 53)
    # Point at a TCP target that can't connect — lifespan should raise
    # on the JWT check BEFORE attempting any Modbus I/O.
    monkeypatch.setenv("GENWATCH_TRANSPORT", "tcp")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__HOST", "127.0.0.1")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__PORT", "1")  # unused port

    app = create_app()
    with pytest.raises(RuntimeError, match=r"jwt_secret"):
        async with app.router.lifespan_context(app):
            pass  # should never enter


async def test_lifespan_mock_mode_still_generates_ephemeral_secret(monkeypatch, tmp_path):
    """Dev/CI shouldn't be blocked by an unset secret — mock mode
    generates an ephemeral one (and logs a warning). The production-
    only refusal preserves that workflow."""
    from genwatch.main import create_app

    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "")
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", "$2b$12$" + "x" * 53)

    app = create_app()
    # Should boot cleanly — no RuntimeError, no auth complaint.
    async with app.router.lifespan_context(app):
        # The settings object inside lifespan got the ephemeral secret;
        # verify by issuing a token using the live state.
        assert app.state.settings.auth.jwt_secret != ""


@pytest.mark.parametrize("bad_secret", ["REPLACE_ME", "short"])
async def test_lifespan_refuses_placeholder_or_short_jwt_secret(monkeypatch, tmp_path, bad_secret):
    """A truthy-but-known/weak jwt_secret must be rejected in production,
    not just an empty one. The shipped config template seeds the literal
    'REPLACE_ME'; a restored/hand-edited config that keeps it would
    otherwise boot signing admin sessions with a world-known HS256 key —
    a full auth bypass. Anything < 32 chars is likewise treated as unset."""
    from genwatch.main import create_app

    monkeypatch.delenv("GENWATCH_MOCK", raising=False)  # non-mock
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", bad_secret)
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", "$2b$12$" + "x" * 53)
    monkeypatch.setenv("GENWATCH_TRANSPORT", "tcp")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__HOST", "127.0.0.1")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__PORT", "1")

    app = create_app()
    with pytest.raises(RuntimeError, match=r"jwt_secret"):
        async with app.router.lifespan_context(app):
            pass  # should never enter


@pytest.mark.parametrize("bad_hash", ["", "REPLACE_ME", "not-a-bcrypt-hash"])
async def test_lifespan_refuses_bad_admin_password_hash(monkeypatch, tmp_path, bad_hash):
    """A missing / placeholder / non-bcrypt admin_password_hash must
    refuse to boot in production. Otherwise the unit comes up 'healthy'
    (green systemd + watchdog) while every login 401s — a silent lockout.
    A real bcrypt hash is required."""
    from genwatch.main import create_app

    monkeypatch.delenv("GENWATCH_MOCK", raising=False)  # non-mock
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "y" * 64)  # valid
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", bad_hash)
    monkeypatch.setenv("GENWATCH_TRANSPORT", "tcp")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__HOST", "127.0.0.1")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__PORT", "1")

    app = create_app()
    with pytest.raises(RuntimeError, match=r"admin_password_hash"):
        async with app.router.lifespan_context(app):
            pass  # should never enter


async def test_control_rejected_when_comms_lost(tmp_path):
    """A Start must be refused when the H-100 link is LOST. engine_state
    is pinned to its last value across an outage (state.py), so the
    validity gate would otherwise pass against stale data and the panel
    gate would read a stale 'auto'. The confirm token must survive the
    denial (the gate runs before token-consume) so the operator can retry
    once comms recover, and no Modbus write may be attempted."""
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from genwatch.modbus.registers import load_register_map
    from genwatch.services.control import ControlError, ControlService

    regmap = load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")

    writes: list = []

    class FakeClient:
        async def write(self, addr, value=None, *, fc=6, values=None):
            writes.append((addr, value, fc, values))
            return SimpleNamespace(ok=True, error=None)

    # Stale-but-confident snapshot: looks startable, but comms are LOST.
    snap = SimpleNamespace(
        panel_mode="auto", engine_state="stopped",
        comms=SimpleNamespace(state="lost"),
    )
    state = SimpleNamespace(snap=snap)
    ctl = ControlService(regmap, FakeClient(), MagicMock(), state, slack=None)

    tok = await ctl.issue_token("op")
    with pytest.raises(ControlError) as ei:
        await ctl.execute("start", tok.token, "op", "operator")
    assert ei.value.code == "comms_lost"
    assert ei.value.http_status == 409
    assert writes == []  # never touched the wire

    # Token survived the denial — once comms recover the same token works.
    snap.comms.state = "healthy"
    res = await ctl.execute("start", tok.token, "op", "operator")
    assert res["ok"] is True
    assert writes == [(0x019C, None, 16, [0x0080, 0x0000, 0x0000])]


async def test_confirm_token_is_verb_bound(tmp_path):
    """A token issued for one action can't be spent on another (stops a
    stale Start tab from confirming a Stop with its token). Unbound
    tokens (no verb at issue) stay usable for any action — backward-compat
    for non-browser clients."""
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from genwatch.modbus.registers import load_register_map
    from genwatch.services.control import ControlError, ControlService

    regmap = load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")
    state = SimpleNamespace(snap=SimpleNamespace(
        panel_mode="auto", engine_state="stopped", comms=SimpleNamespace(state="healthy"),
    ))
    ctl = ControlService(regmap, MagicMock(), MagicMock(), state, slack=None)

    # Bound to 'start' → cannot be consumed for 'stop'.
    tok = await ctl.issue_token("op", verb="start")
    with pytest.raises(ControlError) as ei:
        await ctl.consume_token(tok.token, "op", verb="stop")
    assert ei.value.code == "token_action_mismatch"
    assert ei.value.http_status == 403

    # Bound to 'start' → works for 'start'.
    tok2 = await ctl.issue_token("op", verb="start")
    ct = await ctl.consume_token(tok2.token, "op", verb="start")
    assert ct.verb == "start"

    # Unbound (verb=None at issue) → usable for any action.
    tok3 = await ctl.issue_token("op")
    ct3 = await ctl.consume_token(tok3.token, "op", verb="stop")
    assert ct3 is not None


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


def test_config_env_overrides_yaml(monkeypatch, tmp_path):
    """Environment variables must outrank config.yaml (the documented
    contract). The previous loader passed YAML as constructor kwargs,
    which in pydantic-settings silently won over every GENWATCH_*
    override. The nested deep-merge must also preserve sibling YAML
    fields when env overrides just one of them."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "transport: tcp\n"
        f"data_dir: {tmp_path / 'yamldata'}\n"
        "auth:\n"
        "  jwt_secret: YAML_SECRET\n"
        "  admin_password_hash: YAML_HASH\n"
        "modbus_tcp:\n"
        "  host: 9.9.9.9\n"
    )
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "ENV_SECRET")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path / "envdata"))

    s = load(str(cfg))
    # Env wins on the conflicting key…
    assert s.auth.jwt_secret == "ENV_SECRET"
    # …but the sibling YAML field under the same nested model survives.
    assert s.auth.admin_password_hash == "YAML_HASH"
    # YAML applies where there's no env override.
    assert s.modbus_tcp.host == "9.9.9.9"
    # Top-level env (data_dir, set by the systemd unit) beats YAML too.
    assert s.data_dir == str(tmp_path / "envdata")


def test_db_rollup_1h_and_long_span_read(tmp_path):
    """The 1m→1h rollup must populate telemetry_1h, and a long-span read
    must serve from it — otherwise history older than the 1m horizon
    (90 d) is silently lost despite the config advertising ~2 years."""
    from genwatch.db import Database

    db = Database(tmp_path / "t.sqlite")
    base = 1_000_000  # fixed epoch
    for i in range(0, 3 * 3600, 30):  # 3 hours of raw, every 30 s
        db.write_telemetry(base + i, {"total_kw": 100.0, "rpm": 1800.0}, "running", 0)
    db.aggregate_rollup_1m(base, base + 3 * 3600)
    n1h = db.aggregate_rollup_1h(base, base + 3 * 3600 + 1)
    assert n1h >= 3  # ~3 hourly buckets

    # A >14-day span routes to the 1h tier and returns the rolled data.
    rows = db.read_telemetry("kw", base, base + 30 * 86400)
    assert len(rows) >= 3
    assert all(abs(v - 100.0) < 1.0 for _, v in rows)
    db.close()


def test_db_prune_is_chunked(tmp_path):
    """Chunked prune must delete everything matching across multiple
    chunks (releasing the write lock between each)."""
    from genwatch.db import Database

    db = Database(tmp_path / "t.sqlite")
    for i in range(25):
        db.write_telemetry(1000.0 + i, {"total_kw": 1.0}, "running", 0)
    deleted = db.prune_raw_telemetry(2000.0, chunk=10)  # 25 rows, chunk 10 → 3 passes
    assert deleted == 25
    assert db.read_telemetry("kw", 0, 3000) == []
    db.close()


def test_db_checkpoint_and_reads_dont_take_write_lock(tmp_path):
    """checkpoint() is best-effort and must not raise; a read must be
    possible while the write lock is held (reads use their own
    connection, so they don't serialize behind writes/prunes)."""
    from genwatch.db import Database

    db = Database(tmp_path / "t.sqlite")
    db.write_event("info", "BOOT", "hi")
    db.checkpoint()  # must not raise
    # Hold the write lock and confirm a read still completes.
    with db._writer():
        rows = db.read_events(limit=10)
    assert any(e["type"] == "BOOT" for e in rows)
    db.close()


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


async def test_poller_stamps_value_ages_on_successful_decode(tmp_path):
    """Every successful register decode bumps its monotonic age stamp.
    Downstream eviction logic uses this to retire entries whose fan-out
    has been failing for many cycles — without per-register age, a
    boot-time value can linger forever and masquerade as fresh."""
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
    assert p.reading.value_ages == {}
    t0 = time.monotonic()
    await p._poll_tier("prime")
    # Every decoded prime register has an age, and it's monotonic-clock
    # based (not wall clock).
    assert len(p.reading.value_ages) > 0
    for name, age_ts in p.reading.value_ages.items():
        assert t0 <= age_ts <= time.monotonic(), (
            f"age for {name} should be a monotonic timestamp inside the poll window"
        )
        assert name in p.reading.values, "every aged register should have a value"


async def test_poller_evicts_stale_values_past_tier_threshold(tmp_path):
    """A register whose fan-out keeps failing for many cycles must
    eventually be dropped from `Reading.values`. The TTL is
    TIER_STALE_MULTIPLIER × tier_cadence; before that threshold the
    last-good is preserved (test_poller_skips_registers_whose_fanout_fails),
    after it the value disappears so consumers see None instead of a
    phantom-fresh datum that could fool an alarm comparator."""
    from dataclasses import dataclass, field
    from genwatch.modbus.client import ModbusResult
    from genwatch.modbus.poller import Poller, TIER_STALE_MULTIPLIER
    from genwatch.modbus.registers import load_register_map

    regmap = load_register_map("genwatch/registers/h100.yaml")
    target = regmap.by_name("output_status_1")
    assert target is not None

    @dataclass
    class FakeClient:
        # Always succeed except for the target address — both batches
        # covering it and its single-read fallback fail.
        addrs_failed: list[int] = field(default_factory=list)

        async def connect(self): return True

        async def close(self): pass

        async def read(self, addr, count, fc=3):
            if count == 1:
                if addr == target.addr:
                    self.addrs_failed.append(addr)
                    return ModbusResult.failure("simulated", 1.0)
                return ModbusResult.success([0x1234], 1.0)
            # All batches fail so we fall through to single reads — that
            # exercises the per-register fan-out path including the
            # target's failure case.
            return ModbusResult.failure("simulated_batch_failure", 1.0)

    fc = FakeClient()

    async def cb(tier, reading, health):
        pass

    p = Poller(fc, regmap, cb)
    # Pre-seed a known last-good value AND a fresh age stamp so we can
    # observe the eviction transition. Without the age stamp the
    # eviction logic correctly skips the register (no age = never
    # decoded = nothing to evict).
    import time
    p.reading.values[target.name] = 0xABCD
    p.reading.value_ages[target.name] = time.monotonic()

    # First poll: target's fan-out fails but the value is still fresh
    # (age stamped just now). Must preserve.
    await p._poll_tier("prime")
    assert p.reading.values.get(target.name) == 0xABCD, (
        "value should survive a single failing cycle while still fresh"
    )

    # Now backdate the age past the eviction threshold so the next
    # poll's eviction sweep will drop it. Cadence × multiplier seconds
    # of simulated age, plus a margin.
    threshold_s = (regmap.prime_poll_ms / 1000.0) * TIER_STALE_MULTIPLIER
    p.reading.value_ages[target.name] = time.monotonic() - threshold_s - 1.0

    await p._poll_tier("prime")
    # The eviction sweep ran, target's age was over threshold, and the
    # fan-out still couldn't refresh it — so the value is now gone.
    # Downstream consumers will see None and degrade gracefully.
    assert target.name not in p.reading.values, (
        "stale value past TIER_STALE_MULTIPLIER × cadence should be evicted"
    )
    assert target.name not in p.reading.value_ages


async def test_poller_eviction_is_per_tier(tmp_path):
    """A prime-tier poll must not evict base-tier values (and vice
    versa). Each tier walks only its own registers. Without this,
    a frozen prime cycle would silently retire every base-tier metric
    even though base reads might be working fine."""
    from dataclasses import dataclass
    from genwatch.modbus.client import ModbusResult
    from genwatch.modbus.poller import Poller
    from genwatch.modbus.registers import load_register_map

    import time

    regmap = load_register_map("genwatch/registers/h100.yaml")

    @dataclass
    class FakeClient:
        async def connect(self): return True

        async def close(self): pass

        async def read(self, addr, count, fc=3):
            # Always fail so the fan-out fails and the eviction sweep
            # runs but doesn't refresh anything.
            return ModbusResult.failure("simulated", 1.0)

    fc = FakeClient()

    async def cb(tier, reading, health):
        pass

    p = Poller(fc, regmap, cb)
    # Pre-seed a base-tier value that's well past the prime tier's
    # eviction threshold but still within base tier's threshold.
    base_reg = next(r for r in regmap.tier("base"))
    p.reading.values[base_reg.name] = 0xBEEF
    # Age = 5× prime_cadence ago: past prime threshold (3× prime),
    # under base threshold (3× base, which is much longer).
    age_for_prime_eviction = (regmap.prime_poll_ms / 1000.0) * 5
    p.reading.value_ages[base_reg.name] = time.monotonic() - age_for_prime_eviction

    # Run a PRIME poll. Even though we'd be past the prime threshold
    # if the base reg were on prime, it's actually on base — the
    # prime sweep must not touch it.
    await p._poll_tier("prime")
    assert p.reading.values.get(base_reg.name) == 0xBEEF, (
        "prime poll's eviction sweep must not retire base-tier values"
    )


async def test_short_read_counts_as_failure(tmp_path):
    """A truncated frame (fewer registers than requested, no isError) must
    be treated as a failure — never accepted as success, which would
    zero-extend the decode and read 'healthy' while telemetry froze."""
    from genwatch.modbus.client import SerialModbusClient

    client = SerialModbusClient(
        device="x", baud=9600, parity="N", stopbits=1, bytesize=8,
        timeout_s=0.1, slave=1, retries=0, backoff_s=[0.01],
    )

    class FakeRR:
        registers = [0x11]  # only 1 word for a 4-register request

        def isError(self):
            return False

    class FakeWire:
        async def read_holding_registers(self, address, count, slave):
            return FakeRR()

    client._client = FakeWire()
    r = await client.read(0x0010, 4, fc=3)
    assert not r.ok
    assert r.error == "short_read"


async def test_fanout_partial_failure_does_not_flip_comms_lost(tmp_path):
    """A few unreadable registers inside a fan-out must NOT flip comms to
    LOST. Health is sampled once per logical batch, not once per fan-out
    single — otherwise 3 bad registers in a row trip the 3-consecutive-
    failure LOST threshold while the rest of the block reads fine."""
    from dataclasses import dataclass
    from genwatch.modbus.client import ModbusResult
    from genwatch.modbus.poller import Poller
    from genwatch.modbus.registers import load_register_map

    regmap = load_register_map("genwatch/registers/h100.yaml")
    # Alarm registers (not state registers) — failing these must not LOSE comms.
    fail_addrs = {0x0083, 0x0084, 0x0085}

    @dataclass
    class FakeClient:
        async def connect(self): return True

        async def close(self): pass

        async def read(self, addr, count, fc=3):
            if count > 1:
                return ModbusResult.failure("batch", 1.0)
            if addr in fail_addrs:
                return ModbusResult.failure("addr", 1.0)
            return ModbusResult.success([0x4321], 1.0)

    p = Poller(FakeClient(), regmap, lambda *a: _noop())
    await p._poll_tier("prime")
    assert p.health.state != "lost", (
        "a handful of unreadable registers must not flip comms to LOST when "
        "the batch fan-out recovered the rest"
    )
    # State block (0x0082 / 0x0088) read fine → heartbeat stamped.
    assert p.health.last_prime_good_monotonic is not None


def test_comms_fast_recovery_after_clean_streak():
    """After an outage saturates the 60-sample window with failures, comms
    must fast-recover to 'healthy' on a short clean streak rather than
    waiting ~57 good polls to flush the window (~85 s at prime cadence).
    The degrade/lost thresholds are unchanged."""
    from genwatch.modbus.poller import HEALTHY_RECOVERY_STREAK, Poller
    from genwatch.modbus.registers import load_register_map

    class FakeClient:
        async def connect(self):
            return True

        async def close(self):
            pass

        async def read(self, addr, count, fc=3):
            raise AssertionError("read should not be called in this unit test")

    regmap = load_register_map("genwatch/registers/h100.yaml")
    p = Poller(FakeClient(), regmap, lambda *a: None)

    # Saturate the rolling window with failures → LOST, 0% success.
    for _ in range(60):
        p._record(False)
    assert p.health.state == "lost"
    assert p.health.success_pct == 0.0

    # A clean streak SHORTER than the threshold stays degraded — the
    # window is still almost all failures, so the success_pct path alone
    # would hold it degraded for ~85 s.
    for _ in range(HEALTHY_RECOVERY_STREAK - 1):
        p._record(True)
    assert p.health.state == "degraded"
    assert p.health.success_pct < 95

    # The streak-th consecutive success fast-recovers to healthy even
    # though the window still reads well under 95% — proving it was the
    # streak, not the flushed window.
    p._record(True)
    assert p.health.state == "healthy"
    assert p.health.success_pct < 95

    # A single failure drops it straight back out of healthy (degrade
    # hysteresis intact) and resets the streak.
    p._record(False)
    assert p.health.state == "degraded"
    assert p.health.consecutive_successes == 0


def test_comms_no_fast_recovery_while_flapping():
    """A flapping link (repeated short outages) must NOT keep fast-recovering
    to 'healthy' — that would flap authority and remote-control gating. The
    first clean reconnect fast-recovers; after a second LOST episode in the
    flap window, a short clean streak must stay 'degraded' and earn 'healthy'
    the slow (success_pct) way."""
    from genwatch.modbus.poller import HEALTHY_RECOVERY_STREAK, Poller
    from genwatch.modbus.registers import load_register_map

    class FakeClient:
        async def connect(self):
            return True

        async def close(self):
            pass

        async def read(self, addr, count, fc=3):
            raise AssertionError("read should not be called in this unit test")

    regmap = load_register_map("genwatch/registers/h100.yaml")
    p = Poller(FakeClient(), regmap, lambda *a: None)

    # Episode 1: a single outage then a clean streak → fast-recovers.
    for _ in range(3):
        p._record(False)
    assert p.health.state == "lost"
    for _ in range(HEALTHY_RECOVERY_STREAK):
        p._record(True)
    assert p.health.state == "healthy", "first clean reconnect should fast-recover"

    # Episode 2: it drops again — now the link is flapping.
    for _ in range(3):
        p._record(False)
    assert p.health.state == "lost"

    # A clean streak now must NOT fast-recover (two outages in the window);
    # the link stays 'degraded' until success_pct clears the hysteresis.
    for _ in range(HEALTHY_RECOVERY_STREAK + 2):
        p._record(True)
    assert p.health.state == "degraded", "flapping link must not fast-recover"
    assert p.health.success_pct < 95


async def test_heartbeat_withheld_when_state_block_fails(tmp_path):
    """The prime heartbeat must reflect engine-state detection, not just
    'some prime register was readable'. If output_status_1 (a state
    register) can't be decoded, the watchdog heartbeat is withheld even
    though unrelated prime registers (alarm count, etc.) read fine."""
    from dataclasses import dataclass
    from genwatch.modbus.client import ModbusResult
    from genwatch.modbus.poller import Poller
    from genwatch.modbus.registers import load_register_map

    regmap = load_register_map("genwatch/registers/h100.yaml")
    state_reg = regmap.by_name("output_status_1")
    assert state_reg is not None

    @dataclass
    class FakeClient:
        async def connect(self): return True

        async def close(self): pass

        async def read(self, addr, count, fc=3):
            if count > 1:
                return ModbusResult.failure("batch", 1.0)
            if addr == state_reg.addr:
                return ModbusResult.failure("state_addr", 1.0)
            return ModbusResult.success([0x0001], 1.0)

    p = Poller(FakeClient(), regmap, lambda *a: _noop())
    assert p.health.last_prime_good_monotonic is None
    await p._poll_tier("prime")
    # A far-flung prime single did decode, but the state register didn't —
    # so the heartbeat must stay unstamped (M1).
    assert p.reading.values.get("active_alarm_count") is not None
    assert p.health.last_prime_good_monotonic is None, (
        "heartbeat must be withheld when an engine-state register failed to decode"
    )


async def _noop():
    return None


async def test_modbus_read_releases_lock_between_retry_attempts(tmp_path):
    """A failing read must release the modbus client lock between
    retry attempts so a queued control write can pre-empt the retry
    chain. Without this, a degraded comms link holding the lock for
    ~5s across 3 failing attempts + backoffs would starve operator
    Stop commands exactly when they're most needed. Closes Modbus H3."""
    import asyncio
    import time as time_mod
    from genwatch.modbus.client import SerialModbusClient

    client = SerialModbusClient(
        device="/dev/null", baud=9600, parity="N",
        stopbits=1, bytesize=8, timeout_s=0.01,
        slave=1, retries=2,
        backoff_s=[0.05, 0.05],  # short backoffs for the test
    )
    # Hand-rolled fake pymodbus client: read always fails fast,
    # write returns immediately. The point of the test is that the
    # write doesn't wait for the read's full retry chain to finish.
    class FakeWire:
        async def read_holding_registers(self, *a, **kw):
            await asyncio.sleep(0.005)
            raise asyncio.TimeoutError
        async def read_input_registers(self, *a, **kw):
            return await self.read_holding_registers()
        async def write_register(self, **kw):
            return type("R", (), {"isError": lambda self: False})()
        async def write_registers(self, **kw):
            return type("R", (), {"isError": lambda self: False})()
    client._client = FakeWire()

    # Schedule a slow read and a write concurrently. The read will
    # take ~3 attempts × 0.015s sleep + 2 × 0.05s backoff ≈ 0.145s
    # if the lock is held throughout. With per-attempt locking the
    # write should slip in during one of the backoffs.
    read_done_at: list[float] = []
    write_done_at: list[float] = []

    async def do_read():
        await client.read(0x100, 1)
        read_done_at.append(time_mod.monotonic())

    async def do_write_after_tiny_delay():
        # Wait just long enough for the read's first attempt to be
        # in flight and for the lock to be released for backoff.
        await asyncio.sleep(0.02)
        await client.write(0x200, 1, fc=6)
        write_done_at.append(time_mod.monotonic())

    t0 = time_mod.monotonic()
    await asyncio.gather(do_read(), do_write_after_tiny_delay())
    write_elapsed = write_done_at[0] - t0
    read_elapsed = read_done_at[0] - t0

    # Write must complete before the read's full retry chain finishes.
    # If the lock were held throughout the read, write_elapsed would
    # be ~= read_elapsed. We expect a meaningful gap.
    assert write_elapsed < read_elapsed, (
        f"write should complete before the read's retry chain finishes "
        f"(write={write_elapsed*1000:.0f}ms, read={read_elapsed*1000:.0f}ms)"
    )


async def test_poller_apply_regmap_swaps_batches_and_cadence(tmp_path):
    """POST /api/registers/reload must update the live poller, not just
    app.state.regmap. Otherwise the operator edits the YAML, reloads,
    and the poller silently keeps reading the old addresses."""
    from copy import deepcopy
    from dataclasses import dataclass
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
