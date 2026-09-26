"""Request pacing for Steam (D17).

Phase 1 paced with `time.sleep(1.6)` between fetches. That is a fixed delay, not a rate limit, and the
difference shows up the moment anything else takes time: a slow response makes the run *slower* than
the budget rather than using the headroom, and a fast one is still capped at the same figure.

A token bucket paces against a budget instead. Tokens refill at a steady rate and the bucket holds a
small burst, so a stall is absorbed rather than compounded, and the average lands where D17 measured
it: ~35 requests/minute against Steam's ~40/minute refill. Deliberately under the ceiling, because a
429 costs a 20-30 second backoff -- guessing high makes the whole run slower than pacing correctly.

The clock and sleep are injectable so the tests can prove the pacing without spending real seconds on
it. Nothing else should pass them.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

# D17's measured backoff for a 429. Wide and jittered: a fixed sleep means every concurrent caller
# retries in lockstep, which is how a rate limit turns into a thundering herd.
RATE_LIMIT_BACKOFF = (20.0, 30.0)


class RateLimiter:
    """A token bucket. Thread-safe because the ingest job may grow workers later; the lock is held
    only for arithmetic, never across the sleep."""

    def __init__(
        self,
        per_minute: float,
        burst: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.per_second = per_minute / 60.0
        self.burst = max(1, burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self.burst)
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Blocks until a token is free. Returns how long it waited, which is what the tests read."""
        with self._lock:
            wait = self._reserve()
        if wait > 0:
            self._sleep(wait)
        return wait

    def _reserve(self) -> float:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.per_second)
        self._updated = now
        # The token is charged whether or not one is available, so the balance is allowed to go
        # negative and the debt is what the caller waits off. Zeroing it instead would hand the
        # sleeping caller a free token on its next call, and the run would alternate between waiting
        # and not waiting at half the requested rate.
        self._tokens -= 1.0
        if self._tokens >= 0.0:
            return 0.0
        return -self._tokens / self.per_second

    def penalise(self, reason: str = "429") -> float:
        """Steam said no. Empty the bucket and sit out a jittered backoff.

        Emptying matters as much as sleeping: without it the bucket still holds tokens from before the
        rejection, so the first thing the run does after waiting out a rate limit is burst into it
        again.
        """
        low, high = RATE_LIMIT_BACKOFF
        delay = random.uniform(low, high)
        with self._lock:
            self._tokens = 0.0
            self._updated = self._clock()
        log.warning("rate limited (%s); backing off %.1fs", reason, delay)
        self._sleep(delay)
        return delay
