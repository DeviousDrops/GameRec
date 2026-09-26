# MinDB is a derived index, not the source of truth

When this was decided MinDB had no write-ahead log: writes since the last Snapshot died with the
process. Rather than block GameRec until that changed, we treat the vector store as rebuildable — the
Game Document Store (JSON Lines in object storage) is the source of truth, and recovery is the last
Backup Generation plus an idempotent re-ingest from its Checkpoint.

## Consequences

A crash loses recent vectors, and that is accepted rather than mitigated. This is only safe because
insert is an upsert keyed by appid, so re-ingest can never duplicate. The rule that follows: **nothing
may exist only inside MinDB.** Anything needed to rebuild — documents, names, the Model Stamp, the
Template Version — is written to object storage first. A WAL for MinDB proceeds as separate upstream
work and GameRec does not wait on it.

## Update, 2026-09-20 — the WAL shipped, the decision stands

MinDB `v0.1.0` ships the write-ahead log, on by default (an unset `-wal` means `<snapshot>.wal`), and
`Stats` now reports `wal_enabled` and `wal_healthy`. The premise in the first paragraph is therefore no
longer true, and the decision is unchanged: a WAL narrows MinDB's own crash window, it does not make
MinDB the source of truth. Recovery is still the last Backup Generation plus an idempotent re-ingest,
and nothing may exist only inside MinDB.

What the WAL does change is the on-disk shape of a snapshot: `Save` now writes a `<snapshot>.meta`
sidecar alongside the snapshot file and rotates `<snapshot>.wal.NNNNNN` segments. ADR-0002 assumed a
Backup Generation was that one file; it is now a directory with a manifest, the sidecar included but
not required, and the WAL segments left where they are (D38). Reacting to the atomic rename still
holds — it is what the backup watches — with a re-stat after the read to catch the snapshot being
replaced mid-copy.
