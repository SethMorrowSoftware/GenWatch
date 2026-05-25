"""CSV export endpoints for telemetry and events.

Stream rows as CSV so a 30-day pull doesn't materialise 100 MB in
memory. Auth-gated to operators+ so a public scrape can't drag the
service into a million-row query.

Format choice: RFC 4180 CSV with a UTF-8 BOM so Excel double-clicks
open it correctly. Timestamps are ISO-8601 in the operator's local TZ
plus an epoch-seconds column for round-trip into other tools.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..db import COLUMN_MAP
from .deps import Principal, require_operator

router = APIRouter(prefix="/api", tags=["export"])


# Hard cap so a runaway pull doesn't pin the SQLite writer for minutes.
# Telemetry past this window must use the rollup tables — exposed via
# a separate endpoint if needed; for now we cap and let the operator
# narrow the window.
_MAX_EXPORT_ROWS = 500_000


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


@router.get("/telemetry/export")
async def export_telemetry(
    request: Request,
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    columns: str | None = Query(None, description="CSV of columns to include; default = all"),
    p: Principal = Depends(require_operator),
) -> StreamingResponse:
    """Stream the raw telemetry table as CSV.

    Time window required to bound the dump. Defaults to last 24 h when
    omitted. Each row carries ISO timestamp + epoch + every metric +
    state + alarm_raw.
    """
    now = time.time()
    to_ts = to_ts or now
    from_ts = from_ts or (to_ts - 24 * 3600)
    if from_ts >= to_ts:
        raise HTTPException(400, "from must be < to")
    if (to_ts - from_ts) > 90 * 86400:
        raise HTTPException(400, "window > 90 days; narrow the range or use the 1h rollup")

    selected_cols = list(COLUMN_MAP.values())
    if columns:
        want = [c.strip() for c in columns.split(",") if c.strip()]
        bad = [c for c in want if c not in COLUMN_MAP.values()]
        if bad:
            raise HTTPException(400, f"unknown columns: {','.join(bad)}")
        selected_cols = want
    all_cols = ["iso_ts", "epoch"] + selected_cols + ["state", "alarm_raw"]

    db = request.app.state.db

    def generate():
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        # BOM for Excel; harmless for everyone else.
        yield "﻿"
        w.writerow(all_cols)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()

        sql_cols = ["ts"] + selected_cols + ["state", "alarm_raw"]
        sql = (
            f"SELECT {','.join(sql_cols)} FROM telemetry "
            f"WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?"
        )
        with db._writer() as c:  # noqa: SLF001
            cur = c.execute(sql, (from_ts, to_ts, _MAX_EXPORT_ROWS))
            for row in cur:
                ts = float(row[0])
                vals = [_iso(ts), f"{ts:.3f}"]
                for v in row[1:]:
                    if v is None:
                        vals.append("")
                    elif isinstance(v, float):
                        vals.append(f"{v:.4f}")
                    else:
                        vals.append(str(v))
                w.writerow(vals)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()

    fname = f"genwatch-telemetry-{int(from_ts)}-{int(to_ts)}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/events/export")
async def export_events(
    request: Request,
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    severity: str | None = Query(None),
    type: str | None = Query(None),
    p: Principal = Depends(require_operator),
) -> StreamingResponse:
    """Stream the events table as CSV — handy for monthly compliance reports."""
    now = time.time()
    to_ts = to_ts or now
    from_ts = from_ts or (to_ts - 30 * 86400)
    if from_ts >= to_ts:
        raise HTTPException(400, "from must be < to")
    sevs = severity.split(",") if severity else None

    db = request.app.state.db

    def generate():
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        yield "﻿"
        w.writerow(["id", "iso_ts", "epoch", "severity", "type", "message", "meta"])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()
        rows = db.read_events(
            limit=_MAX_EXPORT_ROWS,
            from_ts=from_ts,
            to_ts=to_ts,
            severities=sevs,
            type_=type,
        )
        for r in rows:
            ts = float(r["ts"])
            w.writerow([
                r["id"], _iso(ts), f"{ts:.3f}",
                r["severity"], r["type"], r["message"], r.get("meta") or "",
            ])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()

    fname = f"genwatch-events-{int(from_ts)}-{int(to_ts)}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/audit/export")
async def export_audit(
    request: Request,
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    p: Principal = Depends(require_operator),
) -> StreamingResponse:
    """Stream the audit log as CSV — every control command, login attempt,
    config update, and register reload, with timestamps and operator.
    """
    now = time.time()
    to_ts = to_ts or now
    from_ts = from_ts or (to_ts - 90 * 86400)
    if from_ts >= to_ts:
        raise HTTPException(400, "from must be < to")

    db = request.app.state.db

    def generate():
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        yield "﻿"
        w.writerow(["id", "iso_ts", "epoch", "operator", "action", "detail", "token", "result"])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()
        with db._writer() as c:  # noqa: SLF001
            cur = c.execute(
                "SELECT id, ts, operator, action, detail, token, result FROM audit "
                "WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
                (from_ts, to_ts, _MAX_EXPORT_ROWS),
            )
            for r in cur:
                ts = float(r["ts"])
                w.writerow([
                    r["id"], _iso(ts), f"{ts:.3f}",
                    r["operator"], r["action"], r["detail"] or "",
                    r["token"] or "", r["result"],
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()

    fname = f"genwatch-audit-{int(from_ts)}-{int(to_ts)}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
