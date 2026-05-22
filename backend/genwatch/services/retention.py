"""Periodic retention + rollup.

Runs every 5 minutes:
  1. Aggregate raw telemetry from the last completed minute window into
     telemetry_1m (idempotent — re-running is safe).
  2. Delete raw telemetry older than config.retention.raw_days.
  3. Delete 1-min rollups older than config.retention.rollup_1m_days.

Kept simple: SQLite handles thousand-row deletes fast enough that we
don't need a separate background thread.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import RetentionConfig
from ..db import Database

log = logging.getLogger("genwatch.retention")

PERIOD_S = 5 * 60


class RetentionService:
    def __init__(self, db: Database, cfg: RetentionConfig):
        self.db = db
        self.cfg = cfg
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Run once at startup so a fresh boot doesn't accumulate dust.
        await self._tick()
        self._task = asyncio.create_task(self._loop(), name="retention")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(PERIOD_S)
            except asyncio.CancelledError:
                break
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("retention tick failed: %s", e)

    async def _tick(self) -> None:
        now = time.time()
        # Aggregate the window [now - 1h, now - 1min] — leave the current
        # minute alone so we don't half-aggregate live data.
        from_ts = now - 3600
        to_ts = now - 60
        rows = await asyncio.to_thread(self.db.aggregate_rollup_1m, from_ts, to_ts)

        raw_pruned = await asyncio.to_thread(
            self.db.prune_raw_telemetry, now - self.cfg.raw_days * 86400
        )
        rollup_pruned = await asyncio.to_thread(
            self.db.prune_rollup_1m, now - self.cfg.rollup_1m_days * 86400
        )
        events_pruned = 0
        if self.cfg.events_days > 0:
            events_pruned = await asyncio.to_thread(
                self.db.prune_events, now - self.cfg.events_days * 86400
            )
        if rows or raw_pruned or rollup_pruned or events_pruned:
            log.info(
                "retention: rolled %d 1m buckets, pruned raw=%d rollup_1m=%d events=%d",
                rows, raw_pruned, rollup_pruned, events_pruned,
            )
