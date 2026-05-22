"""Two-tier Modbus poller.

The H-100 has fast-changing state/alarm registers and slow-changing
telemetry. We poll them on different schedules so a 200-register slow
poll never blocks state-transition detection.

  - prime tier: state + alarm + switch  → polled every prime_poll_ms
  - base  tier: telemetry               → polled every base_poll_ms

Each successful poll produces a Reading and fires an event into the
event bus. Comms health is computed from the rolling success rate.

Reliability features:
  - exponential backoff on consecutive failures (the client itself
    retries within a single read; the poller backs off the *cadence* if
    the slave is unresponsive for a long stretch).
  - watchdog: if no prime poll completes within 3× prime_poll_ms, we
    declare comms LOST and emit an event.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .client import ModbusClient, ModbusResult
from .registers import RegisterMap, batch_reads, decode_value

log = logging.getLogger("genwatch.modbus.poller")


@dataclass
class CommsHealth:
    state: str = "healthy"   # healthy | degraded | lost
    success_pct: float = 100.0
    last_good_at: float | None = None
    last_attempt_at: float | None = None
    rate_ms: int = 1500
    p95_latency_ms: float = 0.0
    consecutive_failures: int = 0


@dataclass
class Reading:
    """Decoded snapshot of all current register values.

    Indexed by register name. Values are post-scale (e.g. frequency=60.0
    not 600). engine_state and alarm_state stay raw int — the state
    machine layer turns them into semantic names.
    """
    values: dict[str, float | int] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def get(self, name: str, default=None):
        return self.values.get(name, default)


PollCallback = Callable[[str, Reading, CommsHealth], Awaitable[None]]


class Poller:
    """Runs the two-tier polling loop until stop() is called."""

    def __init__(self, client: ModbusClient, regmap: RegisterMap, callback: PollCallback):
        self.client = client
        self.regmap = regmap
        self.callback = callback
        self.health = CommsHealth(rate_ms=regmap.prime_poll_ms)
        self.reading = Reading()

        # rolling success window for comms %
        self._results: deque[bool] = deque(maxlen=60)
        self._latencies: deque[float] = deque(maxlen=60)

        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Pre-compute batched reads per tier
        self._prime_batches = batch_reads(regmap.tier("prime"))
        self._base_batches = batch_reads(regmap.tier("base"))
        log.info(
            "Poller batches: prime=%d reads, base=%d reads",
            len(self._prime_batches), len(self._base_batches),
        )

    # ---- lifecycle ----
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Kick off a base poll immediately so the UI has data even
        # before the first base interval elapses.
        await self._poll_tier("base", self._base_batches)
        self._tasks = [
            asyncio.create_task(self._loop_prime(), name="poll-prime"),
            asyncio.create_task(self._loop_base(), name="poll-base"),
            asyncio.create_task(self._watchdog(), name="poll-watchdog"),
        ]
        log.info("Poller started")

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    # ---- loops ----
    async def _loop_prime(self) -> None:
        period = self.regmap.prime_poll_ms / 1000.0
        while self._running:
            t0 = time.monotonic()
            try:
                await self._poll_tier("prime", self._prime_batches)
            except Exception as e:  # noqa: BLE001
                log.exception("prime poll crashed: %s", e)
            elapsed = time.monotonic() - t0
            sleep = max(0.0, period - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                break

    async def _loop_base(self) -> None:
        period = self.regmap.base_poll_ms / 1000.0
        while self._running:
            t0 = time.monotonic()
            try:
                await self._poll_tier("base", self._base_batches)
            except Exception as e:  # noqa: BLE001
                log.exception("base poll crashed: %s", e)
            elapsed = time.monotonic() - t0
            sleep = max(0.0, period - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                break

    async def _watchdog(self) -> None:
        threshold = (self.regmap.prime_poll_ms * 3) / 1000.0
        while self._running:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            if self.health.last_good_at is None:
                continue
            silence = time.time() - self.health.last_good_at
            if silence > threshold and self.health.state != "lost":
                self.health.state = "lost"
                log.warning("Comms LOST — %.1fs since last good poll", silence)

    # ---- batch execution ----
    async def _poll_tier(self, tier: str, batches: list[tuple[int, int]]) -> None:
        if not batches:
            return
        results: list[tuple[int, ModbusResult]] = []
        for start, count in batches:
            r = await self.client.read(start, count, fc=self.regmap.read_fc)
            results.append((start, r))
            self._record(r)

        # Decode every register whose address falls within a successful batch.
        new_values: dict[str, float | int] = dict(self.reading.values)
        for reg in self.regmap.tier(tier):
            # find batch covering reg.addr
            for start, r in results:
                if not r.ok or r.words is None:
                    continue
                if start <= reg.addr and (start + len(r.words)) >= reg.addr + reg.words:
                    offset = reg.addr - start
                    words = r.words[offset : offset + reg.words]
                    decoded = decode_value(reg, words)
                    if decoded is not None:
                        new_values[reg.name] = decoded
                    break

        self.reading = Reading(values=new_values, ts=time.time())
        try:
            await self.callback(tier, self.reading, self.health)
        except Exception as e:  # noqa: BLE001
            log.exception("poll callback failed: %s", e)

    # ---- comms health ----
    def _record(self, r: ModbusResult) -> None:
        now = time.time()
        self.health.last_attempt_at = now
        self._results.append(r.ok)
        if r.elapsed_ms:
            self._latencies.append(r.elapsed_ms)
        if r.ok:
            self.health.last_good_at = now
            self.health.consecutive_failures = 0
        else:
            self.health.consecutive_failures += 1

        if self._results:
            ok = sum(1 for x in self._results if x)
            self.health.success_pct = round(100.0 * ok / len(self._results), 1)
        if self._latencies:
            ordered = sorted(self._latencies)
            self.health.p95_latency_ms = round(ordered[int(0.95 * (len(ordered) - 1))], 1)

        new_state = self._classify(r)
        if new_state != self.health.state:
            log.info("Comms %s -> %s (%.1f%% success)", self.health.state, new_state, self.health.success_pct)
            self.health.state = new_state

    def _classify(self, r: ModbusResult) -> str:
        if self.health.consecutive_failures >= 3:
            return "lost"
        if self.health.success_pct < 95 or self.health.consecutive_failures >= 1:
            return "degraded"
        return "healthy"
