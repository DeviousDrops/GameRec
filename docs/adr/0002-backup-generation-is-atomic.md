# A Backup Generation is atomic

A Snapshot restored against a Checkpoint that does not match it yields a Corpus disagreeing with the
record of what was ingested — silently, and in the direction that skips games forever. So the Snapshot
and its Checkpoint are written under one `gen-<timestamp>/` prefix and only become eligible for restore
once a `COMPLETE` marker lands; the restore path considers no other prefix.

## Consequences

The invariant is `checkpoint ≤ snapshot`, never the reverse: the pending Checkpoint is written *before*
`Snapshot` is called, so a crash between them costs re-done work rather than skipped games. Ownership
sits with a sidecar in the vector store's pod, because it is the only component permitted to mount that
volume, and it reacts solely to the atomic rename of the snapshot file — never to a file still being
written. Seven generations are retained.
