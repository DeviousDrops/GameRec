"""Restore the newest complete Backup Generation (ADR-0002, D5/D12).

    python -m ops.restore --list
    python -m ops.restore --snapshot /data/mindb.snap [--generation gen-...] [--force]

What this does *not* do is decide when to run. A restore overwrites the snapshot MinDB loads at boot,
so it runs as a Job against a stopped MinDB, never beside a running one -- MinDB reads the snapshot
once at startup and would write its own over the top at the next interval.

The checks are the point:

    COMPLETE     a prefix without the marker is invisible, however new it looks
    sha256       every file is verified against manifest.json before anything is written
    --force      an existing snapshot is never overwritten by accident

A generation whose checkpoint claims more than its snapshot holds is the failure this guards against,
and it is silent -- the service comes up, answers queries, and never fetches the games in the gap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from gamerec import objectstore
from gamerec.checkpoint import Checkpoint
from gamerec.config import Config
from gamerec.objectstore import ObjectStore
from ops.backup import COMPLETE_MARKER, MANIFEST, complete_generations

log = logging.getLogger("restore")


class RestoreFailed(RuntimeError):
    """Refusing to restore is always better than restoring something unverified."""


def fetch_generation(store: ObjectStore, prefix: str) -> dict[str, bytes]:
    """Download and verify a generation. Raises rather than returning something half-checked."""
    if store.get(prefix + COMPLETE_MARKER) is None:
        raise RestoreFailed(f"{prefix} has no {COMPLETE_MARKER} marker; it is not restorable")

    raw_manifest = store.get(prefix + MANIFEST)
    if raw_manifest is None:
        raise RestoreFailed(f"{prefix} has a {COMPLETE_MARKER} marker but no {MANIFEST}")
    manifest = json.loads(raw_manifest)

    files: dict[str, bytes] = {}
    for name, expected in manifest["files"].items():
        body = store.get(prefix + name)
        if body is None:
            raise RestoreFailed(f"{prefix}{name} is listed in the manifest and missing")
        digest = hashlib.sha256(body).hexdigest()
        if digest != expected["sha256"]:
            raise RestoreFailed(
                f"{prefix}{name} does not match the manifest: sha256 {digest[:12]} vs "
                f"{expected['sha256'][:12]}"
            )
        files[name] = body

    _verify_checkpoint(prefix, files.get("checkpoint.json"))
    return files


def _verify_checkpoint(prefix: str, body: bytes | None) -> None:
    """The checkpoint is about to become the corpus directory's, so it is checked as a Checkpoint.

    Matching the manifest only proves the bytes arrived as they left. Seven generations on the live VM
    carried `{}` -- valid JSON, uploaded before anything had been ingested, and enough to make the
    next ingest fail on a KeyError instead of a sentence (D53). The digests all matched.
    """
    if body is None:
        raise RestoreFailed(f"{prefix} has no checkpoint.json; a snapshot alone is not restorable")
    try:
        checkpoint = Checkpoint.from_bytes(body)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RestoreFailed(
            f"{prefix}checkpoint.json is not a Checkpoint ({type(exc).__name__}: {exc}); restoring "
            f"it would replace a working checkpoint with one nothing can read"
        ) from exc
    if not checkpoint.restorable:
        raise RestoreFailed(
            f"{prefix} carries a {checkpoint.status} checkpoint; only COMPLETE may be paired with a "
            f"snapshot (ADR-0002)"
        )


# 0644, stated rather than inherited. The restore runs as MinDB's uid so the snapshot it writes is
# owned by the process that has to load it; the next ingest runs as its own uid and has to be able to
# read the checkpoint this leaves behind (D58).
RESTORED_MODE = 0o644


def _write(path: Path, body: bytes) -> None:
    """Write via a temporary file and rename, the way the ingest writes its checkpoint.

    Not `write_bytes`, for a reason that only shows up in the cluster: this Job runs as 65532 and
    `/corpus/checkpoint.json` belongs to the ingest's 10001 at 0644, so opening it for writing is
    EACCES. Replacing it is permitted, because the permission a rename needs is on the directory. It
    is also atomic, which for a file whose entire purpose is to be trusted is worth having anyway.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)
    os.chmod(tmp, RESTORED_MODE)
    os.replace(tmp, path)


def write_files(files: dict[str, bytes], snapshot_path: Path, checkpoint_path: Path,
                force: bool = False) -> None:
    """Put a verified generation on disk, snapshot last.

    Snapshot last for the same reason the ingest checkpoints before snapshotting: if this dies
    halfway, the result is a checkpoint with no snapshot, which reads as "nothing is loaded" and is
    recoverable. A snapshot with no checkpoint reads as "everything is ingested" and is not.
    """
    targets = {
        "checkpoint.json": checkpoint_path,
        "mindb.snap.meta": snapshot_path.with_name(snapshot_path.name + ".meta"),
        "mindb.snap": snapshot_path,
    }
    existing = [str(targets[n]) for n in files if n in targets and targets[n].exists()]
    if existing and not force:
        raise RestoreFailed(f"refusing to overwrite {', '.join(existing)}; pass --force if that is "
                            "what you meant")

    for name in ("checkpoint.json", "mindb.snap.meta", "mindb.snap"):
        if name not in files:
            continue
        path = targets[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, files[name])
        log.info("wrote %s (%d bytes)", path, len(files[name]))

    if "mindb.snap.meta" not in files:
        # Survivable, and worth saying out loud: MinDB warns and writes a fresh one at the next
        # snapshot, having been unable to check the WAL against what it loaded.
        log.warning("this generation has no .meta sidecar; MinDB will regenerate one")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="/data/mindb.snap")
    parser.add_argument("--generation", help="prefix to restore; default is the newest complete one")
    parser.add_argument("--list", action="store_true", help="list restorable generations and exit")
    parser.add_argument("--force", action="store_true", help="overwrite an existing snapshot")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config()
    store = objectstore.from_config(config)
    if store is None:
        log.error("no object storage configured; set R2_ENDPOINT, R2_BUCKET and the credentials")
        return 1

    generations = complete_generations(store)
    if args.list:
        for prefix in generations:
            print(prefix)
        if not generations:
            log.warning("no complete generations in the bucket")
        return 0

    prefix = args.generation
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    if prefix is None:
        if not generations:
            log.error("no complete generations to restore from")
            return 1
        prefix = generations[-1]

    try:
        files = fetch_generation(store, prefix)
        write_files(files, Path(args.snapshot), config.checkpoint_path, force=args.force)
    except RestoreFailed as failed:
        log.error("%s", failed)
        return 1

    log.info("restored %s; start MinDB and it will load this snapshot", prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
