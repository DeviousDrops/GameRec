# Proposal: a write-ahead log for MinDB

**Status:** **implemented upstream in MinDB `v0.1.0`.** Kept here only as the record of what GameRec
asked for and why; the implementation, and any revision of this design, belong upstream. Nothing in
`../mindb` was touched from this repo. What shipped differs from this proposal in its configuration
surface — see the note under [Configuration](#configuration) — and the record is left unedited
otherwise, since the point of the document is what was proposed, not what was built.
**Relationship to GameRec:** none. GameRec proceeds under ADR-0003 (MinDB is a derived index) whether
or not it exists: it proceeded without one, and nothing about it changes now that one is there. This
document exists so the WAL can be judged on its own merits as storage-engine work.

---

## First, a correction to the brief

The requested write path was:

> append → fsync (group commit) → **write buffer** → ack
> Truncate the WAL once a **flushed segment** is durable.
> Recovery = **load segments** → replay WAL tail.

MinDB has no write buffer and no segments. `Insert` writes straight into preallocated arrays under a
single write lock, and durability is one whole-file snapshot (`MINDBSNP`, version 1). The design maps
cleanly once the vocabulary is corrected:

| Brief | MinDB equivalent |
|---|---|
| write buffer | the in-memory arrays themselves — writes apply directly |
| flushed segment becomes durable | `Engine.Save` completes its `tmp → fsync → rename → fsync(dir)` |
| load segments, replay WAL tail | `Load` the snapshot, then replay WAL records newer than it |

Everything else in the brief survives intact.

## Record format

```
┌──────────┬──────────┬──────────┬──────────┬────────────────────┐
│ len  u32 │ seq  u64 │ crc  u32 │ type u8  │ payload            │
└──────────┴──────────┴──────────┴──────────┴────────────────────┘
             └──────────── crc covers seq, type and payload ─────┘
```

- `len` — byte count of everything after itself, so a reader can skip without parsing.
- `seq` — monotonic commit sequence, assigned by the committer. Replay uses it to skip what the
  snapshot already contains.
- `crc` — CRC32 IEEE, matching the polynomial the snapshot format already uses.
- `type` — `1 = insert`, `2 = delete`.

Insert payload: `idLen u16 │ id │ payloadLen u32 │ payload │ dims × float32`.
Delete payload: `idLen u16 │ id`.

**The logged vector is the normalised one.** `snapshot.go` already documents why: re-running
`Normalize` on a unit vector is not the identity in float32, so replaying through `Insert` would
produce subtly different scores than the run being recovered. Replay goes through `store`, exactly as
snapshot loading does.

## Write path

The critical constraint is that **fsync must never happen under `Engine.mu`**. That lock is currently
held for microseconds; an fsync is 0.5–2 ms on a cloud disk. Holding one under the write lock would
stall every concurrent reader for milliseconds and destroy the property the engine was designed around.

The consequence is that append and apply cannot both happen inline in the caller's goroutine — two
concurrent upserts of the same id could fsync in one order and apply in the other, leaving memory and
log disagreeing. So commits go through a single committer:

```
 Insert(id, vec, payload)
        │
        ├─ validate  (dims, non-zero, non-empty id, capacity)   ← before anything is logged
        ├─ normalise                                             ← outside every lock
        │
        ▼
   commit queue ──────► committer goroutine (exactly one)
                              │
                              ├─ drain up to maxBatch / groupWindow
                              ├─ append records, assign seq
                              ├─ fsync  ◄── one syscall for the whole batch
                              ├─ e.mu.Lock(); apply batch; e.mu.Unlock()
                              └─ wake each waiter
        ▼
      ack (after apply, so a client can read its own write)
```

Three things fall out of this shape:

**Validation precedes logging.** If a record reached the log that could never be applied — a capacity
overflow, say — replay would fail forever and the engine would be unopenable. Everything that can
reject a write is checked before a byte is appended.

**One fsync amortises a whole batch.** That is the entire point of group commit, and it is what turns a
~1,000 writes/s ceiling into something usable for bulk ingest.

**The lock is taken once per batch, not per record.** It is held for `O(batch × dims)` — for a batch of
100 at 384 dims, on the order of 200 µs against a ~6 ms scan.

## Truncation

WAL files rotate rather than truncate in place: `wal-000001.log`, `wal-000002.log`, and so on.
Reclaiming space is then `os.Remove` of whole files, which is atomic and needs no in-place rewriting of
a file someone may be reading.

`Save` already holds `RLock` for the whole write, so `lastApplied` cannot advance mid-snapshot — the
snapshot has an exact sequence number by construction. That number goes into the header:

> **Snapshot format version 2** — adds `lastSeq u64` after `count`. `Load` accepts version 1 and treats
> it as `lastSeq = 0`, which is conservative: it replays more of the log than strictly necessary, and
> replay is idempotent.

After a successful `Save`, every WAL segment whose highest `seq ≤ lastSeq` is removed.

## Recovery

```
Load snapshot ──► lastSeq ──► replay wal-*.log in order
                                   │
                                   ├─ seq ≤ lastSeq          → skip
                                   ├─ crc ok                 → store()
                                   └─ crc bad / short read   → see below
```

Distinguishing a **torn tail** from **real corruption** matters, because one is routine and the other
must never be silently swallowed. Records are appended sequentially and fsynced, so an incomplete
record can only ever be the final bytes of the final segment. The rule is therefore exact rather than
heuristic:

- Bad record at the end of the last segment, with nothing after it → torn tail. Truncate there and
  continue.
- Bad record anywhere else, or followed by any further bytes → corruption. **Refuse to open**, with a
  distinct error. Do not skip and carry on.

## Configuration

| Flag | Default | Meaning |
|---|---|---|
| `-wal <dir>` | `""` | Empty preserves today's behaviour exactly |
| `-wal-sync` | `group` | `always` \| `group` \| `off` |
| `-wal-group-window` | `1ms` | How long the committer waits to batch |
| `-wal-max-batch` | `256` | Records per fsync |
| `-wal-segment-size` | `64MiB` | Rotation threshold |

**What `v0.1.0` actually exposes:** one flag, `-wal`, documented as *"write-ahead log base path; empty
means `<snapshot>.wal`, `off` disables logging"* — the inverse of the default proposed here, where empty
meant no log. The log is on unless `-wal=off` is passed. None of the batching or sync knobs are flags;
the committer's behaviour is fixed. `Stats` reports
`wal_enabled` and `wal_healthy`, which is what GameRec surfaces on `/health`. Fewer knobs is the better
call for a single-deployment engine: every one of those flags is a way to configure away the durability
the feature exists to provide.

## Test plan

The four you asked for:

1. **Crash mid-write** — a subprocess inserting continuously, `kill -9`, reopen, assert every acked
   write is present.
2. **Torn tail** — truncate the last segment at each of several byte offsets inside the final record;
   every one must recover to the last intact record.
3. **Replay is idempotent** — replay the same WAL twice; `Len()` and every vector are identical.
4. **Acked writes survive restart** — the durability claim stated directly.

Four more I would insist on:

5. **Mid-file corruption is fatal** — flip a byte in a non-final record; opening must fail loudly
   rather than silently lose data. This is the test that stops #2's leniency becoming a data-loss bug.
6. **Rotation boundary** — snapshot, rotate, delete, crash, recover across the seam.
7. **Readers are not blocked by fsync** — sustained inserts with a concurrent reader; assert p99 search
   latency stays within a small factor of the idle baseline. This is the regression test for the one
   property most at risk from this change.
8. **Same-id ordering** — concurrent upserts of one id; memory and replayed log must agree on the
   winner.

## Benchmarks

Write throughput at `-wal-sync=off | group | always`, and — more interestingly — p50/p99 `Search`
latency under sustained write load with the WAL on versus off. The first number sells the feature; the
second is the one that reveals whether the committer design actually worked.

## Trade-offs and risks

- **Write throughput becomes disk-bound.** Without group commit, ~500–2,000 writes/s on typical SSDs.
  This is a real regression against today's purely in-memory writes, and it is the price of durability.
- **Roughly doubles write I/O**: every vector is written once to the log and again to the next snapshot.
- **Recovery code is the most dangerous code in a storage engine** — it runs rarely, under duress, and
  its bugs destroy data. Hence the test list above being longer than the feature description.
- **Windows caveat inherited.** `snapshot.go` already skips the parent-directory fsync on Windows;
  segment rotation depends on the same durability, so the WAL carries the identical gap. Worth stating
  in the docs rather than discovering later.
- **Estimated size:** ~450 lines in `pkg/core/wal.go`, ~350 lines of tests, ~80 lines of changes to
  `engine.go` for the commit pipeline, ~30 to `snapshot.go` for the version-2 header, plus flags.

## Explicitly out of scope

This does not make MinDB an LSM engine. No memtable, no immutable segments, no compaction, no
replication. It adds crash durability to the existing architecture and nothing else.
