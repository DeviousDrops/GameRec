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
up with zero restarts, and scaling MinDB back restored every vector from its PVC. Exercised again in
the cluster on 2026-09-29, unintentionally and for much longer: MinDB crash-looped for two and a half
hours on the log a restore left behind (D60), and the API pod stayed `Running` with zero restarts
throughout, `0/1` and out of its Service, answering 503 on `/readyz` and `/recommend`. Twenty minutes
in, the restart count was still 0 -- the liveness probe deliberately not touching MinDB is what that
measures, and this is the longest it has been measured over.*

One half of this is still unproven in the cluster: whether *the same* API pod goes Ready again on its
own once MinDB returns. It did on k3d, and the code has no reconnect logic to get wrong -- the channel
is lazy and `/readyz` is a live call -- but on the real cluster the API has been rolled as part of the
fix every time MinDB has come back, so nothing here has watched an untouched pod recover. Bouncing
MinDB to find out is two minutes of 503s and worth doing; it is not worth doing while an initial fill
is in its popularity scan, because MinDB is what the fill writes to when the scan ends.

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

*Exercised in the cluster on 2026-09-29, at the third attempt. The write half works now and the
procedure in `restore.yaml` is the one that was run: MinDB scaled to 0, the newest generation restored
over the volume in six seconds, MinDB scaled back. Two attempts failed first, and both failures were in
the restore rather than in MinDB: the Job runs as 65532 and the checkpoint belongs to the ingest's
10001, so the write was refused (D58); then the write succeeded and MinDB crash-looped on the previous
run's write-ahead log, which a generation does not contain and the restore did not retire (D60). Neither
lost anything -- the checkpoint is written before the snapshot exactly so that dying there is the
recoverable half -- and both cost downtime the ninety-second procedure does not imply: seven minutes,
then eight. Budget ten minutes, not ninety seconds, and expect the API to need
`kubectl rollout restart deploy/gamerec-api` if MinDB was away long enough.*

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

One thing to look for in the restore's own log, because it is the part that was missing: a line reading
`moved mindb.snap.wal.NNNNNN aside as orphaned-...`. The log belongs to the run that wrote the snapshot
being replaced, MinDB checks the run id in the `.meta` against it and refuses to boot on a mismatch, and
the retired file is left on the volume rather than deleted (D60). Nothing cleans those up; they are the
only copy of whatever the replaced snapshot had not yet written, so read them off the volume before
removing them if the restore turned out to be a mistake.

If MinDB is already crash-looping on a log an older restore left behind, there is no way to fix it from
inside the pod: the `mindb` container cannot start, and the backup sidecar mounts `/data` read-only by
design, so `exec ... mv` answers `Read-only file system`. Either re-run the restore on an image that
retires the log, or move the file on the node -- the claim is local-path storage:

```
sudo mv /var/lib/rancher/k3s/storage/pvc-*_gamerec_mindb-data/mindb.snap.wal.??????         /var/lib/rancher/k3s/storage/pvc-*_gamerec_mindb-data/orphaned-wal.bak
sudo kubectl -n gamerec delete pod -l app.kubernetes.io/name=mindb
```

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

*Exercised: no API key configured, and a key with a wrong model name -- and then for real, in
production, on 2026-09-29. `GROQ_MODEL` named a model Groq had decommissioned, the endpoint answered
404, and narration had been absent from every response for as long as the key had been installed.
Nothing was broken and nothing reported anything: this is the failure-open path working as designed.*

```
symptom     recommendations come back with no narration; the results themselves are unchanged
behaviour   the failure is caught and logged; retrieval never depends on it
recovery    none needed; ?narrate=false skips it entirely
```

Narration is the only part of the system with an external dependency at request time, and the only
part that is decoration (D6). It fails open by construction -- and it is why the tables in
`bench/README.md` are measured with narration off.

The thing to know about that trade is that a dead narration and a working one look identical from
outside unless you ask for narration and read the field. There is no alarm to add that would be worth
it, so the check is manual and belongs in whatever gets run after a deploy:

```
curl -s "https://<host>/recommend?q=cozy+farming&narrate=true" | jq -r .narration
```

`null` means it is off, failing, or pointed at a model that no longer exists; the pod log says which
(`narration unavailable: ...`, or `narration came back empty ...` if the model reasoned through its
whole reply budget). Model ids expire without notice, so ask the account what it has rather than
trusting the value in the ConfigMap -- the command is in `deploy/k8s/10-config.yaml` (D62).

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
