"""Maintenance journal — schedules, log entries, due-detection.

Two halves:
  - schedule:  what should be done (oil change, air filter, coolant)
               and at what interval, in run-hours and/or calendar days.
  - log:       what has been done — operator-recorded entries with
               run_hours_at, performed_by, notes, cost.

Due-detection compares the schedule's interval against the most recent
log entry's run_hours_at vs the current run_hours reading, plus the
calendar-day interval against the log entry's ts. An item is "due
soon" within 10 % of either trigger, "overdue" past it.

The service emits OK/WARN events into the standard event log so the
existing Slack channel and UI feeds pick them up without any extra
plumbing. A periodic scanner (every hour) re-evaluates due-state and
emits only on transitions, so we don't spam channels on every scan.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Iterable

from ..db import Database

log = logging.getLogger("genwatch.maintenance")


# Default schedule items — populated on first boot if the
# maintenance_schedule table is empty. Sourced from the Cummins QSB7-G5
# service manual + common standby-genset best practice.
DEFAULT_SCHEDULE = [
    # (kind, interval_hours, interval_days, notes)
    ("oil_change",      250,  365, "Engine oil + drain magnetic plug. Replace gasket."),
    ("oil_filter",      250,  365, "Spin-on filter. Pre-fill before install."),
    ("air_filter",      500,  730, "Inspect every 250 h; replace at 500 h or sooner."),
    ("fuel_filter",     500,  730, "Primary + secondary. Bleed system after."),
    ("coolant",         2000, 1825, "5-year or 2000-h coolant flush + refill."),
    ("battery_replace", 0,    1095, "3-year service life; load-test annually."),
    ("battery_test",    0,    180,  "Bi-annual load test under typical start load."),
    ("belt_inspect",    1000, 365, "Drive belts: tension + cracks check."),
    ("hose_inspect",    1000, 365, "Coolant + air intake hoses."),
    ("annual_service",  0,    365, "Full pro service: valve adjustment, comp test, injectors."),
]


@dataclass
class ScheduleItem:
    id: int
    kind: str
    interval_hours: int          # 0 = no hours-based trigger
    interval_days: int           # 0 = no calendar-based trigger
    notes: str
    enabled: bool


@dataclass
class LogEntry:
    id: int
    ts: float
    kind: str
    run_hours_at: float | None
    performed_by: str
    notes: str
    cost_usd: float | None


@dataclass
class DueStatus:
    """Per-schedule-item status computed from the most recent log entry.

    `status` is one of:
      - 'ok'       : both triggers have headroom (>10 % from due)
      - 'soon'     : within 10 % of either trigger
      - 'overdue'  : past either trigger
      - 'never'    : item has never been recorded (treat as overdue)
    """
    kind: str
    interval_hours: int
    interval_days: int
    last_ts: float | None
    last_run_hours: float | None
    hours_since: float | None
    days_since: float | None
    hours_to_next: float | None
    days_to_next: float | None
    status: str
    enabled: bool


def seed_default_schedule(db: Database) -> int:
    """Populate maintenance_schedule with sensible defaults if empty.

    Returns the number of rows inserted (0 if the table already has
    user-edited contents).
    """
    with db._writer() as c:  # noqa: SLF001
        existing = c.execute("SELECT COUNT(*) FROM maintenance_schedule").fetchone()
        if existing and existing[0] > 0:
            return 0
        now = time.time()
        c.executemany(
            "INSERT INTO maintenance_schedule "
            "(kind, interval_hours, interval_days, notes, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            [(k, h, d, n, now) for (k, h, d, n) in DEFAULT_SCHEDULE],
        )
        return len(DEFAULT_SCHEDULE)


def list_schedule(db: Database) -> list[ScheduleItem]:
    with db._writer() as c:  # noqa: SLF001
        rows = c.execute(
            "SELECT id, kind, interval_hours, interval_days, notes, enabled "
            "FROM maintenance_schedule ORDER BY kind"
        ).fetchall()
    return [
        ScheduleItem(
            id=r["id"], kind=r["kind"],
            interval_hours=r["interval_hours"],
            interval_days=r["interval_days"],
            notes=r["notes"], enabled=bool(r["enabled"]),
        )
        for r in rows
    ]


def upsert_schedule(
    db: Database, *, kind: str, interval_hours: int, interval_days: int,
    notes: str = "", enabled: bool = True,
) -> int:
    """Insert or update a schedule row. Returns the row id."""
    with db._writer() as c:  # noqa: SLF001
        cur = c.execute(
            "INSERT INTO maintenance_schedule "
            "(kind, interval_hours, interval_days, notes, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET "
            "interval_hours=excluded.interval_hours, "
            "interval_days=excluded.interval_days, "
            "notes=excluded.notes, "
            "enabled=excluded.enabled",
            (kind, int(interval_hours), int(interval_days), notes,
             1 if enabled else 0, time.time()),
        )
        return cur.lastrowid or 0


def delete_schedule(db: Database, kind: str) -> bool:
    with db._writer() as c:  # noqa: SLF001
        cur = c.execute("DELETE FROM maintenance_schedule WHERE kind = ?", (kind,))
        return (cur.rowcount or 0) > 0


def add_log_entry(
    db: Database, *, kind: str, run_hours_at: float | None,
    performed_by: str = "", notes: str = "", cost_usd: float | None = None,
    ts: float | None = None,
) -> int:
    with db._writer() as c:  # noqa: SLF001
        cur = c.execute(
            "INSERT INTO maintenance_log "
            "(ts, kind, run_hours_at, performed_by, notes, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts or time.time(), kind, run_hours_at, performed_by, notes, cost_usd),
        )
        return cur.lastrowid or 0


def list_log(db: Database, *, kind: str | None = None, limit: int = 500) -> list[LogEntry]:
    sql = "SELECT id, ts, kind, run_hours_at, performed_by, notes, cost_usd FROM maintenance_log"
    args: list = []
    if kind:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with db._writer() as c:  # noqa: SLF001
        rows = c.execute(sql, args).fetchall()
    return [
        LogEntry(
            id=r["id"], ts=float(r["ts"]),
            kind=r["kind"],
            run_hours_at=(float(r["run_hours_at"]) if r["run_hours_at"] is not None else None),
            performed_by=r["performed_by"] or "",
            notes=r["notes"] or "",
            cost_usd=(float(r["cost_usd"]) if r["cost_usd"] is not None else None),
        )
        for r in rows
    ]


def most_recent_per_kind(db: Database) -> dict[str, LogEntry]:
    """Latest log entry for each kind, indexed by kind."""
    with db._writer() as c:  # noqa: SLF001
        rows = c.execute(
            """
            SELECT id, ts, kind, run_hours_at, performed_by, notes, cost_usd
            FROM maintenance_log
            WHERE id IN (
                SELECT MAX(id) FROM maintenance_log GROUP BY kind
            )
            """
        ).fetchall()
    out: dict[str, LogEntry] = {}
    for r in rows:
        out[r["kind"]] = LogEntry(
            id=r["id"], ts=float(r["ts"]),
            kind=r["kind"],
            run_hours_at=(float(r["run_hours_at"]) if r["run_hours_at"] is not None else None),
            performed_by=r["performed_by"] or "",
            notes=r["notes"] or "",
            cost_usd=(float(r["cost_usd"]) if r["cost_usd"] is not None else None),
        )
    return out


def compute_due(
    db: Database, *, current_run_hours: float | None, now: float | None = None,
) -> list[DueStatus]:
    """For each enabled schedule item, return its due-status."""
    now = now or time.time()
    items = [it for it in list_schedule(db) if it.enabled]
    last = most_recent_per_kind(db)
    out: list[DueStatus] = []
    for it in items:
        le = last.get(it.kind)
        if le is None:
            out.append(DueStatus(
                kind=it.kind,
                interval_hours=it.interval_hours,
                interval_days=it.interval_days,
                last_ts=None,
                last_run_hours=None,
                hours_since=None,
                days_since=None,
                hours_to_next=None,
                days_to_next=None,
                status="never",
                enabled=it.enabled,
            ))
            continue
        hours_since = None
        if it.interval_hours > 0 and current_run_hours is not None and le.run_hours_at is not None:
            hours_since = max(0.0, current_run_hours - le.run_hours_at)
        days_since = max(0.0, (now - le.ts) / 86400.0)

        hours_to_next = None
        days_to_next = None
        triggered = False
        approaching = False
        if it.interval_hours > 0 and hours_since is not None:
            remaining = it.interval_hours - hours_since
            hours_to_next = round(remaining, 1)
            if remaining <= 0:
                triggered = True
            elif remaining <= max(1.0, it.interval_hours * 0.1):
                approaching = True
        if it.interval_days > 0:
            remaining_d = it.interval_days - days_since
            days_to_next = round(remaining_d, 1)
            if remaining_d <= 0:
                triggered = True
            elif remaining_d <= max(1.0, it.interval_days * 0.1):
                approaching = True

        if triggered:
            status = "overdue"
        elif approaching:
            status = "soon"
        else:
            status = "ok"
        out.append(DueStatus(
            kind=it.kind,
            interval_hours=it.interval_hours,
            interval_days=it.interval_days,
            last_ts=le.ts,
            last_run_hours=le.run_hours_at,
            hours_since=(round(hours_since, 1) if hours_since is not None else None),
            days_since=round(days_since, 1),
            hours_to_next=hours_to_next,
            days_to_next=days_to_next,
            status=status,
            enabled=it.enabled,
        ))
    return out


class MaintenanceMonitor:
    """Background task that re-evaluates due-state hourly and emits
    transition events (OK → soon → overdue) so the Slack channel + UI
    feed pick them up without polling. Also writes a 'maint_due_state'
    KV entry so the API can return the last-computed snapshot cheaply.
    """

    PERIOD_S = 60 * 60  # 1 hour

    def __init__(self, db: Database, get_run_hours):
        self.db = db
        self._get_run_hours = get_run_hours
        self._task: asyncio.Task | None = None
        self._running = False
        # In-process memory of last status per kind; persisted to kv too
        # so a service restart doesn't re-emit a stale "overdue" event.
        self._last_status: dict[str, str] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._load_last_state()
        # Run once at startup so a fresh service has a baseline.
        await self._tick()
        self._task = asyncio.create_task(self._loop(), name="maintenance")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _load_last_state(self) -> None:
        raw = self.db.kv_get("maint_due_state")
        if not raw:
            return
        try:
            import json
            self._last_status = {k: str(v) for k, v in json.loads(raw).items()}
        except Exception:  # noqa: BLE001
            self._last_status = {}

    def _save_last_state(self) -> None:
        import json
        self.db.kv_set("maint_due_state", json.dumps(self._last_status))

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.PERIOD_S)
            except asyncio.CancelledError:
                break
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("maintenance tick failed: %s", e)

    async def _tick(self) -> None:
        try:
            run_hours = self._get_run_hours()
        except Exception as e:  # noqa: BLE001
            log.debug("maintenance tick — run_hours unavailable: %s", e)
            run_hours = None
        due = await asyncio.to_thread(compute_due, self.db, current_run_hours=run_hours)
        changed = False
        for d in due:
            prev = self._last_status.get(d.kind, "ok")
            if d.status == prev:
                continue
            changed = True
            self._last_status[d.kind] = d.status
            if d.status == "overdue":
                self.db.write_event(
                    severity="warn",
                    type_="MAINT",
                    message=f"Maintenance overdue — {d.kind}",
                    meta=_due_meta(d),
                )
            elif d.status == "soon":
                self.db.write_event(
                    severity="info",
                    type_="MAINT",
                    message=f"Maintenance due soon — {d.kind}",
                    meta=_due_meta(d),
                )
            elif d.status == "ok" and prev != "ok":
                self.db.write_event(
                    severity="ok",
                    type_="MAINT",
                    message=f"Maintenance recorded — {d.kind}",
                    meta=_due_meta(d),
                )
        if changed:
            self._save_last_state()


def _due_meta(d: DueStatus) -> str:
    parts = []
    if d.hours_to_next is not None:
        parts.append(f"hours_to_next={d.hours_to_next}")
    if d.days_to_next is not None:
        parts.append(f"days_to_next={d.days_to_next}")
    return ", ".join(parts) if parts else ""
