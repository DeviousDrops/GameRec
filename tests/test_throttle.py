"""Pacing is the difference between an ingest that finishes and one that gets rate limited (D17).

The clock is fake on purpose. A test that proves 35 requests/minute by taking a minute is a test
nobody runs, and the thing worth pinning is the arithmetic, not the wall clock.
"""

from __future__ import annotations

import pytest

from ingest.throttle import RATE_LIMIT_BACKOFF, RateLimiter


class FakeClock:
    """A clock that only moves when something sleeps -- so every second the limiter waits is visible
    in `slept`, and nothing that does not wait can hide."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(per_minute=60.0, burst=1) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return RateLimiter(per_minute, burst, clock=clock.time, sleep=clock.sleep), clock


def test_the_burst_is_free_and_then_it_is_not():
    bucket, clock = limiter(per_minute=60.0, burst=3)

    assert [bucket.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    # Fourth has to wait for a refill: 60/minute is one per second.
    assert bucket.acquire() == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(1.0)]


def test_the_average_rate_is_what_was_asked_for():
    bucket, clock = limiter(per_minute=35.0, burst=1)

    for _ in range(36):
        bucket.acquire()

    # 35/minute is one every ~1.714s; the burst token makes the first one free.
    assert clock.now == pytest.approx(35 * 60.0 / 35.0, rel=1e-6)


def test_time_spent_elsewhere_is_not_charged_twice():
    """The Phase 1 sleep(1.6) added its delay on top of however long Steam took. A bucket counts
    that time as refill instead, which is the whole reason for the change."""
    bucket, clock = limiter(per_minute=60.0, burst=1)
    bucket.acquire()

    clock.now += 5.0  # a slow response, not a sleep the limiter chose

    assert bucket.acquire() == 0.0
    assert clock.slept == []


def test_the_bucket_cannot_bank_more_than_its_burst():
    bucket, clock = limiter(per_minute=60.0, burst=2)
    clock.now += 3600.0  # idle for an hour

    assert [bucket.acquire() for _ in range(2)] == [0.0, 0.0]
    assert bucket.acquire() > 0.0


def test_a_429_empties_the_bucket_before_backing_off():
    """Sleeping is not enough on its own: if the bucket keeps its tokens, the first thing the run
    does after waiting out a rate limit is burst straight back into one.

    Paced slowly here so the backoff cannot refill the whole burst -- at 6/minute a ~25s wait earns
    about two tokens, so a full bucket of 5 afterwards could only mean the old ones survived.
    """
    bucket, clock = limiter(per_minute=6.0, burst=5)
    for _ in range(5):
        assert bucket.acquire() == 0.0  # drain, then refill the burst by idling
    clock.now += 600.0

    delay = bucket.penalise()

    assert RATE_LIMIT_BACKOFF[0] <= delay <= RATE_LIMIT_BACKOFF[1]
    assert clock.slept == [delay]
    free = 0
    while bucket.acquire() == 0.0:
        free += 1
    assert free <= 3, "the backoff earned at most ~3 tokens, so more means the burst survived it"


def test_a_nonsense_rate_is_rejected_at_construction():
    with pytest.raises(ValueError):
        RateLimiter(0)
