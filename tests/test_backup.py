"""Backup Generations and the restore that trusts them (ADR-0002, D5/D12).

The failure these guard against does not announce itself. A snapshot restored next to a checkpoint
that claims more than it holds gives a service that starts, answers queries, and never fetches the
games in the gap. So the tests here are about *eligibility and verification* rather than about bytes
making the round trip: what is invisible to a restore, and what a restore refuses.

Against MemoryStore, as with the lease tests -- these prove this code's rules, not R2's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gamerec.checkpoint import COMPLETE, PENDING, Checkpoint
from gamerec.config import Config
from gamerec.objectstore import MemoryStore
from ops import backup, restore
from ops.backup import COMPLETE_MARKER, MANIFEST, Snapshot


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


def snap(data: bytes = b"vectors", meta: bytes | None = b"mindb-meta v1") -> Snapshot:
    return Snapshot(data=data, meta=meta, mtime_ns=1)


def checkpoint_bytes(appids=(1, 2, 3), status: str = COMPLETE) -> bytes:
    return json.dumps({"model_stamp": "m", "template_version": 1, "status": status,
                       "appids": list(appids)}).encode()


def prepare(tmp_path: Path, status: str = COMPLETE, data: bytes = b"vectors") -> tuple[Config, Path]:
    """A corpus directory with a checkpoint, and a snapshot beside it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = Config(corpus_dir=tmp_path)
    Checkpoint("m", 1, appids={1, 2, 3}).save(config.checkpoint_path, status)
    snapshot_path = tmp_path / "mindb.snap"
    snapshot_path.write_bytes(data)
    snapshot_path.with_name("mindb.snap.meta").write_bytes(b"mindb-meta v1\nrun_id=abc\n")
    return config, snapshot_path


def test_the_complete_marker_is_written_last(store):
    """Not an ordering preference -- it is the whole mechanism. Everything else is already uploaded
    when the marker lands, so a restore that sees the marker sees a finished generation."""
    order: list[str] = []
    original = store.put
    store.put = lambda key, body: (order.append(key.split("/")[-1]), original(key, body))[1]

    backup.write_generation(store, snap(), checkpoint_bytes())

    assert order[-1] == COMPLETE_MARKER
    assert order[-2] == MANIFEST, "the manifest must also predate the marker"


def test_a_generation_without_the_marker_is_invisible(store):
    """What a killed backup leaves behind. The files are there and a restore must not see them."""
    backup.write_generation(store, snap(), checkpoint_bytes())
    prefix = backup.complete_generations(store)[0]
    store.delete(prefix + COMPLETE_MARKER)

    assert backup.complete_generations(store) == []
    with pytest.raises(restore.RestoreFailed, match="not restorable"):
        restore.fetch_generation(store, prefix)


def test_the_manifest_covers_every_uploaded_file(store):
    prefix = backup.write_generation(store, snap(), checkpoint_bytes())
    manifest = json.loads(store.get(prefix + MANIFEST))

    assert set(manifest["files"]) == {"mindb.snap", "mindb.snap.meta", "checkpoint.json"}
    assert manifest["meta_included"] is True
    assert manifest["files"]["mindb.snap"]["bytes"] == len(b"vectors")


def test_a_corrupted_snapshot_is_refused_not_restored(store):
    """Bit rot, a truncated upload, a half-overwritten object -- all read as a valid snapshot that is
    simply missing vectors, which nothing downstream can detect."""
    prefix = backup.write_generation(store, snap(b"vectors"), checkpoint_bytes())
    store.put(prefix + "mindb.snap", b"vectorZ")

    with pytest.raises(restore.RestoreFailed, match="does not match the manifest"):
        restore.fetch_generation(store, prefix)


def test_a_file_listed_and_missing_is_refused(store):
    prefix = backup.write_generation(store, snap(), checkpoint_bytes())
    store.delete(prefix + "checkpoint.json")

    with pytest.raises(restore.RestoreFailed, match="listed in the manifest and missing"):
        restore.fetch_generation(store, prefix)


def test_a_snapshot_with_no_sidecar_still_makes_a_generation(store):
    """MinDB boots from a snapshot with no .meta, warns, and writes one at the next snapshot. So the
    sidecar is recorded when present and never required."""
    prefix = backup.write_generation(store, snap(meta=None), checkpoint_bytes())

    assert json.loads(store.get(prefix + MANIFEST))["meta_included"] is False
    assert "mindb.snap.meta" not in restore.fetch_generation(store, prefix)


def test_pruning_keeps_the_newest_and_leaves_incomplete_prefixes_alone(store):
    """Retention is counted in complete generations. An incomplete prefix is either a backup running
    right now or the wreckage of one that died, and sweeping up the second deletes the first on a bad
    day."""
    for hour in range(9):
        backup.write_generation(store, snap(), checkpoint_bytes(), now=hour * 3600)
    store.put("gen-19991231T235959Z/mindb.snap", b"an interrupted backup")

    dropped = backup.prune(store, retain=7)

    assert len(dropped) == 2 and dropped == backup_prefixes(0, 1)
    assert len(backup.complete_generations(store)) == 7
    assert store.get("gen-19991231T235959Z/mindb.snap") is not None
    assert store.list(dropped[0]) == [], "a pruned generation leaves nothing behind"


def backup_prefixes(*hours: int) -> list[str]:
    return [backup.generation_name(hour * 3600) + "/" for hour in hours]


def test_pruning_does_nothing_below_the_retention_count(store):
    for hour in range(3):
        backup.write_generation(store, snap(), checkpoint_bytes(), now=hour * 3600)

    assert backup.prune(store, retain=7) == []


def test_a_pending_checkpoint_defers_the_backup(store, tmp_path):
    """Mid-ingest. The checkpoint is deliberately unrestorable, so a generation built around it would
    be a generation nothing is allowed to use -- and it would displace one that is."""
    config, snapshot_path = prepare(tmp_path, status=PENDING)

    assert backup.back_up_once(store, config, snapshot_path) is None
    assert store.objects == {}


def test_a_complete_checkpoint_is_backed_up_with_the_snapshot(store, tmp_path):
    config, snapshot_path = prepare(tmp_path, data=b"384 dims worth")

    prefix = backup.back_up_once(store, config, snapshot_path)

    assert store.get(prefix + "mindb.snap") == b"384 dims worth"
    assert json.loads(store.get(prefix + "checkpoint.json"))["appids"] == [1, 2, 3]


def test_a_snapshot_that_moves_mid_read_is_left_for_the_next_cycle(tmp_path):
    """MinDB renames a new snapshot into place, so a reader never sees a torn file -- but it can read
    the old file's bytes and then the new file's sidecar, which is a mismatched pair."""
    real = tmp_path / "mindb.snap"
    real.write_bytes(b"the old snapshot")

    class Renamed:
        """Stands in for the path MinDB replaces between the read and the re-stat."""

        name = "mindb.snap"

        def stat(self):
            return real.stat()

        def read_bytes(self):
            data = real.read_bytes()
            real.write_bytes(b"a newer snapshot with more vectors in it")
            return data

        def with_name(self, name):
            return real.with_name(name)

    assert backup.read_snapshot(Renamed()) is None


def test_a_settled_snapshot_reads_with_its_sidecar(tmp_path):
    config, snapshot_path = prepare(tmp_path)

    snapshot = backup.read_snapshot(snapshot_path)

    assert snapshot.data == b"vectors" and snapshot.meta.startswith(b"mindb-meta v1")


def test_restore_refuses_to_overwrite_without_force(store, tmp_path):
    prefix = backup.write_generation(store, snap(), checkpoint_bytes())
    files = restore.fetch_generation(store, prefix)
    target = tmp_path / "mindb.snap"
    target.write_bytes(b"a snapshot someone is using")

    with pytest.raises(restore.RestoreFailed, match="refusing to overwrite"):
        restore.write_files(files, target, tmp_path / "checkpoint.json")

    assert target.read_bytes() == b"a snapshot someone is using"
    restore.write_files(files, target, tmp_path / "checkpoint.json", force=True)
    assert target.read_bytes() == b"vectors"


def test_a_restored_generation_round_trips(store, tmp_path):
    config, snapshot_path = prepare(tmp_path / "live", data=b"the vectors")
    prefix = backup.back_up_once(store, config, snapshot_path)

    restored = tmp_path / "restored"
    restore.write_files(restore.fetch_generation(store, prefix), restored / "mindb.snap",
                        restored / "checkpoint.json")

    assert (restored / "mindb.snap").read_bytes() == b"the vectors"
    assert (restored / "mindb.snap.meta").exists()
    assert Checkpoint.load(restored / "checkpoint.json").status == COMPLETE


def test_the_document_store_is_synced_whole(store, tmp_path):
    """The one loss that is not recoverable: MinDB is derived from these files and they are derived
    from nothing (D13, ADR-0003)."""
    config = Config(corpus_dir=tmp_path)
    config.documents_path.write_bytes(b'{"appid":1}\n{"appid":2}\n')
    config.names_path.write_bytes(b"[]")

    uploaded = backup.sync_documents(store, config)

    assert store.get(backup.DOCUMENTS_KEY).endswith(b'{"appid":2}\n')
    assert store.get(backup.NAMES_KEY) == b"[]"
    assert uploaded == 26


def test_syncing_an_empty_corpus_directory_uploads_nothing(store, tmp_path):
    assert backup.sync_documents(store, Config(corpus_dir=tmp_path)) == 0
    assert store.objects == {}
