"""GET /api/events, /api/alarms, POST /api/alarms/{code}/ack"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..services.control import ControlError
from .deps import Principal, require_operator

log = logging.getLogger("genwatch.api.events")

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
async def events(
    request: Request,
    limit: int = Query(200, ge=1, le=2000),
    severity: str | None = Query(None),
    type: str | None = Query(None),
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    p: Principal = Depends(require_operator),
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
async def alarms(
    request: Request,
    active: bool = Query(True),
    p: Principal = Depends(require_operator),
) -> dict:
    db = request.app.state.db
    if not active:
        # historical alarms are in the events table with type=ALARM
        rows = db.read_events(limit=500, type_="ALARM")
        return {"alarms": rows}
    return {"alarms": db.active_alarms()}


@router.get("/alarm-codes")
async def alarm_codes(
    request: Request,
    p: Principal = Depends(require_operator),
) -> dict:
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


class AckBody(BaseModel):
    confirm_token: str


@router.post("/alarms/{code}/ack")
async def ack_alarm(
    request: Request,
    code: str,
    body: AckBody,
    p: Principal = Depends(require_operator),
) -> dict:
    """Acknowledge an active alarm.

    Sends the H-100's ALARM_ACK write (FC16 0x012E ← 0x0001) — equivalent to
    pressing the ACK/RESET button at the H-100 panel — and then clears the
    row from the local active-alarms table. The local clear is idempotent
    (a re-raise on the next poll is harmless), but issuing the hardware
    write is what actually un-latches the alarm at the controller so the
    panel light goes out.

    Two-step confirm: the operator must have issued a fresh token via
    GET /api/control/confirm and supplied it in the body. Same gate as
    the start/stop/exercise/transfer control endpoints — an alarm ack
    is a write to the controller and a misclick on an active shutdown
    alarm could re-enable a remote-start path the unit was holding
    off. The confirm-token also defeats CSRF on this state-changing
    endpoint regardless of cookie SameSite posture.

    If `ack_alarm` is not defined in the register map, we fall back to a
    local-only clear and log a warning — the H-100 will keep re-raising the
    bit on every poll until cleared at the panel.

    If the Modbus write fails, we surface the error and do NOT clear the
    local DB row: the operator should know the controller still has the
    alarm latched.
    """
    db = request.app.state.db
    regmap = request.app.state.regmap
    client = request.app.state.client
    ctl_service = request.app.state.control

    if code not in {a["code"] for a in db.active_alarms()}:
        raise HTTPException(404, f"alarm {code} not active")

    # Freshness gate: an ack is a write to the controller. If the H-100
    # link is LOST we can't confirm the alarm is still latched (the active
    # set is held from the last good poll), and the write would fail or be
    # dropped anyway — refuse with a clear reason instead of issuing a
    # blind write against a dead link. Mirrors the control-endpoint gate.
    comms_state = getattr(request.app.state.state_machine.snap.comms, "state", "lost")
    if comms_state == "lost":
        raise HTTPException(
            409,
            detail={
                "code": "comms_lost",
                "message": "cannot acknowledge: H-100 communication is LOST. "
                "Restore the link before acknowledging alarms.",
            },
        )

    # Validate + consume the confirm token before touching hardware.
    # consume_token takes the control service's lock so the token is
    # consumed atomically — the same operator's parallel ack attempt
    # with the same token will see token_invalid.
    try:
        await ctl_service.consume_token(body.confirm_token, p.operator, verb="ack")
    except ControlError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})

    ctl = regmap.controls.get("ack_alarm")
    hw_ack = False
    if ctl is not None:
        write_words = list(ctl.write_values)
        log.warning(
            "ALARM ACK by %s -> %s @0x%04X fc=%d values=%s (code=%s)",
            p.operator, ctl.name, ctl.addr, ctl.fc,
            [f"0x{w:04X}" for w in write_words], code,
        )
        if len(write_words) == 1 and ctl.fc == 6:
            res = await client.write(ctl.addr, write_words[0], fc=6)
        else:
            res = await client.write(ctl.addr, fc=ctl.fc, values=write_words)
        if not res.ok:
            db.write_audit(
                p.operator, "alarm.ack",
                f"{code} via reg={ctl.name}@0x{ctl.addr:04X}",
                "",
                f"failed: {res.error}",
            )
            db.write_event(
                severity="warn",
                type_="ALARM",
                message=f"Alarm ack — Modbus write failed for {code}",
                meta=res.error or "",
            )
            raise HTTPException(502, f"Modbus ack write failed: {res.error}")
        hw_ack = True
    else:
        log.warning(
            "alarm ack: no 'ack_alarm' control defined in register map; "
            "clearing local DB only (panel will keep re-raising on next poll)"
        )

    db.clear_alarm(code)
    db.write_event(
        severity="ok",
        type_="ALARM",
        message=f"Alarm acknowledged — {code}",
        meta=p.operator,
    )
    audit_detail = code
    if ctl is not None:
        audit_detail = f"{code} via reg={ctl.name}@0x{ctl.addr:04X} fc{ctl.fc}"
    db.write_audit(p.operator, "alarm.ack", audit_detail, "", "ok")
    return {"ok": True, "code": code, "hw_ack": hw_ack}
