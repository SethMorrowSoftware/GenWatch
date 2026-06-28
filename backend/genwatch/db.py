"""SQLite-backed storage: telemetry, events, audit log.

Schema goals:
  - Telemetry table is wide (one row per poll, columns per metric) so a
    "show me the last hour of kw and rpm" query is one indexed scan.
  - Events and audit are append-only.
  - Rollup tables (1-min, 1-hr) are computed by a periodic task in
    retention.py — keep the source of truth (raw) bounded.
  - WAL journal mode for crash safety + concurrent reads while writes
    are queued.

Concurrency model:
  - ONE persistent write connection (check_same_thread=False) reused for
    every mutation, serialized by a process-wide RLock. This avoids the
    per-write open/close churn (each close ran a WAL checkpoint) that
    dominated write cost and could stall the event loop.
  - Reads use their own short-lived connections and do NOT take the write
    lock — WAL allows concurrent readers, so /api/status, history, and
    events queries don't serialize behind a write or a retention prune.
  - Telemetry and retention writes are dispatched off the event loop via
    asyncio.to_thread; the remaining (sparse) event/alarm/audit writes go
    straight to the persistent connection, which keeps them cheap.
  - A periodic wal_checkpoint(TRUNCATE) (from the retention tick) bounds
    WAL growth on the SD card.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

log = logging.getLogger("genwatch.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    ts          REAL    NOT NULL,
    rpm         REAL,
    hz          REAL,
    kw          REAL,
    oil_p       REAL,
    cool_t      REAL,
    batt        REAL,
    v_ab        REAL,
    v_bc        REAL,
    v_ca        REAL,
    i_a         REAL,
    i_b         REAL,
    i_c         REAL,
    fuel_pct    REAL,
    state       TEXT,
    alarm_raw   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);

CREATE TABLE IF NOT EXISTS telemetry_1m (
    ts          INTEGER NOT NULL,    -- minute bucket epoch
    rpm_avg REAL, rpm_min REAL, rpm_max REAL,
    hz_avg REAL,  hz_min REAL,  hz_max REAL,
    kw_avg REAL,  kw_min REAL,  kw_max REAL,
    oil_p_avg REAL, cool_t_avg REAL, batt_avg REAL,
    v_ab_avg REAL, i_a_avg REAL,
    samples INTEGER NOT NULL,
    PRIMARY KEY (ts)
);

CREATE TABLE IF NOT EXISTS telemetry_1h (
    ts          INTEGER NOT NULL,    -- hour bucket epoch
    rpm_avg REAL, hz_avg REAL, kw_avg REAL,
    oil_p_avg REAL, cool_t_avg REAL, batt_avg REAL,
    samples INTEGER NOT NULL,
    runtime_s INTEGER NOT NULL DEFAULT 0,
    kwh REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (ts)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    severity    TEXT    NOT NULL,        -- ok | info | warn | alarm
    type        TEXT    NOT NULL,        -- TRANSITION | ALARM | COMMAND | COMMS | ...
    message     TEXT    NOT NULL,
    meta        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_sev ON events(severity, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts DESC);

CREATE TABLE IF NOT EXISTS alarms_active (
    code        TEXT    PRIMARY KEY,
    desc        TEXT    NOT NULL,
    severity    TEXT    NOT NULL,
    raised_at   REAL    NOT NULL,
    raw         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    operator    TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    detail      TEXT,
    token       TEXT,
    result      TEXT    NOT NULL        -- ok | denied | failed
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS kv (
    k           TEXT PRIMARY KEY,
    v           TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""

# Map register name -> telemetry column name. Names not present here
# are silently ignored when writing telemetry rows. Keep in sync with
# registers/h100.yaml.
COLUMN_MAP = {
    "rpm": "rpm",
    "frequency": "hz",
    "total_kw": "kw",
    "oil_pressure": "oil_p",
    "coolant_temp": "cool_t",
    "battery_volts": "batt",
    "gen_voltage_ab": "v_ab",
    "gen_voltage_bc": "v_bc",
    "gen_voltage_ca": "v_ca",
    "gen_current_a": "i_a",
    "gen_current_b": "i_b",
    "gen_current_c": "i_c",
    "fuel_level_pct": "fuel_pct",
}

ALL_COLUMNS = ["ts"] + list(COLUMN_MAP.values()) + ["state", "alarm_raw"]

# Which averaged columns each rollup tier carries (see SCHEMA). Used by
# read_telemetry to pick the coarsest tier that actually has the metric,
# and to keep the dynamically-built column name confined to a known set.
ROLLUP_1M_AVG_COLS = {"rpm", "hz", "kw", "oil_p", "cool_t", "batt", "v_ab", "i_a"}
ROLLUP_1H_AVG_COLS = {"rpm", "hz", "kw", "oil_p", "cool_t", "batt"}
RAW_TIER_MAX_SPAN_S = 6 * 3600        # <= 6h → raw samples
ROLLUP_1M_MAX_SPAN_S = 14 * 86400     # <= 14d → 1-minute rollup


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wlock = threading.RLock()
        self._closed = False
        # One persistent write connection reused for every mutation. The
        # previous per-call open/close churned a connection (+5 PRAGMAs +
        # a WAL checkpoint on close) on EVERY write — far more expensive
        # than the INSERT itself, and the close-checkpoint is what could
        # stall the loop. check_same_thread=False + _wlock lets the event
        # loop, the to_thread telemetry/retention workers, and the control
        # path share it safely (serialized by the lock).
        self._wconn = self._connect(check_same_thread=False)
        self._init_schema()

    def _connect(self, check_same_thread: bool = True) -> sqlite3.Connection:
        c = sqlite3.connect(
            self.path, isolation_level=None, timeout=10,
            check_same_thread=check_same_thread,
        )
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        # synchronous=FULL: in WAL mode this fsyncs the WAL on every
        # commit, so a power cut on the Pi cannot lose alarm / control
        # audit / events rows that just completed. The perf cost on our
        # write volume (~1 row/15s telemetry, sparse events) is
        # negligible compared to the integrity benefit on a device that
        # shares power with the generator it's monitoring.
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA cache_size=-8000")  # 8 MiB
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init_schema(self) -> None:
        with self._wlock:
            self._wconn.executescript(SCHEMA)

    @contextmanager
    def _writer(self):
        """Serialized access to the single persistent write connection."""
        with self._wlock:
            yield self._wconn

    @contextmanager
    def _reader(self):
        """A short-lived read connection that does NOT take the write
        lock. WAL permits concurrent readers, so reads never serialize
        behind a write or a multi-thousand-row retention prune."""
        c = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def close(self) -> None:
        """Close the persistent write connection (clean shutdown)."""
        with self._wlock:
            if not self._closed:
                try:
                    self._wconn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._closed = True

    def checkpoint(self) -> None:
        """Truncate the WAL so it can't grow unbounded on the SD card.
        Best-effort — a busy checkpoint (a reader holding a lock) just
        defers to the next retention tick rather than erroring."""
        try:
            with self._writer() as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:  # noqa: BLE001
            log.debug("wal checkpoint deferred: %s", e)

    # ─── telemetry ─────────────────────────────────────────────────────
    def write_telemetry(self, ts: float, values: dict, state: str, alarm_raw: int) -> None:
        cols = ["ts"]
        vals: list = [ts]
        for reg_name, col in COLUMN_MAP.items():
            if reg_name in values and values[reg_name] is not None:
                cols.append(col)
                vals.append(float(values[reg_name]))
        cols.extend(["state", "alarm_raw"])
        vals.extend([state, int(alarm_raw or 0)])
        sql = f"INSERT INTO telemetry ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
        with self._writer() as c:
            c.execute(sql, vals)

    def read_telemetry(
        self,
        metric: str,
        from_ts: float,
        to_ts: float,
        max_points: int = 2000,
    ) -> list[tuple[float, float]]:
        if metric not in COLUMN_MAP.values() and metric != "state":
            raise ValueError(f"unknown metric {metric!r}")
        # Pick the coarsest tier that (a) bounds the payload for the span
        # and (b) actually carries this metric. `state` and metrics not in
        # a rollup tier always fall back to raw. The column name is only
        # ever a member of the validated COLUMN_MAP / rollup sets, so the
        # f-string interpolation can't carry untrusted input.
        span = max(1.0, to_ts - from_ts)
        if metric == "state" or span <= RAW_TIER_MAX_SPAN_S:
            sql = "SELECT ts, " + metric + " FROM telemetry WHERE ts >= ? AND ts <= ? ORDER BY ts"
            params: tuple = (from_ts, to_ts)
        elif span <= ROLLUP_1M_MAX_SPAN_S and metric in ROLLUP_1M_AVG_COLS:
            sql = f"SELECT ts, {metric}_avg FROM telemetry_1m WHERE ts >= ? AND ts <= ? ORDER BY ts"
            params = (int(from_ts), int(to_ts))
        elif metric in ROLLUP_1H_AVG_COLS:
            # Long span: use the hourly rollup (retained ~2 years) so a
            # multi-month query returns data instead of nothing once the
            # 1-minute rollup (90 d) has been pruned.
            sql = f"SELECT ts, {metric}_avg FROM telemetry_1h WHERE ts >= ? AND ts <= ? ORDER BY ts"
            params = (int(from_ts), int(to_ts))
        elif metric in ROLLUP_1M_AVG_COLS:
            # Long span but the metric is only rolled up at 1-minute
            # granularity — use it (sparse past the 1m horizon).
            sql = f"SELECT ts, {metric}_avg FROM telemetry_1m WHERE ts >= ? AND ts <= ? ORDER BY ts"
            params = (int(from_ts), int(to_ts))
        else:
            sql = "SELECT ts, " + metric + " FROM telemetry WHERE ts >= ? AND ts <= ? ORDER BY ts"
            params = (from_ts, to_ts)
        with self._reader() as c:
            rows = c.execute(sql, params).fetchall()

        # decimate
        if len(rows) > max_points:
            step = max(1, len(rows) // max_points)
            rows = rows[::step]
        return [(float(r[0]), float(r[1]) if r[1] is not None else 0.0) for r in rows]

    def telemetry_latest(self) -> dict | None:
        sql = f"SELECT {','.join(ALL_COLUMNS)} FROM telemetry ORDER BY ts DESC LIMIT 1"
        with self._reader() as c:
            r = c.execute(sql).fetchone()
        return dict(r) if r else None

    def _prune_chunked(self, table: str, older_than_ts, chunk: int, extra: str = "") -> int:
        """Delete rows older than ts in bounded chunks, releasing the write
        lock between each so a large prune can't hold off live writes or
        reads for the whole delete. `table`/`extra` are internal constants,
        never user input."""
        total = 0
        while True:
            with self._writer() as c:
                cur = c.execute(
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE ts < ?{extra} LIMIT ?)",
                    (older_than_ts, chunk),
                )
                n = cur.rowcount or 0
            total += n
            if n < chunk:
                return total

    def prune_raw_telemetry(self, older_than_ts: float, chunk: int = 5000) -> int:
        return self._prune_chunked("telemetry", older_than_ts, chunk)

    def prune_rollup_1m(self, older_than_ts: float, chunk: int = 5000) -> int:
        return self._prune_chunked("telemetry_1m", int(older_than_ts), chunk)

    def prune_rollup_1h(self, older_than_ts: float, chunk: int = 5000) -> int:
        return self._prune_chunked("telemetry_1h", int(older_than_ts), chunk)

    def prune_events(self, older_than_ts: float, chunk: int = 5000) -> int:
        """Prune info/ok events older than ts. Alarms and warnings are
        always kept for forensic review."""
        return self._prune_chunked(
            "events", older_than_ts, chunk, extra=" AND severity IN ('info','ok')"
        )

    def aggregate_rollup_1m(self, from_ts: float, to_ts: float) -> int:
        """Aggregate raw telemetry into 1-minute buckets in the half-open
        interval [from_ts, to_ts). Idempotent via INSERT OR REPLACE.
        """
        sql = """
            INSERT OR REPLACE INTO telemetry_1m
                (ts,
                 rpm_avg, rpm_min, rpm_max,
                 hz_avg,  hz_min,  hz_max,
                 kw_avg,  kw_min,  kw_max,
                 oil_p_avg, cool_t_avg, batt_avg,
                 v_ab_avg, i_a_avg,
                 samples)
            SELECT
                CAST(ts/60 AS INTEGER)*60,
                AVG(rpm),   MIN(rpm),   MAX(rpm),
                AVG(hz),    MIN(hz),    MAX(hz),
                AVG(kw),    MIN(kw),    MAX(kw),
                AVG(oil_p), AVG(cool_t), AVG(batt),
                AVG(v_ab),  AVG(i_a),
                COUNT(*)
            FROM telemetry
            WHERE ts >= ? AND ts < ?
            GROUP BY CAST(ts/60 AS INTEGER)
        """
        with self._writer() as c:
            cur = c.execute(sql, (from_ts, to_ts))
            return cur.rowcount or 0

    def aggregate_rollup_1h(self, from_ts: float, to_ts: float) -> int:
        """Aggregate 1-minute rollups into 1-hour buckets in [from_ts, to_ts).

        Sourced from telemetry_1m (which is retained ~90 d, far longer than
        raw's 7 d) so hourly history survives well past the raw window —
        without this, all history older than the 1m horizon was silently
        lost despite the schema/config advertising ~2 years. Idempotent via
        INSERT OR REPLACE. runtime_s is not derivable from the 1m rollup and
        is left 0.

        Averages are SAMPLE-WEIGHTED (M-9): a plain AVG() of the minute
        averages double-counts sparse minutes (a minute with 1 sample would
        weigh the same as one with 120), skewing the hourly rpm/kw/temp toward
        gappy minutes. SUM(x_avg*samples)/SUM(samples) recovers the true
        time-weighted mean. kwh = Σ(minute kW averages)/60 — each present
        minute contributes kw_avg × 1/60 h of energy; missing minutes have no
        data and are honestly excluded.
        """
        sql = """
            INSERT OR REPLACE INTO telemetry_1h
                (ts, rpm_avg, hz_avg, kw_avg, oil_p_avg, cool_t_avg, batt_avg,
                 samples, runtime_s, kwh)
            SELECT
                CAST(ts/3600 AS INTEGER)*3600,
                SUM(rpm_avg * samples)    / NULLIF(SUM(samples), 0),
                SUM(hz_avg * samples)     / NULLIF(SUM(samples), 0),
                SUM(kw_avg * samples)     / NULLIF(SUM(samples), 0),
                SUM(oil_p_avg * samples)  / NULLIF(SUM(samples), 0),
                SUM(cool_t_avg * samples) / NULLIF(SUM(samples), 0),
                SUM(batt_avg * samples)   / NULLIF(SUM(samples), 0),
                COALESCE(SUM(samples), 0),
                0,
                COALESCE(SUM(kw_avg), 0.0) / 60.0
            FROM telemetry_1m
            WHERE ts >= ? AND ts < ?
            GROUP BY CAST(ts/3600 AS INTEGER)
        """
        with self._writer() as c:
            cur = c.execute(sql, (int(from_ts), int(to_ts)))
            return cur.rowcount or 0

    # ─── events ────────────────────────────────────────────────────────
    def write_event(self, severity: str, type_: str, message: str, meta: str | None = None) -> int:
        with self._writer() as c:
            cur = c.execute(
                "INSERT INTO events (ts, severity, type, message, meta) VALUES (?, ?, ?, ?, ?)",
                (time.time(), severity, type_, message, meta),
            )
            return cur.lastrowid or 0

    def read_events(
        self,
        limit: int = 200,
        from_ts: float | None = None,
        to_ts: float | None = None,
        severities: Iterable[str] | None = None,
        type_: str | None = None,
    ) -> list[dict]:
        clauses = []
        args: list = []
        if from_ts is not None:
            clauses.append("ts >= ?")
            args.append(from_ts)
        if to_ts is not None:
            clauses.append("ts <= ?")
            args.append(to_ts)
        if severities:
            ss = list(severities)
            clauses.append(f"severity IN ({','.join('?' for _ in ss)})")
            args.extend(ss)
        if type_:
            clauses.append("type = ?")
            args.append(type_)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT id, ts, severity, type, message, meta FROM events {where} ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._reader() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ─── alarms ────────────────────────────────────────────────────────
    def raise_alarm(self, code: str, desc: str, severity: str, raw: int) -> bool:
        """Returns True if this is a *new* raise (idempotent)."""
        with self._writer() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO alarms_active (code, desc, severity, raised_at, raw) VALUES (?, ?, ?, ?, ?)",
                (code, desc, severity, time.time(), raw),
            )
            return (cur.rowcount or 0) > 0

    def clear_alarm(self, code: str) -> bool:
        with self._writer() as c:
            cur = c.execute("DELETE FROM alarms_active WHERE code = ?", (code,))
            return (cur.rowcount or 0) > 0

    def active_alarms(self) -> list[dict]:
        with self._reader() as c:
            rows = c.execute(
                "SELECT code, desc, severity, raised_at, raw FROM alarms_active ORDER BY raised_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── derived stats (status panel surfaces these) ───────────────────
    # These are cheap (events table is indexed on ts and pruned by
    # retention) but we still cache them implicitly via the prime-poll
    # cadence: status.py builds them once per /api/status call.

    def count_engine_starts(self) -> int:
        """Total times the engine entered 'cranking' from any prior state.

        The H-100's Modbus map doesn't expose a start counter (see
        registers/h100.yaml note), so we derive it from the TRANSITION
        event stream. The state-machine writes a "→ cranking" event on
        every transition into cranking, so this is the canonical count.
        """
        with self._reader() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE type = 'TRANSITION' AND message LIKE '%→ cranking%'"
            ).fetchone()
        return int(row["n"] if row else 0)

    def last_transfer_to_gen(self) -> dict | None:
        """Most recent transition into 'running' state (load on gen).

        Returns {ts, message} or None. The H-100 doesn't expose an ATS
        contact register on this map, so this is our best proxy for
        when the load was last on the generator.
        """
        with self._reader() as c:
            row = c.execute(
                "SELECT ts, message FROM events "
                "WHERE type = 'TRANSITION' AND message LIKE '%→ running%' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def count_transfers_since(self, since_ts: float) -> int:
        """Count transitions into 'running' since `since_ts` (unix seconds)."""
        with self._reader() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE type = 'TRANSITION' AND message LIKE '%→ running%' "
                "AND ts >= ?",
                (since_ts,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def last_alarm_event(self) -> dict | None:
        """Most recent type='ALARM' event of severity warn/alarm (raise or clear).

        Used by the Live view's "Last alarm" line so it remains useful
        when no alarms are currently active.
        """
        with self._reader() as c:
            row = c.execute(
                "SELECT ts, severity, message, meta FROM events "
                "WHERE type = 'ALARM' AND severity IN ('warn', 'alarm') "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ─── audit ─────────────────────────────────────────────────────────
    def write_audit(self, operator: str, action: str, detail: str, token: str, result: str) -> int:
        with self._writer() as c:
            cur = c.execute(
                "INSERT INTO audit (ts, operator, action, detail, token, result) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), operator, action, detail, token, result),
            )
            return cur.lastrowid or 0

    def disk_usage_bytes(self) -> int:
        try:
            return self.path.stat().st_size + (self.path.parent / (self.path.name + "-wal")).stat().st_size
        except FileNotFoundError:
            return 0

    # ─── kv ────────────────────────────────────────────────────────────
    def kv_get(self, key: str) -> str | None:
        with self._reader() as c:
            r = c.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return r["v"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        with self._writer() as c:
            c.execute(
                "INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )
