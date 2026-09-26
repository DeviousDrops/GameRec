"""Backup Generations to object storage (ADR-0002, D5/D12, D20).

A Snapshot restored against a Checkpoint that does not match it yields a corpus that disagrees with
the record of what was ingested -- silently, and in the direction that skips games forever. So a
generation is written under one prefix and becomes eligible for restore only when a COMPLETE marker
lands, and the restore path looks at nothing else.

    gen-20260926T090000Z/
        mindb.snap         the vectors
        mindb.snap.meta    MinDB's run_id, which is what lets it check a WAL against a snapshot
        checkpoint.json    what the ingest claims is in there
        manifest.json      names, sizes and sha256 of the three above
        COMPLETE           written last, and the only thing a restore trusts

Two orderings carry the invariant `checkpoint <= snapshot`:

  * the ingest writes the pending Checkpoint *before* calling Snapshot (ADR-0002), and
  * this reads the Checkpoint *before* the snapshot file.

Read the other way round, a generation could pair a snapshot with a checkpoint claiming more than it
holds, which is the one direction that loses games rather than repeating work.

The document store is synced separately, because it is ~180 MB that grows by appending while a
snapshot is ~300 MB that changes wholesale. Losing it is the unrecoverable failure -- MinDB is
derived and the documents are not (D13, ADR-0003) -- so the ingest pushes it at the end of a run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from gamerec import objectstore
from gamerec.checkpoint import COMPLETE, Checkpoint
from gamerec.config import Config
from gamerec.objectstore import ObjectStore

log = logging.getLogger("backup")

GENERATION_PREFIX = "gen-"
COMPLETE_MARKER = "COMPLETE"
MANIFEST = "manifest.json"
DOCUMENTS_KEY = "corpus/documents.jsonl"
NAMES_KEY = "corpus/names.json"
RETAIN_GENERATIONS = 7  # ADR-0002


@dataclass(frozen=True)
class Snapshot:
    """A snapshot file and its sidecar, read at one instant and known not to have moved."""

    data: bytes
    meta: bytes | None
    mtime_ns: int


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def read_snapshot(path: Path) -> Snapshot | None:
    """Read the snapshot and its .meta, or None if it moved underneath us.

    ADR-0002 has the backup react to the atomic rename of the snapshot file. MinDB renames into
    place, so a reader sees either the old file or the new one -- but it can see the old file's
    *bytes* and then the new file's sidecar, which is a mismatched pair. Re-stat afterwards and
    discard the read if anything changed; the next cycle picks it up.
    """
    try:
        before = path.stat()
        data = path.read_bytes()
        meta_path = path.with_name(path.name + ".meta")
        meta = meta_path.read_bytes() if meta_path.exists() else None
        after = path.stat()
    except FileNotFoundError:
        return None
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        log.info("snapshot changed while being read; leaving it for the next cycle")
        return None
    return Snapshot(data=data, meta=meta, mtime_ns=after.st_mtime_ns)


def generation_name(now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    return f"{GENERATION_PREFIX}{stamp}"


def write_generation(store: ObjectStore, snapshot: Snapshot, checkpoint: bytes,
                     now: float | None = None) -> str:
    """Upload one generation and return its prefix. COMPLETE lands last or not at all."""
    prefix = f"{generation_name(now)}/"
    files = {"mindb.snap": snapshot.data, "checkpoint.json": checkpoint}
    if snapshot.meta is not None:
        files["mindb.snap.meta"] = snapshot.meta

    for name, body in files.items():
        store.put(prefix + name, body)

    manifest = {
        "files": {name: {"bytes": len(body), "sha256": _digest(body)}
                  for name, body in files.items()},
        # MinDB regenerates a missing .meta and warns in its log, so its absence is survivable -- but
        # it is what lets MinDB check a WAL against a snapshot, so whether it made it is recorded.
        "meta_included": snapshot.meta is not None,
    }
    store.put(prefix + MANIFEST, json.dumps(manifest, indent=2).encode())
    store.put(prefix + COMPLETE_MARKER, b"")
    log.info("wrote %s (%d bytes of vectors)", prefix, len(snapshot.data))
    return prefix


def complete_generations(store: ObjectStore) -> list[str]:
    """Prefixes with a COMPLETE marker, oldest first. Anything else is invisible to a restore."""
    return sorted(
        key[: -len(COMPLETE_MARKER)]
        for key in store.list(GENERATION_PREFIX)
        if key.endswith("/" + COMPLETE_MARKER)
    )


def prune(store: ObjectStore, retain: int = RETAIN_GENERATIONS) -> list[str]:
    """Drop the oldest complete generations beyond `retain`, and return what was dropped.

    Incomplete prefixes are left alone rather than swept up. A prefix with no COMPLETE is either a
    backup still running or the wreckage of one that died, and deleting the second means deleting the
    first by accident on a bad day.
    """
    generations = complete_generations(store)
    dropped = generations[: max(0, len(generations) - retain)]
    for prefix in dropped:
        for key in store.list(prefix):
            store.delete(key)
        log.info("pruned %s", prefix)
    return dropped


def back_up_once(store: ObjectStore, config: Config, snapshot_path: Path) -> str | None:
    """One generation, or None when the state on disk is not worth backing up yet."""
    # Checkpoint first: see the module docstring. This ordering is the invariant.
    checkpoint = Checkpoint.load(config.checkpoint_path)
    if checkpoint is not None and checkpoint.status != COMPLETE:
        # An ingest is mid-flight. Its checkpoint is deliberately unrestorable, so a generation built
        # around it would be a generation nothing may use.
        log.info("checkpoint is %s; waiting for the run to finish", checkpoint.status)
        return None
    checkpoint_bytes = config.checkpoint_path.read_bytes() if checkpoint is not None else b"{}"

    snapshot = read_snapshot(snapshot_path)
    if snapshot is None:
        log.info("no readable snapshot at %s yet", snapshot_path)
        return None

    prefix = write_generation(store, snapshot, checkpoint_bytes)
    prune(store)
    return prefix


def sync_documents(store: ObjectStore, config: Config) -> int:
    """Push the Game Document Store and the name index. Returns the number of bytes uploaded.

    Whole-file rather than incremental. The corpus only grows by appending, so an incremental upload
    is possible and is not worth the class of bug it invites: a byte offset agreed wrongly between two
    versions of this code produces a corpus that parses and lies.
    """
    uploaded = 0
    for path, key in ((config.documents_path, DOCUMENTS_KEY), (config.names_path, NAMES_KEY)):
        if not path.exists():
            continue
        body = path.read_bytes()
        store.put(key, body)
        uploaded += len(body)
        log.info("synced %s (%d bytes)", key, len(body))
    return uploaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="/data/mindb.snap",
                        help="path to MinDB's snapshot file, as this container sees it")
    parser.add_argument("--documents", action="store_true",
                        help="sync the document store and name index instead of a generation")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                        help="keep running, backing up whenever the snapshot changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config()
    store = objectstore.from_config(config)
    if store is None:
        log.error("no object storage configured; set R2_ENDPOINT, R2_BUCKET and the credentials")
        return 1

    if args.documents:
        sync_documents(store, config)
        return 0

    snapshot_path = Path(args.snapshot)
    if not args.watch:
        return 0 if back_up_once(store, config, snapshot_path) else 1

    # Watch by mtime rather than inotify: one stat() per interval against a file that changes every
    # few minutes, and no dependency on the filesystem being one inotify understands.
    last = None
    while True:
        try:
            current = snapshot_path.stat().st_mtime_ns
        except FileNotFoundError:
            current = None
        if current is not None and current != last and back_up_once(store, config, snapshot_path):
            last = current
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
