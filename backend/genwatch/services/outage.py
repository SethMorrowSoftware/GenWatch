"""Utility outage tracker.

The H-100's Modbus map doesn't expose a discrete grid-sense input on
the common register layouts (genmon strips it from `output_status_8`
on the residential Evolution lines but not H-100). We infer outages
from engine state transitions instead:

  - START: when the engine transitions stopped → cranking → running
           AND the panel is in AUTO mode, treat that as an automatic
           response to a utility outage. (A MANUAL or AUTO-via-quiet-
           test start isn't an outage.)
  - END:   the next transition from running → cooling or running →
           stopped closes the outage record. Duration is wallclock.

We also capture peak-kW and integrated-kWh during the outage so the
History view can answer "how much energy did the gen deliver during the
storm last Tuesday." Both pieces come from the existing telemetry
samples: we don't introduce a separate poll just for this.

Schema lives next to the maintenance one in db.py for consistency.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..db import Database

log = logging.getLogger("genwatch.outage")


@dataclass
class OutageRow:
    id: int
    started_ts: float
    ended_ts: float | None
    duration_s: float | None
    peak_kw: float
    kwh: float
    samples: int
    cause: str
    notes: str

    def to_ui(self) -> dict:
        return {
            "id": self.id,
            "startedTs": self.started_ts,
            "endedTs": self.ended_ts,
            "durationS": self.duration_s,
            "peakKw": self.peak_kw,
            "kwh": self.kwh,
            "samples": self.samples,
            "cause": self.cause,
            "notes": self.notes,
        }


class OutageTracker:
    """Maintains a single 'open' outage row at a time.

    The open row is identified by `ended_ts IS NULL`. Schema enforces
    at most one such row via a unique index isn't possible with NULL in
    SQLite, but the state machine is the single writer here so a race
    isn't a concern.
    """

    # Engine states that mean the gen is producing power because of a
    # utility outage (i.e., not a planned exercise / quiet-test).
    OUTAGE_STATES = {"running"}

    def __init__(self, db: Database):
        self.db = db
        self._open_id: int | None = None
        self._open_started_ts: float | None = None
        self._open_peak_kw: float = 0.0
        self._open_kwh: float = 0.0
        self._open_samples: int = 0
        self._last_sample_ts: float | None = None
        self._last_kw: float = 0.0
        self._reattach()

    def _reattach(self) -> None:
        """On boot, find any outage left open from a previous run."""
        with self.db._writer() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT id, started_ts, peak_kw, kwh, samples FROM outages "
                "WHERE ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1"
            ).fetchone()
        if row:
            self._open_id = row["id"]
            self._open_started_ts = float(row["started_ts"])
            self._open_peak_kw = float(row["peak_kw"] or 0.0)
            self._open_kwh = float(row["kwh"] or 0.0)
            self._open_samples = int(row["samples"] or 0)
            log.info("reattached open outage id=%d started_ts=%.0f",
                     self._open_id, self._open_started_ts)

    @property
    def open_id(self) -> int | None:
        return self._open_id

    # ── Transition hooks (called by the state machine on each state change) ──

    def on_transition(
        self, *, from_state: str, to_state: str, panel_mode: str, ts: float | None = None,
    ) -> dict | None:
        """Open/close outage records based on engine-state transitions.

        Returns an event dict (for forwarding) or None.
        """
        ts = ts or time.time()
        # Open: entering OUTAGE_STATES while panel was AUTO.
        if to_state in self.OUTAGE_STATES and from_state not in self.OUTAGE_STATES:
            if panel_mode == "auto" and self._open_id is None:
                self._open_outage(ts, cause="auto_start")
                return {
                    "type": "outage-started",
                    "ts": ts,
                    "id": self._open_id,
                }
        # Close: leaving OUTAGE_STATES (e.g., running → cooling / stopped).
        if from_state in self.OUTAGE_STATES and to_state not in self.OUTAGE_STATES:
            closed_id = self._close_outage(ts)
            if closed_id is not None:
                return {
                    "type": "outage-ended",
                    "ts": ts,
                    "id": closed_id,
                }
        return None

    def on_sample(self, ts: float, kw: float | None) -> None:
        """Capture peak-kW and integrate kWh while an outage is open."""
        if self._open_id is None:
            return
        kw_now = float(kw) if kw is not None else 0.0
        # Trapezoidal kWh: avg of last + current kW × elapsed hours.
        if self._last_sample_ts is not None:
            dt_h = max(0.0, (ts - self._last_sample_ts) / 3600.0)
            self._open_kwh += ((self._last_kw + kw_now) / 2.0) * dt_h
        if kw_now > self._open_peak_kw:
            self._open_peak_kw = kw_now
        self._open_samples += 1
        self._last_sample_ts = ts
        self._last_kw = kw_now
        # Flush every sample — cheap, and the row is small.
        with self.db._writer() as c:  # noqa: SLF001
            c.execute(
                "UPDATE outages SET peak_kw=?, kwh=?, samples=? WHERE id=?",
                (self._open_peak_kw, self._open_kwh, self._open_samples, self._open_id),
            )

    # ── Internal ────────────────────────────────────────────────────────

    def _open_outage(self, ts: float, *, cause: str) -> None:
        with self.db._writer() as c:  # noqa: SLF001
            cur = c.execute(
                "INSERT INTO outages (started_ts, ended_ts, peak_kw, kwh, samples, cause) "
                "VALUES (?, NULL, 0, 0, 0, ?)",
                (ts, cause),
            )
            self._open_id = cur.lastrowid
        self._open_started_ts = ts
        self._open_peak_kw = 0.0
        self._open_kwh = 0.0
        self._open_samples = 0
        self._last_sample_ts = None
        self._last_kw = 0.0
        log.info("outage opened id=%d cause=%s", self._open_id, cause)
        self.db.write_event(
            severity="warn",
            type_="OUTAGE",
            message="Utility outage detected — generator started",
            meta=f"cause={cause}",
        )

    def _close_outage(self, ts: float) -> int | None:
        if self._open_id is None or self._open_started_ts is None:
            return None
        duration_s = max(0.0, ts - self._open_started_ts)
        with self.db._writer() as c:  # noqa: SLF001
            c.execute(
                "UPDATE outages SET ended_ts=?, duration_s=? WHERE id=?",
                (ts, duration_s, self._open_id),
            )
        closed_id = self._open_id
        log.info(
            "outage closed id=%d duration=%.0fs peak=%.1fkW kwh=%.2f samples=%d",
            closed_id, duration_s, self._open_peak_kw, self._open_kwh, self._open_samples,
        )
        self.db.write_event(
            severity="ok",
            type_="OUTAGE",
            message=(
                f"Utility restored — duration {int(duration_s)}s, "
                f"peak {self._open_peak_kw:.1f} kW, {self._open_kwh:.2f} kWh"
            ),
            meta=f"id={closed_id}",
        )
        self._open_id = None
        self._open_started_ts = None
        return closed_id


def list_outages(db: Database, *, limit: int = 200, since_ts: float | None = None) -> list[OutageRow]:
    sql = "SELECT id, started_ts, ended_ts, duration_s, peak_kw, kwh, samples, cause, notes FROM outages"
    args: list = []
    if since_ts is not None:
        sql += " WHERE started_ts >= ?"
        args.append(since_ts)
    sql += " ORDER BY started_ts DESC LIMIT ?"
    args.append(limit)
    with db._writer() as c:  # noqa: SLF001
        rows = c.execute(sql, args).fetchall()
    return [
        OutageRow(
            id=r["id"],
            started_ts=float(r["started_ts"]),
            ended_ts=(float(r["ended_ts"]) if r["ended_ts"] is not None else None),
            duration_s=(float(r["duration_s"]) if r["duration_s"] is not None else None),
            peak_kw=float(r["peak_kw"] or 0.0),
            kwh=float(r["kwh"] or 0.0),
            samples=int(r["samples"] or 0),
            cause=r["cause"] or "",
            notes=r["notes"] or "",
        )
        for r in rows
    ]


def annotate_outage(db: Database, *, outage_id: int, notes: str) -> bool:
    with db._writer() as c:  # noqa: SLF001
        cur = c.execute("UPDATE outages SET notes=? WHERE id=?", (notes, outage_id))
        return (cur.rowcount or 0) > 0


def outage_summary(db: Database, *, days: int) -> dict:
    """Aggregate stats over the trailing N days. Cheap; one indexed scan."""
    since = time.time() - days * 86400
    rows = list_outages(db, since_ts=since, limit=10_000)
    completed = [r for r in rows if r.ended_ts is not None]
    return {
        "count": len(rows),
        "completed": len(completed),
        "open": 1 if any(r.ended_ts is None for r in rows) else 0,
        "totalDurationS": sum(r.duration_s or 0.0 for r in completed),
        "totalKwh": round(sum(r.kwh for r in rows), 2),
        "peakKw": round(max((r.peak_kw for r in rows), default=0.0), 1),
        "longestDurationS": max((r.duration_s or 0.0 for r in completed), default=0.0),
    }
