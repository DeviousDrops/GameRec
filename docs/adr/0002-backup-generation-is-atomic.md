# A Backup Generation is atomic

A Snapshot restored against a Checkpoint that does not match it yields a Corpus disagreeing with the
record of what was ingested — silently, and in the direction that skips games forever. So the Snapshot
and its Checkpoint are written under one `gen-<timestamp>/` prefix and only become eligible for restore
once a `COMPLETE` marker lands; the restore path considers no other prefix.

## Consequences

The invariant is `checkpoint ≤ snapshot`, never the reverse: the pending Checkpoint is written *before*
`Snapshot` is called, and the backup reads the Checkpoint *before* the snapshot file, so a crash at any
point costs re-done work rather than skipped games. Ownership sits with a sidecar in the vector store's
pod, because it is the only component permitted to mount that volume, and it reacts solely to the
atomic rename of the snapshot file — never to a file still being written. Seven generations are
retained.

A generation is a directory rather than the single file this ADR first assumed, because MinDB v0.1.0
writes a `mindb.snap.meta` sidecar beside every snapshot (D38):

```
gen-20260926T090000Z/
    mindb.snap         the vectors
    mindb.snap.meta    MinDB's run_id, which is what lets it check a WAL against a snapshot
    checkpoint.json    what the ingest claims is in there
    manifest.json      names, sizes and sha256 of the three above
    COMPLETE           written last, and the only thing a restore trusts
```

Atomicity is therefore the marker's job: everything else is already uploaded when `COMPLETE` lands, and
a prefix without it is invisible however new it looks. Integrity is the manifest's job: the restore
verifies every file's sha256 before writing anything, and refuses the generation otherwise — a snapshot
that is merely *missing vectors* reads as valid to everything downstream.

`mindb.snap.meta` is included and is not mandatory: MinDB boots from a snapshot without it, warns that
the log could not be checked against it, and writes a fresh one at the next `Save`. The WAL segments are
not backed up at all; they narrow MinDB's own crash window and hold nothing the corpus cannot rebuild
(ADR-0003).

Because the snapshot is rewritten in place every interval, reading it is the one race here: a reader can
take the old file's bytes and then the new file's sidecar, which is a mismatched pair that no marker
would catch. The read re-stats the file afterwards and discards anything that moved.
