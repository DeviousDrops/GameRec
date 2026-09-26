"""The corpus is the source of truth (D13), so these tests are about not losing it.

Phase 1 rewrote documents.jsonl on every run. That is the bug these tests exist to keep fixed: a
second run with a smaller limit, or an interrupted one, silently shrank the thing every recovery path
depends on. The checkpoint tests pin the ordering that makes a restore safe (ADR-0002).
"""

from __future__ import annotations

from gamerec.checkpoint import COMPLETE, PENDING, Checkpoint
from gamerec.corpus import CorpusStore
from gamerec.documents import GameDocument


def doc(appid: int, name: str, year: str = "2020") -> GameDocument:
    return GameDocument(appid=appid, name=name, release_year=year)


def test_append_never_shrinks_the_corpus(tmp_path):
    store = CorpusStore(tmp_path / "documents.jsonl")
    store.append([doc(1, "One"), doc(2, "Two")])
    store.append([doc(3, "Three")])

    assert {d.appid for d in store.load()} == {1, 2, 3}


def test_a_later_fetch_shadows_an_earlier_one(tmp_path):
    store = CorpusStore(tmp_path / "documents.jsonl")
    store.append([doc(413150, "Stardew Valley", year="2016")])
    store.append([doc(413150, "Stardew Valley", year="2016 (updated)")])

    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].release_year == "2016 (updated)"
    # Both lines are still on disk -- resolution happens on read, not on write.
    assert store.line_count() == 2


def test_load_keeps_first_seen_order(tmp_path):
    """Fill order is by popularity (D14), and re-fetching a game must not promote it."""
    store = CorpusStore(tmp_path / "documents.jsonl")
    store.append([doc(1, "Most popular"), doc(2, "Second"), doc(3, "Third")])
    store.append([doc(3, "Third, refetched")])

    assert [d.appid for d in store.load()] == [1, 2, 3]


def test_compaction_drops_shadowed_lines_and_changes_nothing_else(tmp_path):
    store = CorpusStore(tmp_path / "documents.jsonl")
    store.append([doc(1, "One"), doc(2, "Two")])
    store.append([doc(1, "One, again"), doc(3, "Three")])
    before = store.load()

    removed = store.compact()

    assert removed == 1
    assert store.line_count() == 3
    assert [(d.appid, d.name) for d in store.load()] == [(d.appid, d.name) for d in before]
    # Nothing to do the second time.
    assert store.compact() == 0


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "documents.jsonl"
    store = CorpusStore(path)
    store.append([doc(1, "One")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    store.append([doc(2, "Two")])

    assert {d.appid for d in store.load()} == {1, 2}


def test_missing_corpus_reads_as_empty(tmp_path):
    store = CorpusStore(tmp_path / "nothing-here.jsonl")
    assert store.load() == []
    assert store.appids() == set()


def test_no_checkpoint_means_first_run(tmp_path):
    assert Checkpoint.load(tmp_path / "checkpoint.json") is None


def test_checkpoint_round_trips(tmp_path):
    path = tmp_path / "checkpoint.json"
    Checkpoint("model/384", 1, appids={1, 2, 3}).save(path, COMPLETE)

    loaded = Checkpoint.load(path)
    assert loaded.model_stamp == "model/384"
    assert loaded.template_version == 1
    assert loaded.appids == {1, 2, 3}
    assert loaded.restorable


def test_pending_is_not_restorable_but_is_resumable(tmp_path):
    """A crash mid-batch leaves PENDING. Resuming from it costs duplicate upserts; restoring from it
    would pair a checkpoint with a snapshot that never happened."""
    path = tmp_path / "checkpoint.json"
    checkpoint = Checkpoint("model/384", 1)
    checkpoint.mark_pending(path, {1, 2})

    loaded = Checkpoint.load(path)
    assert loaded.status == PENDING
    assert not loaded.restorable
    assert loaded.appids == {1, 2}


def test_the_watermark_only_advances_on_complete(tmp_path):
    """checkpoint <= snapshot: the watermark is the claim that work is durable, so it cannot move
    before Snapshot has returned."""
    path = tmp_path / "checkpoint.json"
    checkpoint = Checkpoint("model/384", 1)
    checkpoint.mark_pending(path, {1})
    assert Checkpoint.load(path).watermark == ""

    checkpoint.mark_complete(path)
    assert Checkpoint.load(path).watermark != ""


def test_saving_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "checkpoint.json"
    Checkpoint("model/384", 1).save(path, COMPLETE)
    assert [p.name for p in tmp_path.iterdir()] == ["checkpoint.json"]
