"""Utility-outage history endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..services import outage as outage_svc
from .deps import Principal, get_principal, require_operator

router = APIRouter(prefix="/api/outages", tags=["outages"])


class AnnotateBody(BaseModel):
    notes: str = ""


@router.get("")
async def list_outages(
    request: Request,
    limit: int = Query(200, ge=1, le=2000),
    days: int | None = Query(None, ge=1, le=3650),
    p: Principal = Depends(get_principal),
) -> dict:
    since = None
    if days:
        import time
        since = time.time() - days * 86400
    rows = outage_svc.list_outages(request.app.state.db, limit=limit, since_ts=since)
    summary30 = outage_svc.outage_summary(request.app.state.db, days=30)
    summary365 = outage_svc.outage_summary(request.app.state.db, days=365)
    return {
        "outages": [r.to_ui() for r in rows],
        "summary30d": summary30,
        "summary365d": summary365,
    }


@router.post("/{outage_id}/notes")
async def annotate(
    request: Request,
    outage_id: int,
    body: AnnotateBody,
    p: Principal = Depends(require_operator),
) -> dict:
    ok = outage_svc.annotate_outage(
        request.app.state.db, outage_id=outage_id, notes=body.notes,
    )
    if not ok:
        raise HTTPException(404, f"outage {outage_id} not found")
    request.app.state.db.write_audit(
        p.operator, "outage.annotate", f"id={outage_id}", "", "ok",
    )
    return {"ok": True}
