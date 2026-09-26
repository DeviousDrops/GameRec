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
import sys
from pathlib import Path

from gamerec import objectstore
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
    return files


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
        path.write_bytes(files[name])
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
