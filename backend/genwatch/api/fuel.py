"""GET /api/fuel — current burn rate, running totals, projected hours."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .deps import Principal, get_principal

router = APIRouter(prefix="/api", tags=["fuel"])


@router.get("/fuel")
async def fuel(request: Request, p: Principal = Depends(get_principal)) -> dict:
    accum = getattr(request.app.state, "fuel_accum", None)
    rm = request.app.state.regmap
    sm = request.app.state.state_machine
    if accum is None:
        return {
            "enabled": False,
            "reason": "fuel accumulator not initialised",
        }
    tank_gal = getattr(rm.site, "tank_gal", None)
    tank_pct = sm.snap.last_reading.values.get("fuel_level_pct")
    try:
        tank_pct_f = float(tank_pct) if tank_pct is not None else None
    except (TypeError, ValueError):
        tank_pct_f = None
    return {
        "enabled": True,
        **accum.to_ui(tank_gal=tank_gal, tank_pct=tank_pct_f),
    }
