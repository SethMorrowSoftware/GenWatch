"""GET /api/status — full live snapshot for the UI on mount."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from ..db import COLUMN_MAP


def reading_to_ui(values: dict) -> dict:
    """Translate internal Modbus register names to the UI's camelCase keys.

    Single source of truth for the `reading` block — called by both
    /api/status (REST, on mount) and the WS snapshot push (every prime
    poll). Keeps the two payload shapes in lockstep.
    """
    return {
        "rpm": values.get("rpm"),
        "hz": values.get("frequency"),
        "kw": values.get("total_kw"),
        "pf": values.get("power_factor"),
        "oilP": values.get("oil_pressure"),
        "oilT": values.get("oil_temp"),
        "coolT": values.get("coolant_temp"),
        "coolLevel": values.get("coolant_level"),
        "throttle": values.get("throttle_position"),
        "o2": values.get("o2_sensor"),
        "batt": values.get("battery_volts"),
        "battA": values.get("batt_charge_current"),
        "vAB": values.get("gen_voltage_ab"),
        "vBC": values.get("gen_voltage_bc"),
        "vCA": values.get("gen_voltage_ca"),
        "iA": values.get("gen_current_a"),
        "iB": values.get("gen_current_b"),
        "iC": values.get("gen_current_c"),
        "fuelPct": values.get("fuel_level_pct"),
        "runHours": values.get("run_hours"),
        "startCount": values.get("start_count"),
    }

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

    reading_ui = reading_to_ui(r)
    reading_ui["startCount"] = start_count

    # ATS history (no contact register on H-100 map → derive from state
    # transitions into 'running').
    last_xfer = db.last_transfer_to_gen()
    thirty_days_ago = time.time() - 30 * 86400
    xfer_30d = db.count_transfers_since(thirty_days_ago)

    last_alarm = db.last_alarm_event()

    # Panel block: surfaces previously-dead polled registers and decodes
    # the key-switch position. Operator commands from this UI only
    # engage the controller when panel.mode == 'auto'.
    panel_mode = regmap.derive_panel_mode(r)
    panel = {
        "mode": panel_mode,
        "keySwitchRaw": r.get("key_switch_state"),
        "engineStatusCode": r.get("engine_status_code"),
        "activeAlarmCountHw": r.get("active_alarm_count"),
        "quietTestStatusRaw": r.get("quiettest_status"),
    }

    out = {
        "state": snap.engine_state,
        "alarmRaw": snap.alarm_raw,
        "timeInState": snap.time_in_state_s,
        "stateStartedAt": snap.state_started_at,
        # Derived load source — 'utility' | 'generator' | 'unknown'.
        # Inferred from engine state + output kW/current; see
        # services/state._derive_load_source.
        "loadSource": snap.load_source,
        "loadSourceStartedAt": snap.load_source_started_at,
        "timeInLoadSource": snap.time_in_load_source_s,
        "comms": {
            "state": snap.comms.state,
            "successPct": snap.comms.success_pct,
            "lastGoodAt": snap.comms.last_good_at,
            "rateMs": snap.comms.rate_ms,
            "p95LatencyMs": snap.comms.p95_latency_ms,
        },
        "reading": reading_ui,
        "site": {
            "id": regmap.site.id,
            "name": regmap.site.name,
            "ratingKw": regmap.site.rating_kw,
            "engine": regmap.site.engine,
            "tankGal": regmap.site.tank_gal,
            "fuelType": regmap.site.fuel_type,
        },
        "exercise": {
            "enabled": regmap.site.exercise_enabled,
            "day": regmap.site.exercise_day,
            "time": regmap.site.exercise_time,
            "durationMin": regmap.site.exercise_duration_min,
        },
        "activeAlarms": db.active_alarms(),
        "hts": {
            # Now derived from the load-source classifier rather than
            # engine_state alone, which incorrectly reported "on
            # generator" during quiet-test exercises (always unloaded)
            # and during the warm-up window before the ATS transfers.
            "transferredToGen": snap.load_source == "generator",
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
        "panel": panel,
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
