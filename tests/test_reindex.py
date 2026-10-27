"""The reindex rebuilds vectors; it must not forget what the ingest has already looked at (D66).

Stubs stand in for the model and for MinDB, because what is under test is the checkpoint a reindex
leaves behind and the lease it holds while writing it, not the arithmetic of re-embedding.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from gamerec.checkpoint import COMPLETE, Checkpoint
from gamerec.config import Config
from gamerec.corpus import CorpusStore
from gamerec.documents import GameDocument
from gamerec.lease import LEASE_KEY, acquire
from gamerec.mindb import Stats
from gamerec.objectstore import MemoryStore
from ingest import reindex


class _Embedder:
    model_stamp = "BAAI/bge-small-en-v1.5/384"

    def embed_query(self, text):
        return np.ones(384, dtype=np.float32)

    def embed_documents(self, texts):
        return [np.ones(384, dtype=np.float32) for _ in texts]


class _MinDB:
    def __init__(self, addr):
        self.count = 0

    def wait_ready(self):
        pass

    def stats(self):
        return Stats(self.count, 200_000, 384, "avx2", True, "amd64", True, True)

    def insert(self, items):
        self.count += len(items)
        return len(items)

    def snapshot(self):
        return True, "snapshot written"

    def close(self):
        pass


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    config = Config(corpus_dir=tmp_path)
    CorpusStore(config.documents_path).append([GameDocument(1, "One"), GameDocument(2, "Two")])
    # Appids 3 and 4 were looked at and have no document: filtered, or not a game.
    Checkpoint("BAAI/bge-small-en-v1.5/384", 1, appids={1, 2, 3, 4},
               refresh_after=2).save(config.checkpoint_path, COMPLETE)

    monkeypatch.setattr(reindex, "Config", lambda: config)
    monkeypatch.setattr(reindex, "Embedder", _Embedder)
    monkeypatch.setattr(reindex, "MinDBClient", _MinDB)
    monkeypatch.setattr(sys, "argv", ["reindex"])
    return config


def test_a_reindex_keeps_the_verdicts_on_games_with_no_document(corpus, monkeypatch):
    """The checkpoint is mostly appids with no document -- 52k of 82k on the live corpus. Dropping
    them would send the next ingest back to look at every one again."""
    monkeypatch.setattr(reindex.objectstore, "from_config", lambda _config: None)

    assert reindex.main() == 0

    after = Checkpoint.load(corpus.checkpoint_path)
    assert after.status == COMPLETE
    assert after.appids == {1, 2, 3, 4}
    assert after.refresh_after == 2


def test_a_reindex_holds_the_ingest_lease_and_gives_it_back(corpus, monkeypatch):
    store = MemoryStore()
    held = []
    original = reindex._reindex

    def watching(*args, **kwargs):
        held.append(store.get(LEASE_KEY) is not None)
        return original(*args, **kwargs)

    monkeypatch.setattr(reindex.objectstore, "from_config", lambda _config: store)
    monkeypatch.setattr(reindex, "_reindex", watching)

    assert reindex.main() == 0
    assert held == [True]
    assert store.get(LEASE_KEY) is None


def test_a_reindex_does_not_run_beside_an_ingest(corpus, monkeypatch):
    """Both write checkpoint.json, and the last writer would decide its stamp."""
    store = MemoryStore()
    acquire(store, owner="gamerec-ingest-29884517-ksrcq/1", ttl=1800)
    monkeypatch.setattr(reindex.objectstore, "from_config", lambda _config: store)

    assert reindex.main() == 1
    assert Checkpoint.load(corpus.checkpoint_path).appids == {1, 2, 3, 4}
