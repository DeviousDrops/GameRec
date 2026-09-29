"""That the frontend is served, and that serving it did not eat the API (D50).

The mount is at "/", which matches every path there is. Starlette resolves routes in registration
order, so the API keeps working only because the mount is registered last -- a property of where four
lines sit in a file, invisible at the point of use, and the kind of thing a refactor reorders without
noticing. Hence a test per route that the mount would otherwise swallow.
"""

from __future__ import annotations

import dataclasses

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main
from tests.test_api_probes import Present, _Embedder  # noqa: F401  (fixtures reuse them)

from gamerec.names import NameIndex


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A client with MinDB stubbed out.

    Deliberately not `with TestClient(...)`: the context manager runs the startup event, which
    replaces the stub with a client dialled at MINDB_ADDR. On a machine with the dev compose stack up
    that quietly succeeds, and the test then passes or fails on whatever is in a real index.
    """
    monkeypatch.setattr(main, "config", dataclasses.replace(main.config, corpus_dir=tmp_path))
    main.state.clear()
    main.state.update({"embedder": _Embedder(), "names": NameIndex([]), "mindb": Present()})
    return TestClient(main.app)


def test_the_page_is_served_at_the_root(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "GameRec" in response.text


def test_the_assets_the_page_asks_for_exist(client):
    """index.html references these by path. A rename that misses one leaves a page that loads and
    then does nothing, with a 404 only visible in a browser console."""
    for path, kind in (("/style.css", "text/css"), ("/app.js", "javascript")):
        response = client.get(path)
        assert response.status_code == 200, path
        assert kind in response.headers["content-type"], path


@pytest.mark.parametrize("path", ["/livez", "/readyz", "/health"])
def test_the_probes_still_answer_json(client, path):
    """The failure this catches: a mount registered before the routes turns every probe into a 404,
    which makes Kubernetes drop the pod out of its Service -- a total outage from a static file."""
    response = client.get(path)

    assert response.status_code == 200
    assert response.json()["status"] in {"alive", "ready", "ok"}


def test_recommend_still_answers_json(client):
    response = client.get("/recommend", params={"q": "something relaxing", "narrate": "false"})

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_the_banner_only_reads_health_fields_the_public_endpoint_returns(client):
    """The bug this catches, which shipped and went unnoticed: the banner read `body.mindb.kernel`,
    /health withholds everything under `mindb` unless HEALTH_DETAIL is set (D56), and the resulting
    TypeError landed in a bare catch that said "The service is not answering /health." A healthy
    service was reported as down by the page describing it, for as long as nobody looked.

    HEALTH_DETAIL is false here because it is false in the deployment that serves this page. Only
    unguarded reads count: `body.mindb?.kernel` is a deliberate "show it if this deployment
    publishes it", and `body.mindb.kernel` is the bug.
    """
    public = set(client.get("/health").json())
    source = (Path(main.__file__).resolve().parent.parent / "web" / "app.js").read_text()
    start = source.index("async function showCorpusSize()")
    end = source.index("function card(", start)
    # Comments are stripped first: this file's own prose names the field that used to be the bug.
    code = re.sub(r"//.*", "", source[start:end])
    read = set(re.findall(r"body[.](\w+)(?![\w?])", code))

    assert read, "the banner stopped reading /health; this test is now checking nothing"
    assert read <= public, (
        f"the banner reads {sorted(read - public)} off /health, which the public response withholds")


def test_an_unknown_path_is_still_a_miss(client):
    """html=True serves index.html for a directory, not for everything. A 200 here would mean every
    typo in a fetch() silently returns the page, and JSON parsing it is the error you would debug."""
    assert client.get("/not-a-route").status_code == 404
