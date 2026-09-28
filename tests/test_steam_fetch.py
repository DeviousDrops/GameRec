"""appdetails response handling (D4).

This file exists because of a live run that put Terraria, ELDEN RING and Counter-Strike in the name
index as `not_a_game`. Nothing errored: appdetails does not key its response by the appid that was
asked for, so indexing by the request missed the payload and the absence read as a verdict.

Two lessons, one test file. Don't trust the envelope key, and never record "Steam did not answer" as
"Steam said no" -- the first is retried next run, the second is permanent.
"""

from __future__ import annotations

import httpx
import pytest

from ingest.steam import (APPDETAILS, STEAMSPY_ALL_PER_MIN, STEAMSPY_PAGE_SIZE, FetchFailed,
                          fetch_details, fetch_popular)
from ingest.throttle import RateLimiter


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


def responder(payload, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == httpx.URL(APPDETAILS).path
        return httpx.Response(status, json=payload)
    return handler


def test_the_envelope_key_is_not_trusted():
    """Terraria: asked for 105600, answered under 1323320, with the real appid inside `data`."""
    payload = {"1323320": {"success": True, "data": {"type": "game", "name": "Terraria",
                                                     "steam_appid": 105600}}}
    with client(responder(payload)) as http:
        data = fetch_details(105600, http)

    assert data["name"] == "Terraria"


def test_the_ordinary_shape_still_works():
    payload = {"4000": {"success": True, "data": {"type": "game", "name": "Garry's Mod",
                                                  "steam_appid": 4000}}}
    with client(responder(payload)) as http:
        assert fetch_details(4000, http)["name"] == "Garry's Mod"


def test_a_genuine_miss_is_none_not_an_error():
    """An unknown appid answers under the requested key with success false. That is a verdict."""
    with client(responder({"999999999": {"success": False}})) as http:
        assert fetch_details(999999999, http) is None


def test_another_game_s_data_is_refused():
    """If the payload is for a different appid, storing it would be wrong in a way nothing
    downstream could detect."""
    payload = {"1": {"success": True, "data": {"type": "game", "name": "Some other game",
                                               "steam_appid": 999}}}
    with client(responder(payload)) as http:
        assert fetch_details(123, http) is None


def test_a_multi_key_response_is_not_guessed_at():
    """Only an unambiguous single-entry envelope is unwrapped by position."""
    payload = {"111": {"success": True, "data": {"steam_appid": 111}},
               "222": {"success": True, "data": {"steam_appid": 222}}}
    with client(responder(payload)) as http:
        assert fetch_details(333, http) is None


def test_an_unanswered_fetch_raises_instead_of_reading_as_a_miss():
    """The bug this prevents: returning None here labels a real game not_a_game for good."""
    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    with client(flaky) as http:
        with pytest.raises(FetchFailed):
            fetch_details(105600, http, attempts=2)


def test_rate_limits_do_not_spend_the_failure_budget():
    """A 429 is not a failure. Without a limiter it backs off on its own clock, so the limiter is
    injected here purely to keep the test instant."""
    calls = {"n": 0}

    def throttled(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={})

    class NoWait:
        def acquire(self):
            return 0.0

        def penalise(self, reason=""):
            return 0.0

    with client(throttled) as http:
        with pytest.raises(FetchFailed) as raised:
            fetch_details(105600, http, attempts=3, limiter=NoWait())

    assert "rate limits" in str(raised.value)
    # Three throttles, not three failures -- the two budgets are separate.
    assert calls["n"] == 3


def steamspy(pages: list[dict]) -> tuple[httpx.Client, list[int]]:
    """A SteamSpy that answers `request=all` from a fixed list of pages, recording what was asked."""
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "steamspy.com"
        number = int(request.url.params["page"])
        asked.append(number)
        return httpx.Response(200, json=pages[number] if number < len(pages) else {})

    return client(handler), asked


def full_page(start: int, reviews: int = 1) -> dict:
    """A page of exactly STEAMSPY_PAGE_SIZE games, so the pager reads it as "there is more"."""
    return {str(start + i): {"name": f"game {start + i}", "positive": reviews, "negative": 0}
            for i in range(STEAMSPY_PAGE_SIZE)}


def instant() -> RateLimiter:
    """SteamSpy's real budget is a minute a page. The tests prove the pacing separately rather than
    spending three minutes to fetch three pages."""
    return RateLimiter(STEAMSPY_ALL_PER_MIN, burst=1, sleep=lambda seconds: None)


def test_one_page_is_not_the_whole_catalogue():
    """The bug this file now also exists for: `--limit 180000` fetched page 0 and stopped, so a fill
    the docs described as three days finished in half an hour with the popular head of the catalogue
    and reported success."""
    http, asked = steamspy([full_page(0), full_page(1000), {"9000": {"name": "last", "positive": 5}}])
    with http:
        rows = fetch_popular(180000, client=http, pacer=instant())

    assert len(asked) == 3, "stopped before the end of the catalogue"
    assert len(rows) == 2001


def test_a_short_page_ends_the_scan():
    """Asking past the end answers an empty object rather than an error, so the short page is the
    only signal that there is nothing more."""
    http, asked = steamspy([full_page(0), {"5": {"name": "tail", "positive": 1}}])
    with http:
        fetch_popular(50000, client=http, pacer=instant())

    assert asked == [0, 1], "kept paging past a short page"


def test_the_limit_bounds_the_number_of_requests():
    """A minute a page is the cost, so the nightly's default limit must still be a single request."""
    http, asked = steamspy([full_page(0), full_page(1000)])
    with http:
        rows = fetch_popular(200, client=http, pacer=instant())

    assert asked == [0] and len(rows) == 200


def test_a_game_that_moves_between_pages_is_not_fetched_twice():
    """The catalogue shifts under a scan that takes hours, and a duplicate here costs an appdetails
    request per duplicate later."""
    moved = {"7": {"name": "moved", "positive": 9, "negative": 1}}
    http, _ = steamspy([full_page(0) | moved, {**moved, "8": {"name": "other", "positive": 1}}])
    with http:
        rows = fetch_popular(50000, client=http, pacer=instant())

    assert [r.appid for r in rows].count(7) == 1


def test_the_lease_is_renewed_between_pages():
    """Paging the catalogue takes hours and the lease TTL is half one. Without this the lease expires
    mid-scan and tonight's CronJob steals it and runs alongside the fill (D17)."""
    renewals = []
    http, _ = steamspy([full_page(0), full_page(1000), {}])
    with http:
        fetch_popular(50000, client=http, pacer=instant(),
                      on_page=lambda: renewals.append(1) or True)

    assert len(renewals) == 2, "renewed once per full page"


def test_losing_the_lease_stops_the_scan():
    http, asked = steamspy([full_page(0), full_page(1000), full_page(2000)])
    with http:
        fetch_popular(50000, client=http, pacer=instant(), on_page=lambda: False)

    assert asked == [0], "kept scanning after the lease was gone"


def test_the_pager_waits_a_minute_between_pages():
    """The pacing itself, on an injected clock: SteamSpy documents one `all` request per 60 seconds."""
    slept, now = [], [0.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    http, _ = steamspy([full_page(0), full_page(1000), {}])
    with http:
        fetch_popular(50000, client=http,
                      pacer=RateLimiter(STEAMSPY_ALL_PER_MIN, burst=1,
                                        clock=lambda: now[0], sleep=sleep))

    assert slept and all(abs(s - 60.0) < 0.01 for s in slept), slept
