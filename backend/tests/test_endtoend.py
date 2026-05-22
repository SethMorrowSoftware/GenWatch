"""End-to-end smoke test using the mock Modbus client.

Boots the FastAPI app with mock=True, drives the poller for a few
seconds, then verifies that /api/status returns plausible data and the
control flow accepts a valid token + rejects a bad one.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import httpx
import pytest

from genwatch.config import load
from genwatch.main import create_app
from genwatch.services.auth import hash_password


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
        # Trigger lifespan startup
        async with app.router.lifespan_context(app):
            # Give the poller a moment to do a base read
            await asyncio.sleep(0.3)
            yield c


async def _login(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200, r.text


async def test_status_returns_live_snapshot(client):
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] in {"stopped", "cranking", "running", "exercising", "cooling", "alarm", "unknown"}
    assert "reading" in body
    assert "comms" in body
    assert body["comms"]["state"] in {"healthy", "degraded", "lost"}


async def test_health_endpoint(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mock"] is True


async def test_login_required_for_control(client):
    r = await client.post("/api/control/start", json={"confirm_token": "deadbeef"})
    assert r.status_code == 401


async def test_full_control_flow(client):
    await _login(client)

    # Engine should be stopped (mock default). Issue token, then start.
    r = await client.get("/api/control/confirm")
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    r = await client.post("/api/control/start", json={"confirm_token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["verb"] == "start"

    # token is single-use — replay must 400
    r = await client.post("/api/control/start", json={"confirm_token": token})
    assert r.status_code == 400


async def test_invalid_token_rejected(client):
    await _login(client)
    r = await client.post("/api/control/start", json={"confirm_token": "BADTOKEN"})
    assert r.status_code == 400
    assert "token" in r.json()["detail"]["code"]


async def test_state_validity_enforced(client):
    """Cannot start while the engine is already running."""
    await _login(client)
    # Drive the mock into 'running' by calling start once
    r = await client.get("/api/control/confirm")
    token = r.json()["token"]
    await client.post("/api/control/start", json={"confirm_token": token})

    # Allow a poll or two so the state machine catches the transition
    await asyncio.sleep(3.5)

    r = await client.get("/api/status")
    state = r.json()["state"]
    if state in ("cranking", "running"):
        r = await client.get("/api/control/confirm")
        token = r.json()["token"]
        r = await client.post("/api/control/start", json={"confirm_token": token})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "invalid_state"
