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

from ingest.steam import APPDETAILS, FetchFailed, fetch_details


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
