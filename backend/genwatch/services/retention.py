"""Periodic retention + rollup.

Runs every 5 minutes:
  1. Aggregate raw telemetry into telemetry_1m (idempotent).
  2. Aggregate telemetry_1m into telemetry_1h (idempotent) so history
     survives past the 1-minute rollup's horizon.
  3. Prune raw / 1m / 1h / events past their configured retention.
  4. Checkpoint (TRUNCATE) the WAL to bound its on-disk size.

All DB work is dispatched via asyncio.to_thread so a multi-thousand-row
prune never blocks the event loop. Prunes themselves delete in bounded
chunks (see Database._prune_chunked).
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
        # Aggregate the window [now - 1h, now - 1min] into 1-minute buckets
        # — leave the current minute alone so we don't half-aggregate live
        # data.
        rows_1m = await asyncio.to_thread(self.db.aggregate_rollup_1m, now - 3600, now - 60)
        # Roll the last couple of hours of 1-minute buckets up into hourly
        # buckets. Re-aggregating the current (partial) hour each tick is
        # safe — INSERT OR REPLACE refreshes it as more 1m data lands.
        # Without this, all history older than the 1m horizon (90 d) was
        # silently lost despite the config advertising ~2 years.
        rows_1h = await asyncio.to_thread(self.db.aggregate_rollup_1h, now - 2 * 3600, now)

        raw_pruned = await asyncio.to_thread(
            self.db.prune_raw_telemetry, now - self.cfg.raw_days * 86400
        )
        rollup_1m_pruned = await asyncio.to_thread(
            self.db.prune_rollup_1m, now - self.cfg.rollup_1m_days * 86400
        )
        rollup_1h_pruned = 0
        if self.cfg.rollup_1h_days > 0:
            rollup_1h_pruned = await asyncio.to_thread(
                self.db.prune_rollup_1h, now - self.cfg.rollup_1h_days * 86400
            )
        events_pruned = 0
        if self.cfg.events_days > 0:
            events_pruned = await asyncio.to_thread(
                self.db.prune_events, now - self.cfg.events_days * 86400
            )

        # Bound the WAL after the prunes so it can't grow without limit on
        # the SD card (best-effort; defers if a reader holds a lock).
        await asyncio.to_thread(self.db.checkpoint)

        if rows_1m or rows_1h or raw_pruned or rollup_1m_pruned or rollup_1h_pruned or events_pruned:
            log.info(
                "retention: rolled 1m=%d 1h=%d, pruned raw=%d rollup_1m=%d rollup_1h=%d events=%d",
                rows_1m, rows_1h, raw_pruned, rollup_1m_pruned, rollup_1h_pruned, events_pruned,
            )
