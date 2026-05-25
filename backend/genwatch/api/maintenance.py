"""Maintenance journal endpoints.

  GET    /api/maintenance/schedule    list of scheduled items
  PUT    /api/maintenance/schedule    upsert a scheduled item   (admin)
  DELETE /api/maintenance/schedule/{kind}                       (admin)
  GET    /api/maintenance/log         list of completed entries
  POST   /api/maintenance/log         add a new completed entry (operator+)
  GET    /api/maintenance/due         current due-status per item
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..services import maintenance as maint
from .deps import Principal, get_principal, require_admin, require_operator

log = logging.getLogger("genwatch.api.maintenance")

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


class ScheduleUpsertBody(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    interval_hours: int = Field(0, ge=0, le=100_000)
    interval_days: int = Field(0, ge=0, le=10_000)
    notes: str = ""
    enabled: bool = True


class LogEntryBody(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    run_hours_at: float | None = None
    performed_by: str = ""
    notes: str = ""
    cost_usd: float | None = None
    # Optional explicit ts for backfilled entries (default = now).
    ts: float | None = None


def _current_run_hours(request: Request) -> float | None:
    sm = getattr(request.app.state, "state_machine", None)
    if sm is None:
        return None
    rh = sm.snap.last_reading.values.get("run_hours")
    if rh is None:
        return None
    try:
        return float(rh)
    except (TypeError, ValueError):
        return None


@router.get("/schedule")
async def get_schedule(request: Request, p: Principal = Depends(get_principal)) -> dict:
    items = maint.list_schedule(request.app.state.db)
    return {
        "schedule": [
            {
                "id": s.id,
                "kind": s.kind,
                "intervalHours": s.interval_hours,
                "intervalDays": s.interval_days,
                "notes": s.notes,
                "enabled": s.enabled,
            }
            for s in items
        ]
    }


@router.put("/schedule")
async def put_schedule(
    request: Request,
    body: ScheduleUpsertBody,
    p: Principal = Depends(require_admin),
) -> dict:
    rid = maint.upsert_schedule(
        request.app.state.db,
        kind=body.kind,
        interval_hours=body.interval_hours,
        interval_days=body.interval_days,
        notes=body.notes,
        enabled=body.enabled,
    )
    request.app.state.db.write_audit(
        p.operator, "maint.upsert_schedule",
        f"{body.kind} hours={body.interval_hours} days={body.interval_days}",
        "", "ok",
    )
    return {"ok": True, "id": rid}


@router.delete("/schedule/{kind}")
async def delete_schedule(
    request: Request,
    kind: str,
    p: Principal = Depends(require_admin),
) -> dict:
    ok = maint.delete_schedule(request.app.state.db, kind)
    request.app.state.db.write_audit(
        p.operator, "maint.delete_schedule", kind, "", "ok" if ok else "failed",
    )
    if not ok:
        raise HTTPException(404, f"schedule item {kind!r} not found")
    return {"ok": True}


@router.get("/log")
async def get_log(
    request: Request,
    kind: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    p: Principal = Depends(get_principal),
) -> dict:
    rows = maint.list_log(request.app.state.db, kind=kind, limit=limit)
    return {
        "log": [
            {
                "id": r.id,
                "ts": r.ts,
                "kind": r.kind,
                "runHoursAt": r.run_hours_at,
                "performedBy": r.performed_by,
                "notes": r.notes,
                "costUsd": r.cost_usd,
            }
            for r in rows
        ]
    }


@router.post("/log")
async def post_log(
    request: Request,
    body: LogEntryBody,
    p: Principal = Depends(require_operator),
) -> dict:
    # Auto-fill run_hours_at from the live reading when the operator
    # didn't supply one — typical case: they finish service, click
    # "Record done", we record the current run-hours stamp.
    run_hours = body.run_hours_at
    if run_hours is None:
        run_hours = _current_run_hours(request)
    rid = maint.add_log_entry(
        request.app.state.db,
        kind=body.kind,
        run_hours_at=run_hours,
        performed_by=body.performed_by or p.operator,
        notes=body.notes,
        cost_usd=body.cost_usd,
        ts=body.ts,
    )
    request.app.state.db.write_audit(
        p.operator, "maint.add_log",
        f"{body.kind} run_hours={run_hours}",
        "", "ok",
    )
    request.app.state.db.write_event(
        severity="ok",
        type_="MAINT",
        message=f"Maintenance recorded — {body.kind}",
        meta=f"by {body.performed_by or p.operator}; "
             f"hours={run_hours}; cost={body.cost_usd or ''}",
    )

    # Kick the monitor so the due-state badge updates without waiting
    # an hour for the next periodic tick.
    monitor = getattr(request.app.state, "maintenance_monitor", None)
    if monitor is not None:
        try:
            await monitor._tick()  # noqa: SLF001 — same package
        except Exception:  # noqa: BLE001
            log.debug("maint monitor tick after log entry failed", exc_info=True)

    return {"ok": True, "id": rid, "runHoursAt": run_hours}


@router.get("/due")
async def get_due(request: Request, p: Principal = Depends(get_principal)) -> dict:
    run_hours = _current_run_hours(request)
    items = maint.compute_due(
        request.app.state.db,
        current_run_hours=run_hours,
        now=time.time(),
    )
    return {
        "currentRunHours": run_hours,
        "items": [
            {
                "kind": d.kind,
                "intervalHours": d.interval_hours,
                "intervalDays": d.interval_days,
                "lastTs": d.last_ts,
                "lastRunHours": d.last_run_hours,
                "hoursSince": d.hours_since,
                "daysSince": d.days_since,
                "hoursToNext": d.hours_to_next,
                "daysToNext": d.days_to_next,
                "status": d.status,
                "enabled": d.enabled,
            }
            for d in items
        ],
    }
