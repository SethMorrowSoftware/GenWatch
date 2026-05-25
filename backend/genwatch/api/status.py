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
    db = st.db

    # Build a flat reading suitable for the UI. We expose canonical
    # camelCase keys matching the prototype's design.
    r = reading.values

    # Engine starts: prefer the H-100 register if the map ever exposes
    # it; otherwise derive from the TRANSITION event stream so the UI
    # still has a real number to show.
    start_count = r.get("start_count")
    if start_count is None:
        start_count = db.count_engine_starts()

    # ATS history (no contact register on H-100 map → derive from state
    # transitions into 'running').
    last_xfer = db.last_transfer_to_gen()
    thirty_days_ago = time.time() - 30 * 86400
    xfer_30d = db.count_transfers_since(thirty_days_ago)

    last_alarm = db.last_alarm_event()

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
            "startCount": start_count,
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
        "activeAlarms": db.active_alarms(),
        "hts": {
            "transferredToGen": snap.engine_state in ("running", "exercising"),
            "lastTransferTs": last_xfer["ts"] if last_xfer else None,
            "transfers30d": xfer_30d,
        },
        "lastAlarm": (
            {
                "ts": last_alarm["ts"],
                "severity": last_alarm["severity"],
                "message": last_alarm["message"],
            } if last_alarm else None
        ),
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
        "uptimeS": int(time.time() - st.started_at),
        "dbBytes": db_bytes,
        "mock": st.settings.mock,
        "version": st.version,
    }


@router.get("/columns")
async def columns() -> dict:
    """Expose the telemetry column map so the frontend can render any
    metric without hard-coding the names."""
    return {"columns": COLUMN_MAP}
