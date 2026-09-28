# Failure modes

What breaks, what it looks like from outside, what the system does on its own, and what a human has
to do. Everything marked **exercised** has been made to happen on purpose and watched; the rest is
reasoned from the code and says so, because a runbook that does not distinguish the two gets
trusted in the wrong place.

The shape of most of these follows from one decision: MinDB is a derived index and the Game
Document Store is the source of truth (ADR-0003, D13). Losing vectors costs CPU. Losing documents
costs days of Steam requests.

## MinDB is down or restarting

*Exercised on k3d with `mindb` scaled to zero, and locally with `docker stop`. Both API pods stayed
up with zero restarts, and scaling MinDB back restored every vector from its PVC.*

```
symptom     /recommend -> 503, body "MinDB is unavailable (unavailable)", Retry-After: 5
            /readyz    -> 503, so the endpoint leaves the Service
            /livez     -> 200 throughout
behaviour   the API keeps running and is not restarted; kubelet stops sending it traffic
recovery    none needed; the next successful /readyz puts the pod back
```

The three probes are deliberately different (D7). Liveness never touches MinDB, because a liveness
probe that calls a dependency turns the dependency being down into this pod being killed --
restarting the one component that was still healthy. Startup is also non-fatal: the API logs
`MinDB at ... is not answering yet` and serves 503s rather than exiting, because a CrashLoopBackOff
whose backoff outlasts the MinDB restart leaves the API down after MinDB has come back.

MinDB itself deploys `Recreate` on a ReadWriteOnce volume, so a rollout *is* visible downtime
measured in seconds. That is the trade the 503 and the `Retry-After` exist to paper over.

## MinDB's snapshot is gone or corrupt

*Exercised on k3d: restored a generation over a volume and booted MinDB from it -- `loaded=200`, WAL
checked against the restored `.meta`. Attempted in the cluster on 2026-09-28 and it failed: the Job
runs as 65532 and the checkpoint on the PVC belongs to the ingest's 10001, so the write was refused
(D58). Nothing was lost -- the checkpoint is written before the snapshot exactly so that dying there
is the recoverable half -- but MinDB was down for seven minutes rather than the ninety seconds the
procedure implies, and the API needed `kubectl rollout restart deploy/gamerec-api` to come back.
Treat the write half as unproven in the cluster until it has run there once.*

```
symptom     MinDB boots with vector_count 0; /health shows corpus_size 0
            /recommend?q= returns nothing, and every seed is a 404
behaviour   nothing self-heals: MinDB has no idea it should hold vectors
recovery    restore the newest generation, or reindex from documents.jsonl
```

Two recoveries, and which one to reach for depends on what else survived:

| documents.jsonl | R2 | do this |
|---|---|---|
| present | either | `python -m ingest.reindex` -- no Steam requests, ~176k embeddings |
| lost | present | restore a generation; `ops.backup --documents` put the corpus there too |
| lost | lost | re-fetch from Steam: days, paced at 35 requests a minute (D17) |

Restoring is minutes and reindexing is hours, so restore first when there is a good generation. The
header of `deploy/k8s/manual/restore.yaml` has the exact sequence; MinDB must be scaled to 0 first,
because it writes its own snapshot over the top every `-snapshot-interval`.

## A backup generation is incomplete or corrupt

*Exercised against a real S3 endpoint (moto): a generation with the COMPLETE marker removed, and a
generation with one byte flipped in `mindb.snap`.*

```
symptom     ops.restore --list does not show the generation at all (no COMPLETE), or
            the restore exits 1 with "does not match the manifest"
behaviour   nothing is written to the volume; the existing snapshot is untouched
recovery    restore the generation before it; there are 7 (ADR-0002)
```

The marker is written last and is the only eligibility signal, so a backup killed halfway leaves a
prefix no restore can see. The manifest's per-file sha256 is checked before anything is written,
which is what makes a failed verification a no-op rather than half a restore.

## R2 is unreachable or has no credentials

*Exercised on the live cluster with a bucket name that did not exist, which is how the first version
of this entry turned out to be wrong.*

```
symptom     the backup container logs the botocore error and keeps running
            MinDB stays in its Service; queries keep working
            ingest logs "running without the ingest lease" and runs anyway
behaviour   backups stop and say so every interval. Nothing restarts MinDB
recovery    fix the Secret or the bucket; the next tick retries the same snapshot
```

This entry used to claim the container exited 1 into CrashLoopBackOff and that "MinDB and the API are
unaffected". The first half was true and the second did not follow from it: a pod is Ready only when
every container in it is ready, so the crash-looping sidecar dropped the pod out of the `mindb`
Service, the API got `Connection refused` to the ClusterIP, `/readyz` went 503, and the service was
down -- caused entirely by a backup failure. It was marked *exercised* because MinDB had been watched
not restarting. Nobody had checked whether it was still reachable.

The sidecar now catches every failure and keeps running (D49), and neither watch marker is advanced on
failure, so a transient R2 error costs one interval rather than one generation. The probe decision in
D39 is unchanged and was never the problem.

**Nothing inside the cluster notices backups have stopped** -- that is still true, and now it is the
whole story rather than a footnote. The alert worth having is "the newest generation is older than a
day", and it has to live outside the cluster. That threshold is not arbitrary and it survives D52's
change to when generations are written: a generation is written when the vectors change, the nightly
ingest changes them, so a day with no generation is a day with no ingest.

Ingest running without a lease is deliberate too: backups are worth more than mutual exclusion, and
the cost of two concurrent ingests is repeated work, because every insert is keyed by appid.

## Steam is down, rate-limiting, or returns junk

*Exercised: the throttle against a stub returning 429s, and appids whose response has no `success`.*

```
symptom     ingest logs "fetch failed ...; will retry next run" per appid
            the name index gains PENDING_INGEST entries
            /recommend?seed= on one of those -> 404 with a reason, not a wrong game
behaviour   the run finishes with fewer documents; the checkpoint still completes
recovery    none; the next nightly run retries exactly those appids
```

A fetch that never got an answer is recorded `PENDING_INGEST`, not `NOT_A_GAME` (ADR-0005). The
distinction matters because `NOT_A_GAME` is permanent and would bury the game forever, while
`PENDING_INGEST` is retried. A 429 costs a 20-30 s backoff, which is why the configured pace sits
under Steam's measured ceiling rather than at it.

## The embedding model or the render template changes

*Exercised: a bumped `TEMPLATE_VERSION` against an existing checkpoint.*

```
symptom     ingest exits 1 before fetching anything, logging both stamps
behaviour   no vectors are written, so the index never mixes two embedding spaces
recovery    python -m ingest.reindex, which re-embeds every document at the new stamp
```

A refusal rather than a migration, because a half-migrated index returns confidently wrong
neighbours and there is no way to tell from a vector which model produced it. Every payload carries
its stamp, which is what makes the mismatch detectable at all (D11).

## The ingest lease is lost mid-run

*Exercised: expired a lease under a running holder against moto -- `renew()` returned False.*

```
symptom     ingest logs "lost the ingest lease mid-run; stopping after this batch", exits 1
behaviour   it stops at a batch boundary, after a snapshot, with the checkpoint pending
recovery    none; the next run resumes from the checkpoint
```

Stopping at a boundary is why this is cheap: a batch ends with durable state, so a lease lost there
costs nothing that is not already on disk. A second holder failing to acquire the lease exits
**0**, not 1 -- the lease working as intended should not page anyone.

## The ingest is OOM-killed

*Exercised, unintentionally, twice: the nightly runs of 2026-09-27 and 2026-09-28, both at 03:22 UTC
(D51). The second happened because the fix was merged and never deployed.*

```
symptom     the CronJob reports Completed and the corpus has documents but no vectors:
            /health gives corpus_size 0 next to a non-zero name_index_size
behaviour   the retry takes over the lease its own dead attempt left behind and runs
            (D55). Before D55 it exited 0 instead, and the Job reported success
recovery    the retry, or failing that the next nightly run, which resumes and
            re-fetches only what is missing
```

This was the most misleading failure in this document, because every layer reported success. The Job
succeeded, the pod said `Completed`, exit code 0, and the surviving log said the lease was held —
which reads like the lease working. It was not: the holder was the dead attempt in the same
container, wearing the same name. D55 makes the retry take it over, so an OOM now costs a restart
rather than a night, and a Job that reports success has ingested something.

What does not change is where the evidence lives. The kill happens in the attempt before, whose
container status is garbage-collected within hours, so `kubectl logs --previous` answers `not found`
and nothing in Kubernetes remembers why. The verdict only survives in the kernel log:

```
sudo dmesg -T | grep -i oom-kill      # names the cgroup, the pid and the RSS at death
```

Re-running is safe and cheap. The OOM landed between the corpus append and the checkpoint write, so
there was no checkpoint at all -- and resumption does not need one: `checkpoint.appids |=
store.appids()` unions in whatever the corpus already holds, so the documents written before the kill
are not fetched again. That is the ordering in D13 paying off in the direction it was designed for:
the corpus can run ahead of the vectors, never behind them.

Two things make this cheap rather than serious, and both are worth keeping: a run holds the lease for
a TTL rather than until it exits, so even without the self-takeover a dead holder blocks one retry
window and not the next night; and the fix belongs in whatever grew past the limit, not in the limit.
See D51 and D55.

## The final snapshot fails

*Reasoned, not exercised. The path is `log.error("snapshot failed; checkpoint left pending")`.*

```
symptom     ingest exits 1; the checkpoint stays PENDING
behaviour   the backup sidecar refuses to build a generation from a pending checkpoint
recovery    the next run re-does the batches after the last good snapshot
```

The invariant is `checkpoint <= snapshot`: the checkpoint is marked complete only after `Snapshot`
returns. Backwards -- a checkpoint claiming more than the snapshot holds -- is the one direction
that loses games permanently instead of repeating work, so every ordering in the ingest and in the
backup is chosen to keep it (ADR-0002).

## Groq is down, slow, or out of quota

*Exercised: no API key configured, and a key with a wrong model name.*

```
symptom     recommendations come back with no narration; the results themselves are unchanged
behaviour   the failure is caught and logged; retrieval never depends on it
recovery    none needed; ?narrate=false skips it entirely
```

Narration is the only part of the system with an external dependency at request time, and the only
part that is decoration (D6). It fails open by construction -- and it is why the tables in
`bench/README.md` are measured with narration off.

## The disk fills

*Reasoned, not exercised.*

```
symptom     MinDB's Save fails; ingest leaves a pending checkpoint; the corpus stops growing
behaviour   queries keep being served from memory; nothing crashes immediately
recovery    the snapshot and its rotated WAL are the large files; old generations live in R2
```

The mindb-data claim is 2Gi against a ~310 MB snapshot at full capacity, sized for several
generations plus a rotated WAL rather than exactly one. Worth watching anyway: this is a
single-node cluster on local-path storage, so "the disk" is the whole VM's disk.

## The node reboots

*Not exercised. The VM exists and runs the service; nobody has rebooted it on purpose yet.*

```
symptom     everything is down for as long as the VM takes to come back
behaviour   k3s restarts, the PVCs are local and still there, MinDB replays its WAL
recovery    none expected; check /readyz and the newest generation's timestamp
```

There is one node, so there is no high availability and none is claimed. `deploy/vm/README.md`
lists what has and has not been run on the real VM.
