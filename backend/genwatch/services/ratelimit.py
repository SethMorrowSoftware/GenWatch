"""In-memory token-bucket rate limiter.

Used to slow brute-force attempts on the login endpoint. State is
process-local — restarting the service resets all buckets, which is
fine for our threat model (a single Pi on a LAN behind Tailscale).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    """Token-bucket: ``capacity`` tokens max, ``refill_per_s`` per second.

    Each ``check(key)`` consumes one token. Returns True if allowed,
    False if the bucket is empty. ``reset(key)`` clears the bucket
    (e.g. after a successful login).
    """

    def __init__(self, *, capacity: float, refill_per_s: float):
        self.capacity = float(capacity)
        self.refill = float(refill_per_s)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                self._buckets[key] = _Bucket(tokens=self.capacity - 1, last=now)
                return True
            elapsed = now - b.last
            b.tokens = min(self.capacity, b.tokens + elapsed * self.refill)
            b.last = now
            if b.tokens >= 1:
                b.tokens -= 1
                return True
            return False

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def retry_after_s(self, key: str) -> int:
        """Seconds until ``check(key)`` would succeed again. 0 if it would now."""
        with self._lock:
            b = self._buckets.get(key)
            if b is None or b.tokens >= 1:
                return 0
            needed = 1 - b.tokens
            return max(1, int(needed / max(self.refill, 1e-9)))
