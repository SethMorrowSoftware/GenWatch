"""GET /api/system — host health (Pi CPU temp, disk, memory, uptime).

Auth: any authenticated principal can see this. The values are not
sensitive (anyone on the LAN already has the IP) and we want the
operator's mobile to surface "the Pi is overheating" without having to
SSH in.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..services import system_health
from .deps import Principal, get_principal

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system")
async def system(request: Request, p: Principal = Depends(get_principal)) -> dict:
    settings = request.app.state.settings
    started_at = request.app.state.started_at
    snap = system_health.collect(settings.data_dir, started_at)
    return snap.to_ui()
