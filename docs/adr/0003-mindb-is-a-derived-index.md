# MinDB is a derived index, not the source of truth

MinDB has no write-ahead log: writes since the last Snapshot die with the process. Rather than block
GameRec until that changes, we treat the vector store as rebuildable — the Game Document Store (JSON
Lines in object storage) is the source of truth, and recovery is the last Backup Generation plus an
idempotent re-ingest from its Checkpoint.

## Consequences

A crash loses recent vectors, and that is accepted rather than mitigated. This is only safe because
insert is an upsert keyed by appid, so re-ingest can never duplicate. The rule that follows: **nothing
may exist only inside MinDB.** Anything needed to rebuild — documents, names, the Model Stamp, the
Template Version — is written to object storage first. A WAL for MinDB proceeds as separate upstream
work and GameRec does not wait on it.
