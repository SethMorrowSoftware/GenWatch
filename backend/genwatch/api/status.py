"""GET /api/status — full live snapshot for the UI on mount."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from ..db import COLUMN_MAP

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict:
    st = request.app.state
    sm = st.state_machine
    snap = sm.snap
    reading = snap.last_reading
    regmap = st.regmap

    # Build a flat reading suitable for the UI. We expose canonical
    # camelCase keys matching the prototype's design.
    r = reading.values
    out = {
        "state": snap.engine_state,
        "alarmRaw": snap.alarm_raw,
        "timeInState": snap.time_in_state_s,
        "stateStartedAt": snap.state_started_at,
        "comms": {
            "state": snap.comms.state,
            "successPct": snap.comms.success_pct,
            "lastGoodAt": snap.comms.last_good_at,
            "rateMs": snap.comms.rate_ms,
            "p95LatencyMs": snap.comms.p95_latency_ms,
        },
        "reading": {
            "rpm": r.get("rpm"),
            "hz": r.get("frequency"),
            "kw": r.get("total_kw"),
            "oilP": r.get("oil_pressure"),
            "coolT": r.get("coolant_temp"),
            "batt": r.get("battery_volts"),
            "vAB": r.get("gen_voltage_ab"),
            "vBC": r.get("gen_voltage_bc"),
            "vCA": r.get("gen_voltage_ca"),
            "iA": r.get("gen_current_a"),
            "iB": r.get("gen_current_b"),
            "iC": r.get("gen_current_c"),
            "fuelPct": r.get("fuel_level_pct"),
            "runHours": r.get("run_hours"),
            "startCount": r.get("start_count"),
        },
        "site": {
            "id": regmap.site.id,
            "name": regmap.site.name,
            "ratingKw": regmap.site.rating_kw,
            "engine": regmap.site.engine,
            "tankGal": regmap.site.tank_gal,
        },
        "exercise": {
            "enabled": regmap.site.exercise_enabled,
            "day": regmap.site.exercise_day,
            "time": regmap.site.exercise_time,
            "durationMin": regmap.site.exercise_duration_min,
        },
        "activeAlarms": st.db.active_alarms(),
        "hts": {
            "transferredToGen": snap.engine_state in ("running", "exercising"),
        },
        "serverTs": time.time(),
    }
    return out


@router.get("/health")
async def health(request: Request) -> dict:
    st = request.app.state
    db_bytes = st.db.disk_usage_bytes()
    return {
        "ok": True,
        "comms": st.state_machine.snap.comms.state,
        "engineState": st.state_machine.snap.engine_state,
        "dbBytes": db_bytes,
        "mock": st.settings.mock,
        "version": st.version,
    }


@router.get("/columns")
async def columns() -> dict:
    """Expose the telemetry column map so the frontend can render any
    metric without hard-coding the names."""
    return {"columns": COLUMN_MAP}
