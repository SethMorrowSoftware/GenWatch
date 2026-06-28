"""Tests for the Slack notifier.

Covers:
  - SlackNotifier respects the per-event-type config flags
  - disabled / missing-token notifier is a no-op
  - test() returns (False, ...) when config is incomplete
  - queue + worker actually drain a message through to _post_slack
  - retries on transport failures, not on Slack 'ok: false' errors
  - PUT /api/config slack section hot-reloads the in-memory notifier
  - POST /api/slack/test returns the underlying ok/detail
  - GET /api/config never exposes the bot token
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import yaml

from genwatch.config import SlackConfig
from genwatch.db import Database
from genwatch.main import create_app
from genwatch.services.auth import hash_password
from genwatch.services import slack as slack_mod
from genwatch.services.slack import SlackNotifier, _build_blocks


# ─── Block builder ────────────────────────────────────────────────────────


def test_build_blocks_uses_title_and_fields():
    blocks = _build_blocks(
        severity="alarm",
        title=":rotating_light: test",
        fields=[("Code", "0x42"), ("Site", "SITE-1")],
    )
    # first block: title section
    assert blocks[0]["type"] == "section"
    assert "test" in blocks[0]["text"]["text"]
    # second block: fields
    assert blocks[1]["type"] == "section"
    field_texts = [f["text"] for f in blocks[1]["fields"]]
    assert any("Code" in t and "0x42" in t for t in field_texts)
    # context block last
    assert blocks[-1]["type"] == "context"
    assert "alarm" in blocks[-1]["elements"][0]["text"]


def test_build_blocks_chunks_more_than_ten_fields():
    fields = [(f"k{i}", f"v{i}") for i in range(25)]
    blocks = _build_blocks(severity="info", title="t", fields=fields)
    # title + ceil(25/10)=3 field blocks + context = 5
    field_blocks = [b for b in blocks if b.get("type") == "section" and "fields" in b]
    assert len(field_blocks) == 3
    assert sum(len(b["fields"]) for b in field_blocks) == 25


# ─── Enabled / disabled gating ────────────────────────────────────────────


def test_notifier_disabled_when_missing_token(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="")
    n = SlackNotifier(cfg, db)
    assert n.is_enabled() is False


def test_notifier_disabled_when_missing_channel(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    assert n.is_enabled() is False


def test_notifier_disabled_when_flag_off(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=False, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    assert n.is_enabled() is False


def test_notifier_enabled_with_full_config(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    assert n.is_enabled() is True


# ─── Event-flag gating ────────────────────────────────────────────────────


async def test_alert_alarm_respects_flag(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(
        enabled=True, channel="#x", bot_token="xoxb-test",
        alert_on_alarm=False, alert_on_warning=True,
    )
    n = SlackNotifier(cfg, db)

    posted: list[dict] = []

    def fake_post(token, payload, timeout):
        posted.append(payload)
        return True, "ok"

    monkeypatch.setattr(slack_mod, "_post_slack", fake_post)
    await n.start()
    try:
        await n.alert_alarm("0x42", "Low Oil", "alarm", ts=0)
        await n.alert_alarm("0x60", "Low Battery", "warn", ts=0)
        # let the worker drain
        for _ in range(20):
            if posted:
                break
            await asyncio.sleep(0.05)
    finally:
        await n.stop()

    # alarm-severity was suppressed; warn was sent
    assert len(posted) == 1
    assert "Low Battery" in posted[0]["text"]


async def test_alert_state_change_suppressed_by_default(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)

    posted: list[dict] = []
    monkeypatch.setattr(
        slack_mod,
        "_post_slack",
        lambda *a, **kw: (posted.append(a[1]) or (True, "ok"))[1],
    )
    await n.start()
    try:
        await n.alert_state_change("running", "cooling", ts=0)
        await asyncio.sleep(0.15)
    finally:
        await n.stop()
    # Default config sets alert_on_state_change=False → no post
    assert posted == []


async def test_comms_change_suppresses_healthy_to_degraded(tmp_path, monkeypatch):
    """healthy→degraded is noisy jitter; only fire on lost or recovered."""
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test", alert_on_comms_lost=True)
    n = SlackNotifier(cfg, db)

    posted: list[dict] = []
    monkeypatch.setattr(slack_mod, "_post_slack", lambda t, p, to: (posted.append(p) or True, "ok"))
    await n.start()
    try:
        # this transition should be suppressed
        await n.alert_comms_change("healthy", "degraded", success_pct=80.0, ts=0)
        # this one should fire
        await n.alert_comms_change("degraded", "lost", success_pct=10.0, ts=0)
        # and recovery
        await n.alert_comms_change("lost", "healthy", success_pct=99.5, ts=0)
        for _ in range(20):
            if len(posted) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await n.stop()
    assert len(posted) == 2
    texts = [p["text"] for p in posted]
    assert any("lost" in t for t in texts)
    assert any("healthy" in t for t in texts)


# ─── Worker dispatch ──────────────────────────────────────────────────────


async def test_worker_sends_command_event(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db, site_name="Test Site")

    calls: list[tuple[str, dict]] = []

    def fake_post(token, payload, timeout):
        calls.append((token, payload))
        return True, "ok"

    monkeypatch.setattr(slack_mod, "_post_slack", fake_post)
    await n.start()
    try:
        await n.alert_command("start", "alice", "ok", ts=0)
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0.05)
    finally:
        await n.stop()
    assert len(calls) == 1
    token, payload = calls[0]
    assert token == "xoxb-test"
    assert payload["channel"] == "#x"
    assert "start" in payload["text"]
    assert "alice" in payload["text"]
    assert "Test Site" in payload["text"]


async def test_worker_does_not_retry_on_slack_error(tmp_path, monkeypatch, caplog):
    """A `{"ok": false}` from Slack is a config issue — don't retry."""
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    n.BACKOFF_S = (0.01, 0.01, 0.01)  # speed up if retry leaks through

    attempts = []

    def fake_post(token, payload, timeout):
        attempts.append(payload)
        return False, "slack_error channel_not_found"

    monkeypatch.setattr(slack_mod, "_post_slack", fake_post)
    await n.start()
    try:
        await n.alert_command("start", "op", "ok", ts=0)
        await asyncio.sleep(0.3)
    finally:
        await n.stop()
    assert len(attempts) == 1, "should not retry on slack-side error"


async def test_alarm_dedupe_suppresses_repeats_inside_window(tmp_path, monkeypatch):
    """A flapping alarm (raise → clear → raise → clear within a few
    seconds) should fire Slack only once per direction per dedupe
    window. Otherwise a chattery alarm bit can exhaust the 200-slot
    queue and push real alerts off the floor."""
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(
        enabled=True, channel="#x", bot_token="xoxb-test",
        alert_on_alarm=True, alert_on_alarm_cleared=True,
    )
    n = SlackNotifier(cfg, db)

    posted: list[dict] = []
    monkeypatch.setattr(
        slack_mod, "_post_slack",
        lambda t, p, to: (posted.append(p) or (True, "ok"))[1],
    )
    await n.start()
    try:
        # Same code raised 3 times in rapid succession — only the
        # first should land in Slack.
        await n.alert_alarm("LOW_OIL", "Low Oil", "alarm", ts=0)
        await n.alert_alarm("LOW_OIL", "Low Oil", "alarm", ts=0)
        await n.alert_alarm("LOW_OIL", "Low Oil", "alarm", ts=0)
        # A different code is NOT deduped against LOW_OIL.
        await n.alert_alarm("HIGH_TEMP", "High Temp", "alarm", ts=0)
        # And cleared events live in their own key — same code,
        # different kind, no dedupe collision.
        await n.alert_alarm_cleared("LOW_OIL", "Low Oil", ts=0)
        await n.alert_alarm_cleared("LOW_OIL", "Low Oil", ts=0)  # deduped
        for _ in range(40):
            if len(posted) >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        await n.stop()
    # Expect: 1 LOW_OIL raise + 1 HIGH_TEMP raise + 1 LOW_OIL clear = 3
    assert len(posted) == 3, [p["text"] for p in posted]
    texts = " ".join(p["text"] for p in posted)
    # Two LOW_OIL events (one raise, one clear) — distinguished by kind.
    assert texts.count("LOW_OIL") == 2
    assert "HIGH_TEMP" in texts


async def test_alarm_dedupe_window_expires(tmp_path, monkeypatch):
    """After DEDUPE_WINDOW_S elapses, a repeat alarm fires again."""
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test", alert_on_alarm=True)
    n = SlackNotifier(cfg, db)
    n.DEDUPE_WINDOW_S = 0.05  # very short for the test

    posted: list[dict] = []
    monkeypatch.setattr(
        slack_mod, "_post_slack",
        lambda t, p, to: (posted.append(p) or (True, "ok"))[1],
    )
    await n.start()
    try:
        await n.alert_alarm("LOW_OIL", "Low Oil", "alarm", ts=0)
        # Inside the window — dropped
        await n.alert_alarm("LOW_OIL", "Low Oil", "alarm", ts=0)
        # Wait past the window — next call fires again
        await asyncio.sleep(0.1)
        await n.alert_alarm("LOW_OIL", "Low Oil", "alarm", ts=0)
        for _ in range(40):
            if len(posted) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await n.stop()
    assert len(posted) == 2


async def test_worker_abandons_messages_past_max_age(tmp_path, monkeypatch):
    """Per-message wall-clock deadline: a sustained Slack outage stops
    producing retry tasks for stale messages so newer (more urgent)
    alerts can still reach the channel when comms recover."""
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    n.BACKOFF_S = (0.01, 0.01, 0.01)
    n.MAX_ATTEMPTS = 10  # lots of retries available
    n.MAX_AGE_S = 0.05   # but the deadline is shorter

    attempts = []

    def fake_post(token, payload, timeout):
        attempts.append(time.time())
        return False, "url_error"  # transport error → triggers retry path

    monkeypatch.setattr(slack_mod, "_post_slack", fake_post)
    await n.start()
    try:
        await n.alert_command("start", "op", "ok", ts=0)
        # Wait for the worker to give up. Total time bounded by
        # MAX_AGE_S + a few backoffs of slack.
        await asyncio.sleep(0.4)
    finally:
        await n.stop()
    # Without MAX_AGE_S we'd see MAX_ATTEMPTS attempts (10). With the
    # deadline the worker abandons partway through. We just need to
    # see fewer than MAX_ATTEMPTS to prove the deadline fired.
    assert 0 < len(attempts) < 10, f"expected early abandon, got {len(attempts)} attempts"


async def test_worker_retries_on_transport_error(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    n.BACKOFF_S = (0.05, 0.05, 0.05)
    n.MAX_ATTEMPTS = 3

    counter = {"n": 0}

    def fake_post(token, payload, timeout):
        counter["n"] += 1
        if counter["n"] < 3:
            return False, "url_error"
        return True, "ok"

    monkeypatch.setattr(slack_mod, "_post_slack", fake_post)
    await n.start()
    try:
        await n.alert_command("start", "op", "ok", ts=0)
        for _ in range(40):
            if counter["n"] >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        await n.stop()
    assert counter["n"] == 3


# ─── test() endpoint helper ───────────────────────────────────────────────


async def test_test_returns_false_when_missing_token(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="")
    n = SlackNotifier(cfg, db)
    ok, msg = await n.test()
    assert ok is False
    assert "bot_token" in msg


async def test_test_returns_false_when_missing_channel(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    ok, msg = await n.test()
    assert ok is False
    assert "channel" in msg


async def test_test_returns_true_on_success(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#x", bot_token="xoxb-test")
    n = SlackNotifier(cfg, db)
    monkeypatch.setattr(slack_mod, "_post_slack", lambda t, p, to: (True, "ok"))
    ok, msg = await n.test()
    assert ok is True
    assert msg == "ok"


# ─── API integration ──────────────────────────────────────────────────────


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    # Point at an empty config.yaml so PUT /api/config has a path to write to.
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("mock: true\n")
    monkeypatch.setenv("GENWATCH_CONFIG_PATH", str(cfg_file))
    yield cfg_file


@pytest.fixture
async def client(app_env):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
        headers={"X-Requested-With": "pytest"},  # compliant client (M-8)
    ) as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)
            yield c, app


async def _login(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200, r.text


async def test_get_config_never_exposes_bot_token(client):
    c, _app = client
    await _login(c)
    # set a token via PUT
    r = await c.put("/api/config", json={"slack": {"bot_token": "xoxb-secret"}})
    assert r.status_code == 200, r.text

    # GET must not include the raw token
    r = await c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    blob = json.dumps(body)
    assert "xoxb-secret" not in blob
    assert body["slack"]["botTokenConfigured"] is True


async def test_put_slack_hot_reloads_notifier(client, app_env):
    c, app = client
    await _login(c)
    r = await c.put(
        "/api/config",
        json={
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-new",
                "channel": "#new",
                "alert_on_state_change": True,
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slack_updated"] is True
    assert body["restart_required"] is False

    # in-memory notifier picked up the change
    n = app.state.slack
    assert n.cfg.enabled is True
    assert n.cfg.bot_token == "xoxb-new"
    assert n.cfg.channel == "#new"
    assert n.cfg.alert_on_state_change is True

    # YAML on disk reflects the change
    on_disk = yaml.safe_load(app_env.read_text())
    assert on_disk["slack"]["bot_token"] == "xoxb-new"
    assert on_disk["slack"]["channel"] == "#new"


async def test_slack_test_endpoint_reports_detail(client, monkeypatch):
    c, _app = client
    await _login(c)
    # configure first
    await c.put(
        "/api/config",
        json={"slack": {"enabled": True, "bot_token": "xoxb-t", "channel": "#x"}},
    )
    # stub the actual HTTP call
    monkeypatch.setattr(
        slack_mod, "_post_slack",
        lambda token, payload, timeout: (False, "slack_error invalid_auth"),
    )
    r = await c.post("/api/slack/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "invalid_auth" in body["detail"]


async def test_slack_test_requires_admin(client):
    c, _app = client
    # no login → 401
    r = await c.post("/api/slack/test")
    assert r.status_code == 401


async def test_put_serial_still_requires_restart(client):
    """Regression: non-Slack updates must still report restart_required."""
    c, _app = client
    await _login(c)
    r = await c.put("/api/config", json={"serial": {"baud": 19200}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restart_required"] is True
    assert body["slack_updated"] is False


async def test_bot_token_does_not_leak_to_audit_log(client, app_env):
    """The bot_token must be sanitized in the audit detail."""
    c, app = client
    await _login(c)
    r = await c.put(
        "/api/config",
        json={"slack": {"bot_token": "xoxb-very-secret-12345"}},
    )
    assert r.status_code == 200, r.text
    # Peek at the audit table
    with app.state.db._writer() as cur:
        rows = cur.execute(
            "SELECT action, detail FROM audit WHERE action='config.update' ORDER BY id DESC LIMIT 5"
        ).fetchall()
    assert rows, "no audit row written"
    for row in rows:
        assert "xoxb-very-secret-12345" not in (row["detail"] or "")


# ─── State machine integration ────────────────────────────────────────────


async def test_state_machine_emits_comms_event(tmp_path):
    """The state machine should now emit a 'comms' event on transition."""
    from genwatch.modbus.poller import CommsHealth, Reading
    from genwatch.modbus.registers import load_register_map
    from genwatch.services.state import EventBus, StateMachine

    db = Database(tmp_path / "t.sqlite")
    regmap = load_register_map(
        Path(__file__).parent.parent / "genwatch" / "registers" / "h100.yaml"
    )
    sm = StateMachine(regmap, db, EventBus())
    # First poll establishes baseline (healthy)
    sm.update(Reading(values={"engine_state": 0}), CommsHealth(state="healthy"))
    # Now transition to degraded
    emitted = sm.update(
        Reading(values={"engine_state": 0}),
        CommsHealth(state="lost", success_pct=12.0),
    )
    comms_events = [e for e in emitted if e["type"] == "comms"]
    assert len(comms_events) == 1
    e = comms_events[0]
    assert e["from"] == "healthy"
    assert e["to"] == "lost"
    assert e["successPct"] == 12.0


async def test_state_machine_alarm_cleared_includes_desc(tmp_path):
    """alarm-cleared events should now include the description.

    Uses the H-100 bitfield alarm model: setting Coolant Temp High Alarm
    (output_status_2 bit 0x1000) and then clearing it should emit a
    transition + an alarm-cleared event with the full description.
    """
    from genwatch.modbus.poller import CommsHealth, Reading
    from genwatch.modbus.registers import load_register_map
    from genwatch.services.state import EventBus, StateMachine

    db = Database(tmp_path / "t.sqlite")
    regmap = load_register_map(
        Path(__file__).parent.parent / "genwatch" / "registers" / "h100.yaml"
    )
    sm = StateMachine(regmap, db, EventBus())

    base = {"output_status_1": 0x0100, "output_status_2": 0, "output_status_7": 0}  # stopped, no alarm
    sm.update(Reading(values=dict(base)), CommsHealth())
    raised = dict(base)
    raised["output_status_2"] = 0x1000  # Coolant Temp High Alarm
    sm.update(Reading(values=raised), CommsHealth())
    emitted = sm.update(Reading(values=dict(base)), CommsHealth())
    cleared = [e for e in emitted if e["type"] == "alarm-cleared"]
    assert len(cleared) == 1
    assert cleared[0]["code"] == "COOLANT_TEMP_HIGH_ALARM"
    assert "Coolant" in cleared[0]["desc"]
