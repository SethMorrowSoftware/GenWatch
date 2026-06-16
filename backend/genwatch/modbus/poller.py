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

from .client import ModbusClient
from .registers import RegisterMap, batch_reads, decode_value

log = logging.getLogger("genwatch.modbus.poller")


# After an outage the rolling success window (maxlen 60) is full of
# failures, and at the prime cadence it takes ~57 good polls (~85 s at
# 1.5 s) for success_pct to climb back over the 95% 'healthy' threshold.
# That left the ATS link non-authoritative — and remote control gated —
# for well over a minute after a clean reconnect. An unbroken streak of
# good polls is a strong recovery signal, so we promote straight back to
# 'healthy' on it while leaving the degrade/lost thresholds untouched.
HEALTHY_RECOVERY_STREAK = 5

# A clean reconnect is ONE outage followed by sustained success — fast-recover
# it. A *flapping* link (repeated short drops) also rebuilds the recovery
# streak every few seconds, which would flap authority and remote-control
# gating on/off (a brief 'healthy' window an operator could slip a command
# through). So the fast path is denied once the link has gone LOST
# FLAP_MAX_EPISODES or more times within FLAP_WINDOW_S; it must then earn
# 'healthy' the slow way (the success_pct hysteresis below).
FLAP_WINDOW_S = 120.0
FLAP_MAX_EPISODES = 2


@dataclass
class CommsHealth:
    state: str = "healthy"   # healthy | degraded | lost
    success_pct: float = 100.0
    last_good_at: float | None = None        # wall-clock, for UI display
    last_attempt_at: float | None = None
    rate_ms: int = 1500
    p95_latency_ms: float = 0.0
    consecutive_failures: int = 0
    # Symmetric to consecutive_failures — an unbroken run of successful
    # samples. Lets _classify fast-recover to 'healthy' after an outage
    # instead of waiting for the whole rolling window to flush its
    # failures (see HEALTHY_RECOVERY_STREAK). Reset on any failure.
    consecutive_successes: int = 0
    # Monotonic timestamp of the last successful *prime* poll. Used by
    # the systemd watchdog so a wall-clock jump (NTP, DST) can't either
    # mask a hung loop or trigger a phantom restart. None until first
    # prime poll succeeds.
    last_prime_good_monotonic: float | None = None


@dataclass
class Reading:
    """Decoded snapshot of all current register values.

    Indexed by register name. Values are post-scale (e.g. frequency=60.0
    not 600). engine_state and alarm_state stay raw int — the state
    machine layer turns them into semantic names.

    ``value_ages`` carries a monotonic timestamp per successfully-decoded
    register name. The poller stamps it on every successful decode and
    uses it in ``_poll_tier`` to evict last-good values that have aged
    past ``TIER_STALE_MULTIPLIER × tier_cadence`` — without this, a
    base-tier value decoded once at boot can linger forever in
    ``values`` and masquerade as fresh to alarm comparators and UI
    metrics. ``ts`` stays wall-clock for UI display; ``value_ages`` is
    monotonic so NTP steps can't fool the staleness check.
    """
    values: dict[str, float | int] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    value_ages: dict[str, float] = field(default_factory=dict)

    def get(self, name: str, default=None):
        return self.values.get(name, default)


# How many tier-cadences a register's last successful decode can age
# before we drop it from ``Reading.values``. At 1.5 s prime cadence
# this is ~4.5 s — long enough to ride through a one-cycle batch failure
# that the fan-out path recovers from, short enough that a sustained
# per-register failure surfaces as None to downstream consumers (alarm
# comparators, status API, state machine derivations) instead of a
# stale value that looks fresh. Per-tier so a slow base poll doesn't
# evict legitimate base values while prime hums along.
TIER_STALE_MULTIPLIER = 3


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
        # Monotonic timestamps of transitions INTO 'lost', for flap detection
        # in _classify (a flapping link must not keep fast-recovering).
        self._lost_episodes: deque[float] = deque(maxlen=32)

        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Pre-compute batched reads per tier. apply_regmap() rebuilds
        # these under _regmap_lock when the operator reloads h100.yaml.
        self._prime_batches = batch_reads(regmap.tier("prime"))
        self._base_batches = batch_reads(regmap.tier("base"))
        # Serializes the (regmap, _prime_batches, _base_batches) triple so
        # a mid-cycle hot-reload can't leave the poller decoding new
        # registers against words read for the old map. _poll_tier
        # takes a brief snapshot under this lock at the top of each
        # poll; apply_regmap holds it across the swap.
        self._regmap_lock = asyncio.Lock()
        log.info(
            "Poller batches: prime=%d reads, base=%d reads",
            len(self._prime_batches), len(self._base_batches),
        )

    async def apply_regmap(self, new_regmap: RegisterMap) -> None:
        """Swap the register map mid-run.

        Re-computes the prime/base batch tables from the new tier
        membership so subsequent polls hit the operator's edited
        addresses. Holds _regmap_lock so any in-flight _poll_tier sees
        a consistent (regmap, batches) snapshot — never new batches
        with old register definitions or vice versa.

        Called by POST /api/registers/reload after the YAML edit + parse
        succeeds. The Modbus client is not torn down; the existing TCP /
        serial connection keeps serving.
        """
        new_prime = batch_reads(new_regmap.tier("prime"))
        new_base = batch_reads(new_regmap.tier("base"))
        async with self._regmap_lock:
            old_prime, old_base = len(self._prime_batches), len(self._base_batches)
            self.regmap = new_regmap
            self._prime_batches = new_prime
            self._base_batches = new_base
            self.health.rate_ms = new_regmap.prime_poll_ms
        log.info(
            "Poller register-map reloaded: prime %d→%d batches, base %d→%d batches",
            old_prime, len(new_prime), old_base, len(new_base),
        )

    @property
    def is_running(self) -> bool:
        return self._running

    # ---- lifecycle ----
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Kick off a base poll immediately so the UI has data even
        # before the first base interval elapses.
        await self._poll_tier("base")
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
        # Read cadence each iteration so apply_regmap() picks up the new
        # prime_poll_ms without a restart.
        while self._running:
            period = self.regmap.prime_poll_ms / 1000.0
            t0 = time.monotonic()
            try:
                await self._poll_tier("prime")
            except Exception as e:  # noqa: BLE001
                log.exception("prime poll crashed: %s", e)
            elapsed = time.monotonic() - t0
            sleep = max(0.0, period - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                break

    async def _loop_base(self) -> None:
        while self._running:
            period = self.regmap.base_poll_ms / 1000.0
            t0 = time.monotonic()
            try:
                await self._poll_tier("base")
            except Exception as e:  # noqa: BLE001
                log.exception("base poll crashed: %s", e)
            elapsed = time.monotonic() - t0
            sleep = max(0.0, period - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                break

    async def _watchdog(self) -> None:
        while self._running:
            threshold = (self.regmap.prime_poll_ms * 3) / 1000.0
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            mono_last = self.health.last_prime_good_monotonic
            if mono_last is None:
                continue
            silence = time.monotonic() - mono_last
            if silence > threshold and self.health.state != "lost":
                log.warning("Comms LOST — %.1fs since last good prime poll", silence)
                # Drive the transition through the same counters _classify
                # uses (rather than poking state directly): reset the recovery
                # streak and bump failures to the LOST threshold so a single
                # later success can't immediately un-LOST the link — it must
                # rebuild the streak and clear the flap gate like any other
                # reconnect. Record the episode for flap accounting.
                self.health.consecutive_successes = 0
                self.health.consecutive_failures = max(self.health.consecutive_failures, 3)
                self.health.state = "lost"
                self._lost_episodes.append(time.monotonic())

    # ---- batch execution ----
    async def _poll_tier(self, tier: str, batches: list[tuple[int, int]] | None = None) -> None:
        # Snapshot regmap + batches atomically so a mid-cycle hot-reload
        # via /api/registers/reload doesn't leave us decoding registers
        # from one map against words read for another. apply_regmap()
        # acquires the same lock when swapping.
        async with self._regmap_lock:
            regmap = self.regmap
            if batches is None:
                batches = self._prime_batches if tier == "prime" else self._base_batches
        if not batches:
            return
        # Maps start-of-batch addr → (words, failed_offsets) successfully
        # read covering that range. failed_offsets carries the per-word
        # indices that the single-register fan-out couldn't read so the
        # decoder can skip those registers rather than substitute zeros.
        results: list[tuple[int, list[int], set[int]]] = []
        any_batch_ok = False
        for start, count in batches:
            r = await self.client.read(start, count, fc=regmap.read_fc)
            if r.ok and r.words is not None:
                self._record(True, r.elapsed_ms)
                results.append((start, list(r.words), set()))
                any_batch_ok = True
                continue
            # Batch read failed. Rather than blanking every register in the
            # batch for this cycle, fall back to single-register reads so
            # that one bad register (or a transient exception code on a
            # specific address) can't take out an entire telemetry block.
            log.debug(
                "batch %#06x+%d failed (%s) — falling back to single-register reads",
                start, count, r.error or "?",
            )
            fb = await self._fallback_singles(start, count, regmap.read_fc)
            if fb is not None:
                fb_words, fb_failed = fb
                results.append((start, fb_words, fb_failed))
                any_batch_ok = True
                # Record ONE comms-health sample per batch — not one per
                # fan-out single. Otherwise a handful of unreadable
                # registers flood the 60-sample rolling window and flip
                # comms to LOST (3 consecutive failures) even though the
                # rest of the block reads fine. Fan-out recovered data, so
                # the link is alive → count the batch as a success.
                self._record(True, r.elapsed_ms)
            else:
                # Not a single register in the batch could be read — the
                # link is down for this range. One failure sample.
                self._record(False, r.elapsed_ms)

        # Decode every register whose address falls within a successful
        # batch. Registers whose words include a fan-out failure are
        # skipped entirely (rather than decoded against a sentinel zero),
        # preserving the previous good value in self.reading.values.
        new_values: dict[str, float | int] = dict(self.reading.values)
        new_ages: dict[str, float] = dict(self.reading.value_ages)
        mono_now = time.monotonic()
        for reg in regmap.tier(tier):
            for start, words, failed_offsets in results:
                if not (start <= reg.addr and (start + len(words)) >= reg.addr + reg.words):
                    continue
                offset = reg.addr - start
                reg_offsets = range(offset, offset + reg.words)
                if any(o in failed_offsets for o in reg_offsets):
                    # One or more words for this register failed even
                    # after fan-out. Leave the prior value in place
                    # rather than overwriting with a sentinel that could
                    # trip an out-of-range alarm comparator. Per-tier
                    # eviction below will retire the entry if the
                    # failure persists across multiple cycles.
                    log.debug(
                        "skipping decode of %s @0x%04X — fan-out read failed",
                        reg.name, reg.addr,
                    )
                    break
                reg_words = words[offset : offset + reg.words]
                decoded = decode_value(reg, reg_words)
                if decoded is not None:
                    new_values[reg.name] = decoded
                    new_ages[reg.name] = mono_now
                break

        # Per-tier TTL on last-good values. A register that failed to
        # decode this cycle keeps its prior value (handled above) — but
        # only up to TIER_STALE_MULTIPLIER × tier_cadence. Past that
        # threshold the value is dropped: a stale coolant_temp from
        # boot would otherwise look fresh forever to consumers that
        # don't track ages (e.g. the YAML alarm comparators). Downstream
        # consumers already handle None gracefully (`values.get(...)`
        # returns None and the derivation rules skip the register), so
        # eviction degrades to a safe "metric unknown" rather than a
        # phantom-fresh datum.
        tier_cadence_s = (regmap.prime_poll_ms if tier == "prime" else regmap.base_poll_ms) / 1000.0
        stale_threshold_s = tier_cadence_s * TIER_STALE_MULTIPLIER
        for reg in regmap.tier(tier):
            age_ts = new_ages.get(reg.name)
            if age_ts is None:
                # Never decoded successfully yet — nothing to evict.
                # The first-poll case is naturally handled because the
                # value dict won't have this key either.
                continue
            if (mono_now - age_ts) >= stale_threshold_s:
                if reg.name in new_values:
                    log.debug(
                        "evicting stale value for %s "
                        "(last decoded %.1fs ago, threshold %.1fs)",
                        reg.name, mono_now - age_ts, stale_threshold_s,
                    )
                    new_values.pop(reg.name, None)
                # Drop the age entry too so it doesn't linger forever
                # for a register the operator later removes from YAML.
                new_ages.pop(reg.name, None)

        self.reading = Reading(values=new_values, ts=time.time(), value_ages=new_ages)

        # Heartbeat for the systemd watchdog. It must mean "engine-state
        # detection is alive", not merely "some prime register was
        # readable". Stamp only when EVERY register the engine-state rules
        # depend on (output_status_1 / output_status_7 in the H-100 map)
        # got a fresh decode THIS cycle. A poll where the contiguous state
        # block failed but a far-flung prime single (active_alarm_count,
        # quiettest_status, key_switch_state…) succeeded must not satisfy
        # the watchdog — that's the M1 gap where a frozen state block hid
        # behind an unrelated readable register. For a map without
        # engine_state_bits (the ATS-Pi poller) fall back to "any batch ok"
        # so its own comms-LOST detection still works.
        if tier == "prime":
            state_regs = {rule.register for rule in regmap.engine_state_bits}
            if state_regs:
                state_fresh = all(new_ages.get(n) == mono_now for n in state_regs)
            else:
                state_fresh = any_batch_ok
            if state_fresh:
                self.health.last_prime_good_monotonic = time.monotonic()

        try:
            await self.callback(tier, self.reading, self.health)
        except Exception as e:  # noqa: BLE001
            log.exception("poll callback failed: %s", e)

    async def _fallback_singles(
        self, start: int, count: int, read_fc: int,
    ) -> tuple[list[int], set[int]] | None:
        """Re-read a failed batch one register at a time.

        Returns (words, failed_offsets):
          - words is a list of `count` ints covering [start, start+count).
            Failed positions hold 0 as a placeholder so the list stays
            the right length; callers MUST check failed_offsets before
            decoding any register touching those positions.
          - failed_offsets is the set of per-word indices that still
            failed after fan-out, so the poller can skip decoding those
            registers rather than emit a sentinel zero.

        Returns None if *every* single-register read failed (link is
        truly down — let the caller skip this batch entirely).
        """
        words: list[int] = []
        failed: set[int] = set()
        any_ok = False
        for offset in range(count):
            r = await self.client.read(start + offset, 1, fc=read_fc)
            # NB: comms health is recorded once per batch by the caller —
            # not here — so a few unreadable registers in a fan-out don't
            # flood the rolling window and flip comms to LOST.
            if r.ok and r.words:
                words.append(int(r.words[0]))
                any_ok = True
            else:
                words.append(0)
                failed.add(offset)
        return (words, failed) if any_ok else None

    # ---- comms health ----
    def _record(self, ok: bool, elapsed_ms: float | None = None) -> None:
        """Record one comms-health sample. Called once per logical batch
        (a fan-out of many singles still counts as one sample) so a few
        unreadable registers can't dominate the rolling window."""
        now = time.time()
        self.health.last_attempt_at = now
        self._results.append(ok)
        if elapsed_ms:
            self._latencies.append(elapsed_ms)
        if ok:
            self.health.last_good_at = now
            self.health.consecutive_failures = 0
            self.health.consecutive_successes += 1
        else:
            self.health.consecutive_failures += 1
            self.health.consecutive_successes = 0

        if self._results:
            n_ok = sum(1 for x in self._results if x)
            self.health.success_pct = round(100.0 * n_ok / len(self._results), 1)
        if self._latencies:
            ordered = sorted(self._latencies)
            # Guard the small-window case: nearest-rank p95 of a nearly
            # empty window collapses toward the minimum (e.g. n=2 → index
            # 0), making a degrading link look fast right after a
            # reconnect. Report the max until the window has some depth.
            if len(ordered) < 5:
                self.health.p95_latency_ms = round(ordered[-1], 1)
            else:
                self.health.p95_latency_ms = round(ordered[int(0.95 * (len(ordered) - 1))], 1)

        new_state = self._classify()
        if new_state != self.health.state:
            if new_state == "lost":
                self._lost_episodes.append(time.monotonic())
            log.info("Comms %s -> %s (%.1f%% success)", self.health.state, new_state, self.health.success_pct)
            self.health.state = new_state

    def _classify(self) -> str:
        if self.health.consecutive_failures >= 3:
            return "lost"
        # Fast recovery: an unbroken streak of good polls returns us to
        # 'healthy' without waiting for the 60-sample window to flush an
        # outage's failures (~85 s at the prime cadence). A flapping link
        # never builds the streak — any failure resets it — so this does
        # not weaken the anti-flap hysteresis on the degrade path below.
        if self.health.consecutive_successes >= HEALTHY_RECOVERY_STREAK:
            # Allow the fast path only if the link isn't flapping: a single
            # recent outage (a clean reconnect) fast-recovers; repeated
            # outages within the window must earn 'healthy' the slow way so
            # authority / remote control don't flap with the link.
            now = time.monotonic()
            recent_flaps = sum(1 for t in self._lost_episodes if now - t <= FLAP_WINDOW_S)
            if recent_flaps < FLAP_MAX_EPISODES:
                return "healthy"
        if self.health.success_pct < 95 or self.health.consecutive_failures >= 1:
            return "degraded"
        return "healthy"
