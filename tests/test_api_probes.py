"""What the API does while MinDB is away, and what each probe is allowed to check (D7).

A single-instance MinDB deployed with Recreate means downtime is a normal event, not an incident. The
interesting cases are therefore the ones during a restart: liveness must not notice, readiness must,
and a query must say "later" rather than "broken". The routes are called directly, so no test here
waits on a channel that is meant to be unreachable.
"""

from __future__ import annotations

import dataclasses
import json

import grpc
import numpy as np
import pytest
from fastapi import HTTPException

from api import main
from gamerec.mindb import Hit, Stats
from gamerec.names import IN_CORPUS, NameEntry, NameIndex


class Away:
    """MinDB, restarting. grpc raises the same error for every call, so one class covers all of it."""

    def _fail(self, *args, **kwargs):
        raise _rpc_error(grpc.StatusCode.UNAVAILABLE)

    stats = search = get = _fail


class Present:
    def __init__(self, hits: list[Hit] | None = None) -> None:
        self._hits = hits or []

    def stats(self) -> Stats:
        return Stats(vector_count=200, capacity=5000, dims=384, kernel_name="avx2",
                     fast_int8=True, goarch="amd64", wal_enabled=True, wal_healthy=True)

    def search(self, query, top_k):
        return self._hits[:top_k]

    def get(self, ids):
        return {i: np.ones(384, dtype=np.float32) for i in ids}


def _rpc_error(code: grpc.StatusCode) -> grpc.RpcError:
    error = grpc.RpcError()
    error.code = lambda: code
    return error


@pytest.fixture
def api(monkeypatch, tmp_path):
    """A state dict without the startup event, which would dial a real MinDB."""
    monkeypatch.setattr(main, "config", dataclasses.replace(main.config, corpus_dir=tmp_path))
    main.state.clear()
    main.state.update({"embedder": _Embedder(), "names": NameIndex([])})
    return main.state


class _Embedder:
    def embed_query(self, text: str) -> np.ndarray:
        return np.ones(384, dtype=np.float32) / np.sqrt(384)


def test_liveness_ignores_mindb_entirely(api):
    """The bug this prevents: a liveness probe that calls MinDB kills the API when MinDB restarts --
    restarting the component that was still healthy."""
    api["mindb"] = Away()
    assert main.livez() == {"status": "alive"}


def test_readiness_fails_while_mindb_is_away(api):
    api["mindb"] = Away()
    with pytest.raises(HTTPException) as raised:
        main.readyz()

    assert raised.value.status_code == 503
    assert raised.value.headers["Retry-After"] == str(main.RETRY_AFTER_SECONDS)


def test_readiness_passes_once_mindb_answers(api):
    api["mindb"] = Present()
    assert main.readyz() == {"status": "ready", "corpus_size": 200}


def test_a_query_during_a_restart_is_503_not_500(api):
    """503 is retryable and 500 is a bug report. Which one a client sees decides whether it comes
    back in five seconds or pages someone."""
    api["mindb"] = Away()
    with pytest.raises(HTTPException) as raised:
        main.recommend(q="something relaxing", seed=None, k=5, narrate_results=False)

    assert raised.value.status_code == 503
    assert "Retry-After" in raised.value.headers


def test_health_reports_the_kernel_mindb_selected(api):
    api["mindb"] = Present()
    body = main.health()
    assert body["mindb"]["kernel"] == "avx2" and body["corpus_size"] == 200


def test_the_name_index_reloads_when_ingest_rewrites_it(api, tmp_path):
    """Long-lived API pods beside a nightly ingest: an index read once at startup goes stale by
    exactly the games users are most likely to ask for."""
    api["mindb"] = Present()
    path = tmp_path / "names.json"
    NameIndex.dump([NameEntry(1, "One", IN_CORPUS)], path)
    assert len(main._names()) == 1

    NameIndex.dump([NameEntry(1, "One", IN_CORPUS), NameEntry(2, "Two", IN_CORPUS)], path)
    api["names_mtime"] = None  # stat() resolution is coarser than this test is fast.

    assert len(main._names()) == 2


def test_a_missing_name_index_is_not_an_error(api):
    """Before the first ingest there is no file. Mood queries still work; only seeds cannot resolve."""
    api["mindb"] = Present([Hit("appid:1", 0.9, json.dumps({"appid": 1, "name": "One"}).encode())])

    body = main.recommend(q="something relaxing", seed=None, k=1, narrate_results=False)

    assert len(main._names()) == 0
    assert body["results"][0]["name"] == "One"
