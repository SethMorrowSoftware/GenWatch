"""GET /api/events, /api/alarms, POST /api/alarms/{code}/ack"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .deps import Principal, require_operator

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
async def events(
    request: Request,
    limit: int = Query(200, ge=1, le=2000),
    severity: str | None = Query(None),
    type: str | None = Query(None),
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
) -> dict:
    db = request.app.state.db
    sevs = severity.split(",") if severity else None
    rows = db.read_events(
        limit=limit,
        severities=sevs,
        type_=type,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    return {"count": len(rows), "events": rows}


@router.get("/alarms")
async def alarms(request: Request, active: bool = Query(True)) -> dict:
    db = request.app.state.db
    if not active:
        # historical alarms are in the events table with type=ALARM
        rows = db.read_events(limit=500, type_="ALARM")
        return {"alarms": rows}
    return {"alarms": db.active_alarms()}


@router.get("/alarm-codes")
async def alarm_codes(request: Request) -> dict:
    regmap = request.app.state.regmap
    return {
        "codes": [
            {
                "code": a.code,
                "desc": a.desc,
                "severity": a.severity,
                "register": a.register,
                "mask": f"0x{a.mask:04X}",
            }
            for a in regmap.alarm_bits
        ]
    }


@router.post("/alarms/{code}/ack")
async def ack_alarm(
    request: Request,
    code: str,
    p: Principal = Depends(require_operator),
) -> dict:
    db = request.app.state.db
    cleared = db.clear_alarm(code)
    if not cleared:
        raise HTTPException(404, f"alarm {code} not active")
    db.write_event(
        severity="ok",
        type_="ALARM",
        message=f"Alarm acknowledged — {code}",
        meta=p.operator,
    )
    db.write_audit(p.operator, "alarm.ack", code, "", "ok")
    return {"ok": True, "code": code}
