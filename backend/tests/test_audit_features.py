"""Tests for the post-genmon-audit feature set:
  - Pi system health endpoint
  - Maintenance journal (schedule, log, due)
  - Utility outage tracking
  - CSV exports (telemetry, events, audit)
  - Config backup/restore
  - Fuel consumption estimator
"""
from __future__ import annotations

import asyncio
import io
import tarfile
import time

import httpx
import pytest

from genwatch.db import Database
from genwatch.main import create_app
from genwatch.services import maintenance as maint
from genwatch.services.auth import hash_password
from genwatch.services.fuel import MODELS, FuelAccumulator
from genwatch.services.outage import OutageTracker, list_outages, outage_summary
from genwatch.services.system_health import collect


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    # Each test gets a fresh on-disk config + DB.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "mock: true\n"
        "auth:\n"
        f"  admin_password_hash: \"{hash_password('test')}\"\n"
        f"  jwt_secret: \"{'x' * 64}\"\n"
        f"data_dir: \"{tmp_path}\"\n"
    )
    monkeypatch.setenv("GENWATCH_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    yield cfg_path


@pytest.fixture
async def client(app_env):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)  # let poller stabilise
            yield c, app


async def _login(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/auth/login", json={"password": "test"})
    assert r.status_code == 200, r.text


# ─── Pi system health ──────────────────────────────────────────────────


def test_system_health_collect_does_not_crash(tmp_path):
    """All probes degrade gracefully when files are missing."""
    snap = collect(str(tmp_path), time.time())
    # Disk should always be readable for an existing dir.
    assert snap.disk_total_mb is None or snap.disk_total_mb > 0
    # Throttled flags list is always present (possibly empty).
    assert isinstance(snap.throttled_flags, list)
    d = snap.to_ui()
    assert "cpuTempC" in d
    assert "memUsedPct" in d
    assert "diskUsedPct" in d


async def test_system_endpoint_requires_auth(client):
    c, _ = client
    r = await c.get("/api/system")
    assert r.status_code == 401


async def test_system_endpoint_authed(client):
    c, _ = client
    await _login(c)
    r = await c.get("/api/system")
    assert r.status_code == 200
    body = r.json()
    assert "cpuTempC" in body
    assert "procUptimeS" in body
    assert isinstance(body["throttledFlags"], list)


# ─── Maintenance journal ───────────────────────────────────────────────


def test_default_schedule_seeded(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    n = maint.seed_default_schedule(db)
    assert n > 0  # seeded on first call
    # Idempotent: second call is a no-op
    assert maint.seed_default_schedule(db) == 0
    items = maint.list_schedule(db)
    assert any(it.kind == "oil_change" for it in items)


def test_maintenance_log_and_due(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    maint.seed_default_schedule(db)

    # Compute due before any log entries — every item should be 'never'.
    due = maint.compute_due(db, current_run_hours=100.0)
    kinds = {d.kind: d.status for d in due}
    assert all(s == "never" for s in kinds.values())

    # Add a fresh oil change at 100 hours. Status should flip to 'ok'.
    maint.add_log_entry(
        db, kind="oil_change", run_hours_at=100.0,
        performed_by="tester", notes="initial",
    )
    due = maint.compute_due(db, current_run_hours=110.0, now=time.time())
    oil = next(d for d in due if d.kind == "oil_change")
    assert oil.status == "ok"
    assert oil.hours_since == 10.0

    # Fast-forward run hours past the 250-hour interval → overdue.
    due = maint.compute_due(db, current_run_hours=400.0, now=time.time())
    oil = next(d for d in due if d.kind == "oil_change")
    assert oil.status == "overdue"
    assert oil.hours_to_next <= 0


def test_maintenance_due_handles_calendar_only_items(tmp_path):
    """battery_test has interval_hours=0 — should still go overdue on time."""
    db = Database(tmp_path / "db.sqlite")
    maint.seed_default_schedule(db)
    # Record 200 days ago — past the 180-day interval.
    past = time.time() - 200 * 86400
    maint.add_log_entry(db, kind="battery_test", run_hours_at=None, ts=past)
    due = maint.compute_due(db, current_run_hours=None)
    bt = next(d for d in due if d.kind == "battery_test")
    assert bt.status == "overdue"


async def test_maintenance_api(client):
    c, _ = client
    await _login(c)

    r = await c.get("/api/maintenance/schedule")
    assert r.status_code == 200
    body = r.json()
    assert any(s["kind"] == "oil_change" for s in body["schedule"])

    r = await c.get("/api/maintenance/due")
    assert r.status_code == 200
    due = r.json()
    assert "items" in due

    # Add a maintenance log entry.
    r = await c.post(
        "/api/maintenance/log",
        json={"kind": "oil_change", "run_hours_at": 100.0, "notes": "test"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Now due-state for oil_change should not be 'never'.
    r = await c.get("/api/maintenance/due")
    items = {d["kind"]: d for d in r.json()["items"]}
    assert items["oil_change"]["status"] != "never"


async def test_maintenance_schedule_upsert_admin_only(client):
    c, _ = client
    # Without auth → 401
    r = await c.put(
        "/api/maintenance/schedule",
        json={"kind": "custom", "interval_hours": 500, "interval_days": 0},
    )
    assert r.status_code == 401
    await _login(c)
    r = await c.put(
        "/api/maintenance/schedule",
        json={"kind": "custom", "interval_hours": 500, "interval_days": 0},
    )
    assert r.status_code == 200


# ─── Outage tracker ────────────────────────────────────────────────────


def test_outage_tracker_open_and_close(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    tr = OutageTracker(db)
    # Engine starts due to utility outage (panel in AUTO).
    evt = tr.on_transition(from_state="stopped", to_state="cranking",
                           panel_mode="auto", ts=1000.0)
    assert evt is None  # cranking isn't OUTAGE_STATES
    evt = tr.on_transition(from_state="cranking", to_state="running",
                           panel_mode="auto", ts=1010.0)
    assert evt is not None and evt["type"] == "outage-started"
    assert tr.open_id is not None

    # Integrate kW samples — peak + kwh should grow.
    tr.on_sample(1010.0, kw=10.0)
    tr.on_sample(1610.0, kw=50.0)  # 10 minutes later
    tr.on_sample(2210.0, kw=80.0)  # 10 minutes later

    # Engine returns to stopped → outage closes.
    evt = tr.on_transition(from_state="running", to_state="cooling",
                           panel_mode="auto", ts=2810.0)
    assert evt is not None and evt["type"] == "outage-ended"
    assert tr.open_id is None

    rows = list_outages(db)
    assert len(rows) == 1
    o = rows[0]
    assert o.ended_ts is not None
    assert o.duration_s == pytest.approx(1800.0, rel=0.01)
    assert o.peak_kw == 80.0
    assert o.kwh > 0  # ~25 kWh from the trapezoidal integration


def test_outage_not_opened_when_panel_manual(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    tr = OutageTracker(db)
    # MANUAL means an operator is running the gen on purpose — not an outage.
    evt = tr.on_transition(from_state="stopped", to_state="running",
                           panel_mode="manual", ts=1000.0)
    assert evt is None
    assert tr.open_id is None


def test_outage_reattach_after_restart(tmp_path):
    """If the service dies while an outage is open, the next boot picks
    it up rather than orphaning the row."""
    db = Database(tmp_path / "db.sqlite")
    tr = OutageTracker(db)
    tr.on_transition(from_state="stopped", to_state="running",
                     panel_mode="auto", ts=1000.0)
    open_id = tr.open_id
    assert open_id is not None

    # Simulate a service restart — fresh tracker, same DB.
    tr2 = OutageTracker(db)
    assert tr2.open_id == open_id

    # Closing now should work normally.
    evt = tr2.on_transition(from_state="running", to_state="stopped",
                            panel_mode="auto", ts=2000.0)
    assert evt is not None and evt["type"] == "outage-ended"


def test_outage_summary(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    tr = OutageTracker(db)
    tr.on_transition(from_state="stopped", to_state="running",
                     panel_mode="auto", ts=time.time() - 600)
    tr.on_sample(time.time() - 600, kw=20.0)
    tr.on_sample(time.time() - 60, kw=30.0)
    tr.on_transition(from_state="running", to_state="stopped",
                     panel_mode="auto", ts=time.time())
    s = outage_summary(db, days=30)
    assert s["count"] == 1
    assert s["completed"] == 1
    assert s["totalDurationS"] > 0


async def test_outage_api_lists(client):
    c, app = client
    await _login(c)
    r = await c.get("/api/outages")
    assert r.status_code == 200
    body = r.json()
    assert "outages" in body
    assert "summary30d" in body
    assert "summary365d" in body


# ─── CSV export ────────────────────────────────────────────────────────


async def test_telemetry_export_streams_csv(client):
    c, app = client
    await _login(c)
    # Mock client should have generated a few rows by now; force a write
    # so we have at least one regardless of poll timing.
    db = app.state.db
    db.write_telemetry(
        ts=time.time(),
        values={"frequency": 60.0, "total_kw": 12.3, "rpm": 1800.0},
        state="running",
        alarm_raw=0,
    )
    r = await c.get("/api/telemetry/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.text
    # BOM + header row
    assert text.startswith("﻿") or "ts" in text.lower()
    assert "iso_ts" in text
    assert "epoch" in text


async def test_events_export(client):
    c, app = client
    await _login(c)
    app.state.db.write_event(severity="info", type_="TEST", message="hello")
    r = await c.get("/api/events/export")
    assert r.status_code == 200
    assert "hello" in r.text


async def test_export_window_validation(client):
    c, _ = client
    await _login(c)
    # to < from is rejected.
    r = await c.get("/api/telemetry/export?from=2000&to=1000")
    assert r.status_code == 400


async def test_export_requires_operator(client):
    c, _ = client
    r = await c.get("/api/telemetry/export")
    assert r.status_code == 401


# ─── Backup / restore ──────────────────────────────────────────────────


async def test_backup_download_returns_tarball(client):
    c, _ = client
    await _login(c)
    r = await c.get("/api/backup/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    # Verify the tarball contains config.yaml + a manifest.
    buf = io.BytesIO(r.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tf:
        names = tf.getnames()
        assert "genwatch/config.yaml" in names
        assert "genwatch/MANIFEST.yaml" in names


async def test_backup_requires_admin(client):
    c, _ = client
    r = await c.get("/api/backup/download")
    assert r.status_code == 401


async def test_backup_restore_roundtrip(client, tmp_path):
    c, _ = client
    await _login(c)
    # Download
    r = await c.get("/api/backup/download")
    assert r.status_code == 200
    tarball = r.content

    # Restore — should accept the same payload.
    files = {"file": ("backup.tar.gz", tarball, "application/gzip")}
    r = await c.post("/api/backup/restore", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["restart_required"] is True


async def test_backup_restore_rejects_path_traversal(client):
    """Crafted tarball with ../../../etc/passwd must be silently skipped."""
    c, _ = client
    await _login(c)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        evil = tarfile.TarInfo(name="../../../etc/passwd")
        evil.size = 4
        tf.addfile(evil, io.BytesIO(b"haha"))
    buf.seek(0)
    files = {"file": ("evil.tar.gz", buf.getvalue(), "application/gzip")}
    r = await c.post("/api/backup/restore", files=files)
    # Restore returns ok=False (no files extracted) — the path-traversal
    # member was filtered.
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ─── Fuel consumption ──────────────────────────────────────────────────


def test_fuel_models_load_curve():
    diesel = MODELS["diesel"]
    # Idle: 0.6 gph
    assert diesel.estimate(0) == pytest.approx(0.6)
    # At 100 kW: 0.6 + 0.07 * 100 = 7.6 gph
    assert diesel.estimate(100) == pytest.approx(7.6, rel=0.01)
    # None / negative kw treated as idle (engine running, no load).
    assert diesel.estimate(None) == pytest.approx(0.6)
    assert diesel.estimate(-5) == pytest.approx(0.6)


def test_fuel_accumulator_integration():
    accum = FuelAccumulator(model=MODELS["diesel"])
    # First sample establishes baseline only — no fuel burned yet.
    accum.update(ts=1000.0, kw=0.0, engine_running=True)
    assert accum.lifetime_units == 0.0

    # Second sample: 1 hour later at 100 kW.
    accum.update(ts=1000.0 + 3600.0, kw=100.0, engine_running=True)
    # Trapezoidal: avg of (0.6 gph idle, 7.6 gph at 100 kW) × 1 h = 4.1 gph × 1 = 4.1 gal
    assert accum.lifetime_units == pytest.approx(4.1, rel=0.01)
    assert accum.day_units == pytest.approx(4.1, rel=0.01)


def test_fuel_accumulator_engine_off_no_burn():
    accum = FuelAccumulator(model=MODELS["diesel"])
    accum.update(ts=1000.0, kw=0.0, engine_running=False)
    accum.update(ts=1000.0 + 3600.0, kw=0.0, engine_running=False)
    assert accum.lifetime_units == 0.0


def test_fuel_accumulator_projected_hours():
    accum = FuelAccumulator(model=MODELS["diesel"])
    accum.update(ts=1000.0, kw=100.0, engine_running=True)
    accum.update(ts=1000.0 + 10.0, kw=100.0, engine_running=True)
    # last_rate ~7.6 gph. With 76 gal in tank: 10 hours.
    assert accum.projected_hours_remaining(76.0) == pytest.approx(10.0, rel=0.01)
    # Engine off → None
    accum.last_engine_running = False
    assert accum.projected_hours_remaining(76.0) is None


async def test_fuel_endpoint(client):
    c, _ = client
    await _login(c)
    r = await c.get("/api/fuel")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["fuelType"] == "diesel"
    assert body["unit"] == "gph"
    assert "rateNow" in body
    assert "tankGal" in body
