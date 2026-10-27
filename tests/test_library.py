"""Leaving owned games out of recommendations, through library-service (D67).

Two layers, as with narration: `api.library.owned_appids` against the shapes the service answers with,
and `/recommend` using it. The property both protect is that the filter fails open -- a slow or broken
library-service costs at most its timeout and never a recommendation.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from api import library, main
from gamerec.mindb import Hit
from gamerec.names import NameIndex
from tests.test_api_probes import Present, _Embedder


def service(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def answering(*appids: int):
    asked: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, json={"appIds": list(appids)})

    return handle, asked


def test_owned_appids_asks_for_owned_games_only():
    handle, asked = answering(413150, 105600)
    owned = library.owned_appids("u1", "http://library-service:8080", client=service(handle))

    assert owned == {413150, 105600}
    assert asked[0].url.path == "/api/v1/users/u1/library/ids"
    assert asked[0].url.params["status"] == "OWNED"


def test_a_user_id_cannot_change_which_url_is_called():
    handle, asked = answering()
    library.owned_appids("../admin?x=", "http://library-service:8080", client=service(handle))
    assert asked[0].url.raw_path.startswith(b"/api/v1/users/..%2Fadmin%3Fx%3D/library/ids")


def test_the_timeout_is_300_ms():
    seen = {}

    class Recording:
        def get(self, url, params, timeout):
            seen["timeout"] = timeout
            return httpx.Response(200, json={"appIds": []}, request=httpx.Request("GET", url))

    library.owned_appids("u1", "http://library-service:8080", client=Recording())
    assert seen["timeout"] == 0.3


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("connection refused"),
])
def test_an_unreachable_service_is_none_and_a_warning(failure, caplog):
    def handle(request):
        raise failure

    with caplog.at_level("WARNING"):
        assert library.owned_appids("u1", "http://x", client=service(handle)) is None
    assert "recommending unfiltered" in caplog.text


@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"status": 500}),
    httpx.Response(400, json={"status": 400}),
    httpx.Response(200, json={"unexpected": "shape"}),
])
def test_an_answer_that_is_not_a_list_of_ids_is_none(response):
    assert library.owned_appids("u1", "http://x", client=service(lambda request: response)) is None


# /recommend with a user.

HITS = [Hit(f"appid:{i}", 0.9 - i / 100, json.dumps({"appid": i, "name": f"Game {i}"}).encode())
        for i in range(1, 8)]


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "config", dataclasses.replace(
        main.config, corpus_dir=tmp_path, library_url="http://library-service:8080"))
    main.state.clear()
    main.state.update({"embedder": _Embedder(), "names": NameIndex([]), "mindb": Searching(HITS)})
    return main.state


class Searching(Present):
    """Records the k it was asked for, so the over-fetch for excluded games can be checked."""

    def __init__(self, hits):
        super().__init__(hits)
        self.asked: list[int] = []

    def search(self, query, top_k):
        self.asked.append(top_k)
        return super().search(query, top_k)


def recommend(**kwargs):
    return main.recommend(q="something relaxing", seed=None, k=3, narrate_results=False, **kwargs)


def test_owned_games_are_left_out_and_the_page_still_fills(api, monkeypatch):
    monkeypatch.setattr(main, "owned_appids", lambda user, url: {1, 3})

    body = recommend(user="u1")

    assert [r["appid"] for r in body["results"]] == [2, 4, 5]
    assert body["library_filtered"] is True
    assert api["mindb"].asked == [3 + 2]


def test_library_service_failing_leaves_the_results_unfiltered(api, monkeypatch):
    monkeypatch.setattr(main, "owned_appids", lambda user, url: None)

    body = recommend(user="u1")

    assert [r["appid"] for r in body["results"]] == [1, 2, 3]
    assert body["library_filtered"] is False


def test_no_user_means_no_call(api, monkeypatch):
    def must_not_be_called(user, url):
        raise AssertionError("library-service was called without a user")

    monkeypatch.setattr(main, "owned_appids", must_not_be_called)
    assert recommend(user=None)["library_filtered"] is None


def test_no_library_service_configured_means_no_call(api, monkeypatch):
    monkeypatch.setattr(main, "config", dataclasses.replace(main.config, library_url=""))
    monkeypatch.setattr(main, "owned_appids", lambda user, url: pytest.fail("called"))
    assert recommend(user="u1")["library_filtered"] is None
