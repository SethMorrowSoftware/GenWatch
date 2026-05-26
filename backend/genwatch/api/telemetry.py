"""GET /api/telemetry — time-series for the History view."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..db import COLUMN_MAP
from .deps import Principal, require_operator

router = APIRouter(prefix="/api", tags=["telemetry"])


# UI-friendly metric keys -> SQLite columns. Mirrors the History tabs in
# the design.
METRIC_TO_COLUMN = {
    "kw": "kw",
    "rpm": "rpm",
    "hz": "hz",
    "oilP": "oil_p",
    "coolT": "cool_t",
    "batt": "batt",
    "vAB": "v_ab",
    "iA": "i_a",
}


@router.get("/telemetry")
async def telemetry(
    request: Request,
    metric: str = Query(..., description="kw|rpm|hz|oilP|coolT|batt|vAB|iA"),
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    max_points: int = Query(2000, ge=10, le=10_000),
    p: Principal = Depends(require_operator),
) -> dict:
    column = METRIC_TO_COLUMN.get(metric)
    if column is None:
        raise HTTPException(400, f"unknown metric {metric!r}; options: {','.join(METRIC_TO_COLUMN.keys())}")
    now = time.time()
    to_ts = to_ts or now
    from_ts = from_ts or (to_ts - 3600)
    if from_ts >= to_ts:
        raise HTTPException(400, "from must be < to")

    rows = await _read(request, column, from_ts, to_ts, max_points)
    return {
        "metric": metric,
        "column": column,
        "from": from_ts,
        "to": to_ts,
        "count": len(rows),
        "points": rows,   # list of [ts, value]
    }


async def _read(request: Request, column: str, from_ts: float, to_ts: float, max_points: int):
    db = request.app.state.db
    import anyio  # local import to keep module light
    rows = await anyio.to_thread.run_sync(
        db.read_telemetry, column, from_ts, to_ts, max_points
    )
    return [[ts, v] for ts, v in rows]


@router.get("/telemetry/columns")
async def columns(p: Principal = Depends(require_operator)) -> dict:
    return {"metric_to_column": METRIC_TO_COLUMN, "all_columns": COLUMN_MAP}
