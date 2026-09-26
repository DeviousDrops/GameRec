# Decisions

A learning journal: every non-obvious choice as **context → options → choice → trade-off**.
Architecturally load-bearing entries also have an ADR in `docs/adr/`; the link is noted where one exists.

Phase 0 established a fact that shapes almost everything below: **MinDB is not LSM-structured.**
It is an in-memory, fixed-capacity, exact-kNN store with whole-file snapshot durability. See
`docs/research/mindb-api-surface.md`. `v0.1.0` added a write-ahead log (D18), which narrows the crash
window without changing any of that shape — and without changing the derived-index stance of D13.

---

## Retrieval

### D1 — A Game Document is metadata plus review-derived phrases

**Context:** The unit of embedding decides recommendation quality more than any other choice.
**Options:** (a) metadata only — name, description, genres, tags; (b) metadata plus phrases mined
from the most-helpful reviews; (c) several vectors per game, description and reviews searched separately.
**Choice:** (b). One vector per appid, built from a fixed template.
**Trade-off:** (a) is cheapest but captures what a game *is*, not what it *feels like*, which is what a
mood query asks about. (c) is richer but breaks the one-appid-one-vector property, and that property is
exactly what makes idempotent upsert free. Paying one extra Steam call per game buys the players'
own vocabulary — "relaxing", "brutally hard" — which is the vocabulary queries are written in.

### D2 — `/recommend` accepts a Mood Query, a Seed Game, or both

**Context:** Two plausible product shapes: describe a vibe, or name a game you liked.
**Options:** (a) free text only; (b) seed only; (c) both.
**Choice:** (c).
**Trade-off:** (b) and (c) require resolving an appid to its stored vector, which MinDB could not do —
this is what forced D8. Free-text-only would have needed no changes to MinDB at all.

### D3 — `bge-small-en-v1.5`, 384 dimensions, embedded in-process → ADR-0001

**Context:** Dimension multiplies directly into MinDB's eagerly allocated memory, and the model has to
run somewhere on a single VM.
**Options:** (a) `all-MiniLM-L6-v2`, 384d, fastest, weakest; (b) `bge-small-en-v1.5`, 384d, better
retrieval; (c) a 768d model — double the RAM and roughly double the scan; (d) a hosted embedding API.
**Choice:** (b), running in the API process, with MinDB started at `-dims 384`.
**Trade-off:** Halves MinDB's footprint against the 768d default and keeps query latency under our own
control rather than behind someone else's rate limit. The cost is a real footgun: BGE is *asymmetric*,
so queries need a prefix that documents must not get. Getting it wrong degrades results silently
instead of erroring.

### D10 — Blend the two query modes by weighted vector sum

**Context:** When a request carries both a Mood Query and a Seed Game, they must become one query vector.
**Options:** (a) `α·mood + (1−α)·seed`, one search; (b) two searches fused by reciprocal rank;
(c) concatenate the texts and embed once.
**Choice:** (a), with α weighting the *mood* side, configurable.
**Trade-off:** One search instead of two, and α is a dial that can later be exposed to users. MinDB
stores documents unit-normalised, so scaling the query scales all scores equally and ranking is
unaffected — the blend cannot corrupt ordering. **α is deliberately unfixed**: seed vectors are
document-shaped and will dominate query-shaped mood vectors, so the default comes from a ~10-query
eval, not from taste.

### D11 — Seed Games resolve by name outside MinDB → ADR-0004

**Context:** A Seed Game may arrive as an appid or as a name a human typed.
**Options:** (a) semantic search in MinDB for the name; (b) a separate lexical index.
**Choice:** (b). Ingest publishes `names.json` — `{appid, name, status}` where status is
`in_corpus | filtered_low_reviews | not_a_game | pending_ingest` — alongside each Backup Generation.
The API holds it in memory and matches with `rapidfuzz` over normalised names. Score ≥ 90 **and**
`in_corpus` wins; otherwise 404 carrying either the top-5 `did_you_mean` or the precise exclusion
reason. An appid bypasses matching entirely.
**Trade-off:** Two lookup systems instead of one. But **semantic similarity is not lexical similarity**:
a vector search for "Portal" happily returns puzzle games that are not Portal, and a typo has no
semantic neighbourhood at all. The status enum is what lets a 404 say *why* — "filtered: under 50
reviews" rather than a bare miss.

---

## Corpus and ingest

### D4 — Scope Filter is `type == game` plus a configurable review floor

**Context:** Most of the Steam Catalogue is not a game, and much of what is left is unplayed shovelware.
**Options:** (a) everything; (b) games only; (c) games with at least N reviews, excluding adult content.
**Choice:** (c), N configurable, starting at 50.
**Trade-off:** Cuts the long tail that would otherwise pad every top-5 with titles nobody has played.
The catch found during research: review counts come from a separate endpoint, so the filter cannot be
applied *before* paying the per-game fetch cost. It saves index space and quality, not ingest time.

### D14 — Initial Fill is a resumable Job, ordered by popularity → ADR-0005

**Context:** 176,952 games at a measured ~208 requests per 5 minutes is roughly 71 hours for details
alone, about six days including reviews. A nightly CronJob cannot do this in a night.
**Options:** (a) catch-up mode — run the nightly loop repeatedly for a week; (b) shrink the Corpus until
the first fill fits; (c) seed from a third-party bulk dataset.
**Choice:** (a) as a single long-running resumable Job that hands off to the nightly CronJob once the
Checkpoint reports caught up, with (c) as an optional accelerator. Fill order is by SteamSpy owner
estimates — **used only for ordering, never as data**. Seeded documents use the same template, are
tagged `source=seed`, and are overwritten by a later real fetch.
**Trade-off:** The service runs on a partial Corpus for its first week. That is documented rather than
hidden. The alternative — a one-shot bootstrap script — is worse precisely because nobody can safely
rerun it.

### D15 — Deltas by watermark *and* appid set

**Context:** The replacement app-list endpoint offers `if_modified_since`, which returns changed apps,
not merely new ones.
**Options:** (a) watermark only; (b) appid set only, as the original brief specified; (c) both.
**Choice:** (c). Fetch from `watermark − 1h`; the appid set backs idempotency and detects drift. The
watermark advances only after the batch is snapshotted, preserving `checkpoint ≤ snapshot`.
**Trade-off:** This **departs from the brief's "NEW appids only"** and costs extra fetches. It buys
correctness the original could not: under (b) a game that leaves early access or is overhauled keeps
its stale document forever. The one-hour overlap trades duplicate work for immunity to clock skew,
which is safe only because upsert is idempotent.
**Guard rail:** Each run does **new appids first, then updates up to `max_updates_per_run`**, deferring
the remainder to the next run. Without a cap, a large Steam change day turns an unbounded update set
into an ingest that never finishes inside its window — new games, the thing users notice, would starve
behind refreshes of games already in the Corpus.

### D16 — Groq narrates

**Context:** Narration needs a free-tier LLM, and the inputs are other people's queries.
**Options:** Groq, Gemini, OpenRouter, Mistral, Cerebras.
**Choice:** Groq. Model name and base URL are config, not code.
**Trade-off:** Research found **Gemini's free tier explicitly trains on inputs** while paid does not,
Mistral free mode does so by default, and **Cerebras has no genuinely free tier** — it needs a verified
card. Groq is the only one with a contractual non-training clause. Being OpenAI-compatible makes a
later swap a base-URL change.

### D17 — One ingest at a time, enforced by a lease

**Context:** Pacing is per-process. A manual backfill running beside the nightly CronJob would double
the request rate and trip the throttle.
**Options:** (a) trust the schedule; (b) `concurrencyPolicy: Forbid`; (c) a lease every job must hold.
**Choice:** (b) **and** (c) — an object-storage lease carrying owner and expiry; no lease, exit.
**Trade-off:** `Forbid` alone only stops the CronJob overlapping *itself*; it says nothing about a human
running a backfill by hand. The lease is the only thing that actually enforces the invariant. Measured
defaults: ~35 req/min against a ~40/min refill, 20–30s backoff with jitter.

### D6 — Narration degrades; it never gates a response

**Context:** MinDB decides the recommendations; the LLM only writes prose about them.
**Options:** (a) synchronous and required; (b) synchronous with a hard timeout and graceful fallback;
(c) a separate endpoint.
**Choice:** (b), plus a `narrate=false` parameter.
**Trade-off:** A free-tier LLM can never take down a service whose actual job is vector search, and the
p50/p95 benchmark measures retrieval rather than someone else's queue.

---

## Durability

### D13 — MinDB is a derived index, not the source of truth → ADR-0003

**Context:** MinDB has no write-ahead log, so any write since the last snapshot dies with the process.
**Options:** (a) block GameRec until MinDB grows a WAL; (b) treat MinDB as rebuildable.
**Choice:** (b). The Game Document Store — JSONL in object storage — is the source of truth. Recovery is
the last snapshot plus idempotent re-ingest from the Checkpoint. A WAL proceeds as a separate MinDB
track and **GameRec does not wait on it**.
**Trade-off:** Accepts that a crash loses recent vectors, in exchange for never blocking on another
repo's roadmap. This is only tolerable because upsert is idempotent — re-ingest is always safe.
**Guard rail — this is a rule, not a note:**

> **MinDB holds no data that is not reproducible from R2.**

It appears at the top of the README and in the deployment notes. **If anything ever becomes
MinDB-only, D13 is void and durability must be revisited** before that change ships. The rule exists
because the failure mode is gradual: one field stored only in a payload, and the derived-index stance
is quietly false while every document still claims it is true.

### D9 — Game Documents live as JSONL in object storage

**Context:** Requirement 3 demands a full Reindex when the Model Stamp changes. Re-fetching 176k games
from Steam takes days, so the documents must be stored.
**Options:** (a) append-only JSONL in object storage; (b) SQLite on a volume; (c) inside MinDB payloads.
**Choice:** (a), with **latest-wins compaction per appid** at Reindex and a `template_version` on each
document.
**Trade-off:** Reindex is batch work where latency is irrelevant, so the cheap option wins and no extra
volume or database is needed. (c) was rejected outright: MinDB holds payloads in RAM, so ~150k
documents would add roughly 150 MB to an eagerly allocated process. `template_version` matters because
the Model Stamp is not the only thing that can invalidate a vector — changing the template does too.

### D5 / D12 — A Backup Generation is a matched snapshot and Checkpoint → ADR-0002

**Context:** Restoring a snapshot against a mismatched Checkpoint produces a Corpus that disagrees with
the record of what was ingested.
**Options:** Checkpoint on MinDB's volume (excluded by the rule that ingest never mounts it), on its own
volume, or in object storage beside the snapshot.
**Choice:** Object storage, under one `gen-<timestamp>/` prefix, written by a **sidecar in the MinDB
pod** — the only component that can legitimately mount that volume. The sidecar reacts *only* to the
atomic snapshot rename, never to a partially written file. The invariant is `checkpoint ≤ snapshot`,
enforced by writing the pending Checkpoint before calling `Snapshot`. Interval snapshots pair with the
last `COMPLETE` Checkpoint. Seven generations are retained.
**Trade-off:** A sidecar rather than a backup CronJob, because CronJobs fire on a clock and would race
the ingest or wait a padded interval. The `COMPLETE` marker is what makes "never restore a mismatched
pair" a property rather than an intention.

### D7 — Snapshot every N batches; the API returns 503 during a restart

**Context:** No WAL means snapshot cadence is the bound on how much work a crash destroys. `Recreate`
means the old MinDB pod dies before the new one starts.
**Options:** Snapshot once at the end of ingest, or every N batches; hold requests open during a
restart, or fail them.
**Choice:** Every N batches (start at 10) with `-snapshot-interval` as a backstop; 503 plus
`Retry-After` when MinDB is unavailable.
**Trade-off:** 503 is the honest answer — holding connections open converts a short visible outage into
a long mysterious one.

---

## MinDB (upstream — not built here)

> MinDB changes are owned by the MinDB repo. GameRec consumes a released multi-arch image by tag and
> **does not plan, implement or modify MinDB from this repo.** A gap found here is reported upstream,
> not patched locally. The entries below record what GameRec may assume.

### D8 — `Get(ids)` and `Stats` land upstream

**Context:** Two gaps blocked GameRec: capacity is a hard wall that is unobservable over the network,
and a Seed Game cannot be resolved to its stored vector because there is no get-by-id.
**Options:** Work around both in GameRec — keep a parallel vector store, scrape capacity from the boot
log — or make the smallest additive change upstream.
**Choice:** Two additive RPCs. `Stats` returns count, dims and capacity, reusing the already-exported
`Engine.Len/Dims/Cap`. `Get(ids)` returns stored vectors, using the existing `idMap`.
**Trade-off:** Touches a second repo. The alternative for `Get` was a document store on the query path
with a ~10–30 ms re-embed per seeded request; the alternative for `Stats` was discovering the capacity
wall when the nightly ingest failed. Both additive, neither changes existing wire messages.

### D18 — A write-ahead log for MinDB, as an independent track

**Context:** The absence of a WAL is MinDB's largest correctness gap as a storage engine, independent of
whether GameRec needs it.
**Options:** Build it as a GameRec dependency, or as separate upstream work.
**Choice:** Separate, and handed off to the MinDB repo; `docs/proposals/mindb-wal.md` is kept only as
the record of what GameRec asked for. **Shipped in `v0.1.0`.** The derived-index stance of D13/ADR-0003
holds unchanged, as that decision always said it would.
**Trade-off:** Keeps two repos' schedules uncoupled. The risk is that the WAL design is settled without
a live consumer exercising it, which is why the proposal leads with its test plan.
**What shipped, versus what was proposed:** one flag, `-wal`, where empty means `<snapshot>.wal` and
`off` disables logging — so the log is **on by default**, the inverse of the proposal, in which an empty
value meant no log. The sync mode, group window, batch size and segment size are not configurable.
`Stats` reports `wal_enabled` and `wal_healthy`, which GameRec surfaces on `/health`. GameRec passes no
WAL flags and relies on the default, so a snapshot directory now holds `mindb.snap`,
`mindb.snap.meta` and rotating `mindb.snap.wal.NNNNNN` — which is what D28 has to account for.

---

## Deployment

### D19 — MinDB arrives as a tagged multi-arch image from GHCR

**Satisfied by `v0.1.0`**, published at `ghcr.io/deviousdrops/mindb:v0.1.0` for `linux/amd64` and
`linux/arm64`. See D28 for what consuming it changed.

**Context:** MinDB needs a container image and CI, and both are being built in its own repo.
**Options:** Build MinDB from source in GameRec's CI, vendor it, or consume a published image.
**Choice:** Consume a published multi-arch image by **released tag** — never `latest`.
**Trade-off:** GameRec cannot fix a MinDB bug by editing code; it must wait for an upstream tag. That
is the point: a pinned tag makes the deployed engine version an explicit, reviewable fact rather than
whatever happened to be on disk. Upgrades become a one-line manifest change with a visible diff.

### D20 — Cloudflare R2, with the Ingest Lease built on conditional PutObject

**Context:** Backup Generations, the Game Document Store, `names.json` and the Ingest Lease all need
durable storage outside the VM, and D17's lease needs an atomic create-if-absent.
**Options:** R2, S3, B2, or MinIO on the same VM.
**Choice:** R2. The lease is `PutObject` with `If-None-Match: *`.
**Trade-off:** MinIO on the VM was rejected outright — a backup sharing a failure domain with the thing
it backs up is not a backup. R2 gives S3 compatibility with no egress charges, which matters because a
Reindex streams the entire document store back. **Verified** against Cloudflare's release notes: wildcard
`If-None-Match` since 2022-07-30, `412 Precondition Failed` on conflict since 2022-05-27. Two caveats
carried into the implementation: **treat 409 as well as 412** as "lease already held" (AWS returns 409
on a racing write; R2 documents only 412), and **the lease test must run against real R2** — emulators
have a history of implementing this header backwards, so a passing test under Docker Compose would be
meaningless.

### D21 — Capacity 200,000, with memory requests and limits on every pod

**Context:** MinDB allocates `capacity × dims × 4` eagerly and treats overflow as a hard error.
**Options:** Size to the expected Corpus, or size past the whole catalogue.
**Choice:** 200,000 — above all 176,952 games, so the wall is unreachable even if the review floor is
dropped to zero. Every pod declares memory requests and limits.
**Trade-off:** ~520 MB reserved at boot whether or not it is used. On a 4 GB host that is affordable,
and it converts a possible outage into a fixed, visible cost. Requests and limits matter more than
usual here: one eagerly-allocating process and an embedding model on the same small node is exactly the
shape that ends in an OOM kill of the wrong container.

### D22 — Template v1 is metadata-only; v2 adds reviews

**Context:** Fetching reviews doubles the Initial Fill from ~3 days to ~6.
**Options:** Reviews from the start, or a versioned template that adds them later.
**Choice:** v1 metadata-only for the Initial Fill; v2 adds the top 10 English reviews by helpfulness.
Budget ~250 tokens metadata and ~250 reviews, **measured with the `bge-small` tokenizer, never by
character count** — the model truncates at 512 tokens, and a character heuristic silently drops the end
of documents.
**Trade-off:** A searchable Corpus in three days instead of six, and the template-versioning path gets
exercised for real rather than in theory. The honest cost: **the Corpus is mixed v1/v2 for a while, and
a v2 document may rank differently from a v1 one for the same query** — not because the game is a better
match, but because it has more text. This must be documented wherever recommendations are evaluated.
**Guard rail:** `/recommend` returns `template_version` per Recommendation, so the inconsistency is
visible in the response rather than inferred. The v2 backfill runs in **popularity order**, so the
most-searched games stop being mixed-version first.

### D23 — ARM host; benchmarks are labelled by architecture

**Context:** The host is an Oracle Cloud Ampere A1 (arm64) with ≥4 GB, running k3s.
**Options:** Pay for x86, or accept ARM.
**Choice:** ARM. All GameRec images build multi-arch for arm64. Ingress is k3s's bundled Traefik with a
single host rule and ACME TLS on a DuckDNS subdomain.
**Trade-off:** MinDB's AVX2/FMA cascade does not exist on ARM — it falls back to pure Go, so **the
deployed service is measurably slower than any x86 benchmark of the same code**. Phase 5 therefore
reports x86 and arm64 figures as separate, labelled results. Quoting an x86 number for an ARM
deployment would be the easiest dishonest thing in this whole project.

### D24 — MIT licence

**Context:** The repo needed a licence before it went public. It is a portfolio project meant to be read
by people evaluating the work, and it depends on MinDB, which is the same author's.
**Options:** No licence (default "all rights reserved", which makes reading it legally awkward and looks
unfinished); MIT; Apache-2.0 (adds an explicit patent grant and a NOTICE convention); AGPL (copyleft,
aimed at stopping someone running it as a service).
**Choice:** MIT.
**Trade-off:** MIT gives up the patent grant Apache-2.0 offers and any copyleft protection. Neither
matters here: there is nothing patentable, and a recruiter or engineer cloning this to look at it should
hit the shortest, most familiar licence text there is. Apache-2.0 would be the right answer if MinDB
were ever positioned as a product rather than a component of this one.

### D25 — fastembed (ONNX) rather than sentence-transformers

**Context:** The API embeds queries in-process (D3), on an Ampere A1 with ~4 GB of RAM shared with
MinDB, the ingest job and k3s itself.
**Options:** `sentence-transformers`, which pulls PyTorch; `fastembed`, which runs the same
bge-small-en-v1.5 weights through ONNX Runtime; calling a hosted embedding API.
**Choice:** fastembed. The model is baked into the image at build time rather than downloaded at
startup, so a Hugging Face outage cannot stop a pod from becoming ready.
**Trade-off:** fastembed exposes far less than sentence-transformers — no fine-tuning, no pooling
control, a smaller model catalogue. None of that is needed here, and PyTorch would roughly triple the
image and compete with MinDB for the memory the corpus lives in. A hosted API was rejected because it
puts a network call on the hot path of every single query.

### D26 — Sequel numbers are matched exactly, not fuzzily

**Context:** `seed=Half-Life 3` resolved to *Half-Life*. rapidfuzz scores that pair 95, comfortably
over the ≥90 threshold, so a game that does not exist silently became a different game — destroying
the one thing the name index is for (ADR-0004).
**Options:** Raise the threshold, which breaks real typos; use a stricter scorer, which has the same
problem in a different place; treat the sequel number as structure rather than text.
**Choice:** Extract sequel markers (digits and roman numerals) from both strings and require them to
be equal; the fuzzy score only ranks what is left. "II" and "2" are the same marker.
**Trade-off:** A title whose number is incidental rather than a sequel marker (*Left 4 Dead*, *Portal
2*, *7 Days to Die*) now needs that number typed correctly. That is a much smaller harm than
confidently recommending the wrong game, and the appid always bypasses it.

### D27 — rapidfuzz gets an explicit processor

**Context:** rapidfuzz does no normalisation by default. `"stardew valley"` scored **85.7** against
`"Stardew Valley"` and was rejected by the ≥90 rule; `"half life 2"` scored **72.7** against
`"Half-Life 2"`. Ordinary lowercase typing was failing.
**Options:** Lower the threshold to absorb the penalty; normalise the text before scoring.
**Choice:** Pass `rapidfuzz.utils.default_process`, which case-folds and strips punctuation. Both
examples become 100, and the ≥90 threshold keeps its intended meaning: how different the *words* are.
**Trade-off:** A long subtitle can still fall below 90 — `"DARK SOULS 2"` against *DARK SOULS II:
Scholar of the First Sin* is 86.1. Lowering the threshold to catch it would start matching genuinely
different games, so that case is left to the appid and documented as a known limitation.

### D28 — The dev stack consumes `ghcr.io/deviousdrops/mindb:v0.1.0`

**Context:** D19 said MinDB would arrive as a tagged multi-arch GHCR image, and until one existed
`deploy/dev/mindb.Dockerfile` built `mindb-server` from a pseudo-version with `go install`. That file
carried its own instruction to delete itself once the image shipped. `v0.1.0` shipped it, and the repo
also moved from `typicallhavok/mindb` to `DeviousDrops/mindb`.
**Options:** Keep the local build as a fallback alongside the image; keep it for a dev-only fast path
against unreleased MinDB commits; or delete it and consume the tag everywhere.
**Choice:** Delete it. `docker-compose.yml` pulls the released tag, the same one a Phase 3 manifest will
name, so the dev stack and production run identical bytes. The FlatBuffers pin in `clients/` moved to
the same tag; its schema is byte-identical to the commit the bindings were generated from, so nothing
was regenerated.
**Trade-off:** Testing against an unreleased MinDB commit now means tagging a release upstream — which
is the right friction, since the alternative is a dev stack that can pass against code no deployment
will ever run. Two consequences worth recording. The image is **distroless**: no shell, so the compose
healthcheck that shelled out to `nc` cannot work, and it was removed in favour of the API blocking on
channel readiness at startup (`MinDBClient.wait_ready`); `grpc.health.v1` on `:50052` is the real probe
and Phase 3 uses it. And `Save` now writes a `mindb.snap.meta` sidecar next to the snapshot, so a Backup
Generation is no longer one file — ADR-0002 assumes it is, and that has to be resolved before the backup
sidecar is built.

**Verified against the published image**, not assumed: `tests/test_flatbuffers_codec.py` passes against
it, and `Stats` reports `dims=384 capacity=5000 kernel_name=avx2 fast_int8=true wal_enabled=true
wal_healthy=true`.

### D29 — Resumption trusts the union of the corpus and the checkpoint

**Context:** Two files on disk record what has been ingested: `documents.jsonl`, which is what actually
exists (D9), and `checkpoint.json`, which is what a run claimed. They can disagree, because a run that
dies between appending a batch and writing the checkpoint leaves the corpus ahead of the claim.
**Options:** (a) the checkpoint is authoritative and the corpus is ignored; (b) derive the seen set from
the corpus each run and drop the checkpoint's appid set; (c) start from the union of both.
**Choice:** (c) — `checkpoint.appids |= store.appids()` before planning. The checkpoint keeps its own
set because it records appids that have *no* document: a game that failed the review floor or is not a
game at all was still looked at, and under (b) those would be re-fetched on every run forever.
**Trade-off:** The union can only over-claim relative to the corpus, never under-claim, and over-claiming
costs a refresh that D15's cap already bounds. The reverse error is the expensive one — an appid dropped
from both records is a hole nothing later fills, because nothing knows it is missing. Resumption
therefore reads any checkpoint, `PENDING` or `COMPLETE`; only *restore* insists on `COMPLETE`, since
that is the case where a checkpoint claiming more than its snapshot holds does real damage (ADR-0002).

### D30 — Pacing is a token bucket, and a throttle does not spend the retry budget

**Context:** D17 fixed the request rate as a per-process budget but not its mechanism. The first
implementation slept a fixed interval between fetches, which is a rate limit only if every request
costs the same and none of them fail.
**Options:** (a) fixed sleep; (b) a token bucket sized by requests-per-minute with a small burst;
(c) adaptive pacing that reads Steam's rate-limit headers.
**Choice:** (b). One bucket for the whole run, so `fetch_popular` and every `fetch_details` draw from
the same budget instead of each pacing itself correctly and jointly overrunning. Defaults are 35
requests per minute against the ~40/minute D17 measured, burst 5. A 429 empties the bucket and sleeps a
jittered 20–30s.
**Trade-off:** (c) needs headers Steam does not document and would silently stop working if they change
shape; a bucket is wrong in a knowable direction instead. The subtlety is in the accounting: a request
that has to wait still *charges* its token and waits off the debt, because zeroing the balance instead
hands the next caller a free token and halves the effective rate. Retries are budgeted separately from
throttles — being rate-limited is not evidence that a game cannot be fetched, and spending the failure
budget on it is how a busy afternoon turns into missing games.

### D31 — Ingest refuses to mix embedding models; the template may mix

**Context:** Vectors from two different models in one index are not comparable, and nothing about the
resulting recommendations looks wrong. The checkpoint already carries the Model Stamp and the template
version (D9), so a mismatch is detectable before the first request.
**Options:** (a) trust the operator; (b) refuse on any change to either field; (c) refuse on a model
change, warn on a template change.
**Choice:** (c). A model change exits non-zero naming `python -m ingest.reindex`, which rebuilds every
vector from the documents already on disk without touching Steam — the concrete payoff of the
derived-index stance in D13/ADR-0003. A template change only warns, because D22 plans exactly that
transition and a corpus rendered at mixed template versions is a documented intermediate state, not a
fault.
**Trade-off:** (b) is simpler and would block the v1→v2 rollout D22 already committed to. The guard is
also only as good as the stamp: it catches a changed model, not a changed model that kept its name, so
the stamp includes the dimension count and `Stats.dims` is checked against a live probe embedding on
every run as a second, cheaper net.

### D32 — `appdetails` is unwrapped by `data.steam_appid`, not by the envelope key

**Context:** A live run recorded Terraria, ELDEN RING, Counter-Strike and five other unmistakable games
as `not_a_game`. Nothing errored and nothing logged. `appdetails` does not always key its response by
the appid that was asked for — 105600 comes back under `"1323320"` — so a lookup by the requested key
missed the payload, and the absence of a payload was read as a verdict.
**Options:** (a) index by the requested appid and treat a miss as "not a game"; (b) take the only entry
in a single-entry envelope and verify `data.steam_appid`; (c) ignore the appid in the response entirely
and trust whatever came back.
**Choice:** (b). A multi-entry envelope is refused rather than guessed at, and data whose `steam_appid`
is some other game is refused outright — storing it would be wrong in a way nothing downstream could
detect, because the document would be internally consistent and simply about the wrong game.
**Trade-off:** This is a bug fix, but it is recorded here for the second half, which is a design
position: `fetch_details` previously returned `None` both for "Steam said no" and for "Steam never
answered", and the caller could only read that as the former. It now raises `FetchFailed` when the
attempts run out, and the run records those appids as `pending_ingest` and leaves them out of the
checkpoint. **A verdict is permanent; an outage is not**, and any code path that can turn the second
into the first will eventually be given the chance. The cost is that a genuinely unreachable appid is
retried every run, which is cheap and visible, unlike a permanent mislabel.

### D33 — The corpus is one ReadWriteOnce volume, shared because there is one node

**Context:** The ingest writes `documents.jsonl`, `names.json` and `checkpoint.json`; the API reads the
name index; MinDB is rebuildable from the documents (D9, ADR-0003). In Kubernetes that means a volume
mounted by a Deployment and by a CronJob at the same time.
**Options:** (a) a `ReadWriteMany` volume, which on k3s means adding an NFS or Longhorn provisioner;
(b) `ReadWriteOnce`, which works only while every pod lands on the same node; (c) put the corpus in
object storage now and skip the volume.
**Choice:** (b) for Phase 3, with (c) arriving in Phase 4 alongside R2 and backups (D20). The API mounts
it `readOnly` at both the volume and the mount, so a code change cannot quietly start writing it.
**Trade-off:** This is a single-node assumption written into the manifests, and it breaks the moment a
second node exists — the ingest Job and the API pods would be scheduled apart and one of them would
fail to mount. That is acceptable because the target is a single VM (D23) and because the real fix is
R2, not a fight with `ReadWriteMany`: the corpus is a file that is written once a night and read
rarely, which is what object storage is for.

### D34 — Liveness never touches a dependency; readiness always does

**Context:** MinDB restarts are expected and visible (D7). The API's probes decide what a restart does
to it.
**Options:** (a) one `/health` endpoint behind both probes; (b) split them, with liveness checking only
the process and readiness checking MinDB; (c) make readiness ignore MinDB too, so the API keeps
serving and returns 503 per request.
**Choice:** (b). `/livez` touches nothing, `/readyz` calls `Stats`, and startup no longer exits when
MinDB is unreachable.
**Trade-off:** (a) is the common shape and it is a trap: a liveness probe that calls a dependency turns
that dependency being down into *this* pod being killed, so a MinDB restart would restart the API too —
and a CrashLoopBackOff whose backoff outlasts the restart leaves the API down after MinDB is back. The
cost of (b) is that during a MinDB restart both API pods go unready and the Service has no endpoints,
so callers see a connection failure rather than the 503 the code is ready to send. (c) would deliver
that 503, at the price of a pod that advertises itself as able to serve when it cannot. Readiness
means readiness; the 503 is for requests already in flight and for anything holding a connection.

### D35 — MinDB is fronted by a NetworkPolicy, because it has no authentication

**Context:** MinDB's gRPC API is unauthenticated by design — it is an embedded store that happens to
speak over a socket. Anything that can reach port 50051 can read the whole index and overwrite it.
**Options:** (a) rely on the Service being `ClusterIP`, so nothing outside the cluster can reach it;
(b) add a default-deny NetworkPolicy allowing only the API and the ingest; (c) ask upstream for auth.
**Choice:** (b) as well as (a). With no credential to check, network reachability *is* the access
control, and a namespace where any pod can dial any other is the same as none. The health port stays
open to every source because the kubelet probes it from the node, which no pod selector matches.
**Trade-off:** (c) is the real fix and it is not GameRec's to make (see the MinDB note above), and it
would be a larger surface than this deployment needs. A NetworkPolicy also depends on the CNI
enforcing it — k3s does, and it was verified from an unlabelled pod rather than assumed, but a cluster
whose CNI ignores policies would be silently open.

### D36 — The embedding model is baked to `/opt/models`, not to fastembed's default

**Context:** Downloading bge-small-en-v1.5 at startup makes a cold pod wait on huggingface.co, which
turns an unrelated outage into a GameRec outage. The image therefore downloads it at build time.
**Options:** (a) leave it at fastembed's default, `/tmp/fastembed_cache`; (b) set
`FASTEMBED_CACHE_PATH` to a path outside `/tmp`; (c) ship the model on a volume instead of in the
image.
**Choice:** (b), world-readable, with nothing writing there at runtime.
**Trade-off:** (a) looks identical and fails in deployment only: a hardened pod runs with a read-only
root filesystem and an `emptyDir` on `/tmp` for onnxruntime, which hides the baked model and sends
every cold start to huggingface.co — the exact outage the baking was meant to prevent, discoverable
only by watching a pod's first request. (c) decouples the model from the image, at the price of a
volume that has to be populated before anything can start and a model version that is no longer
pinned by the image tag. The cost of (b) is ~130 MB of image and a rebuild to change models, which is
correct: a model change is a new Model Stamp and a full reindex anyway (D31).

### D37 — One image for the API, the ingest and the reindex

**Context:** Three workloads run the same code: the query service, the nightly ingest, and the reindex.
**Options:** (a) one image, different commands; (b) an image per workload, each with only what it
needs.
**Choice:** (a). The API entrypoint is uvicorn; the Jobs override `command` with `python -m ingest.run`
or `python -m ingest.reindex`.
**Trade-off:** (b) would give the API an image without the ingest code and shave a little surface. It
would also give two images two chances to disagree about the render template or the embedding model,
and that disagreement is undetectable downstream: a document embedded by one version and queried by
another returns plausible, wrong neighbours. One image makes the model stamp a property of the
deployment rather than of whichever pod happens to be running.

### D38 — A Backup Generation is a directory with a manifest, not a single file

**Context:** ADR-0002 was written when a snapshot was one file. MinDB v0.1.0's `Save` writes a
`mindb.snap.meta` sidecar beside it, so a generation now has to carry at least two files plus the
checkpoint, and the restore has to decide what is mandatory.
**Options:** (a) back up only `mindb.snap` and let MinDB regenerate the sidecar; (b) a directory per
generation with a `manifest.json` of sizes and sha256s, and a `COMPLETE` marker written last; (c) a tar
archive per generation.
**Choice:** (b). Booting MinDB from a snapshot with its sidecar deleted settled what is mandatory: it
loads (`loaded=200`), warns that the log could not be checked against the snapshot, and writes a fresh
sidecar at the next `Save`. So the sidecar is included and never required, and `meta_included` is
recorded in the manifest.
**Trade-off:** (c) would make atomicity trivial — one object, either there or not — at the cost of
being unable to fetch the checkpoint without downloading ~310 MB of vectors, which is exactly what
`--list` and any inspection wants to do. (b) needs the `COMPLETE` marker to stand in for atomicity and
a sha256 check on restore to stand in for tar's integrity, both of which are cheap and both of which
are now tested: a generation with one flipped byte is refused with nothing written.

### D39 — The backup is a sidecar in MinDB's pod, not a CronJob

**Context:** Something has to copy the snapshot to R2. The snapshot lives on a ReadWriteOnce volume.
**Options:** (a) a sidecar in the MinDB pod; (b) a CronJob mounting the same PVC; (c) MinDB uploading
its own snapshots.
**Choice:** (a), watching the snapshot's mtime on a 60s poll.
**Trade-off:** (b) reads better — a backup is a scheduled task — and works today only because there is
one node, so a second pod is guaranteed to land beside the volume. That guarantee is the single-node
assumption from D33 in a place where it would fail silently by scheduling nowhere. (c) is out of scope:
MinDB is a separate repo and an embedded vector store has no business holding cloud credentials. The
cost of (a) is a container that can crash-loop beside a healthy MinDB, which is the intended signal
rather than a defect — no probe on the sidecar can restart MinDB, and a cluster answering queries while
taking no backups is worth being loud about.

### D40 — Traefik and local-path stay; cert-manager does not arrive

**Context:** k3s bundles an ingress controller, a storage provisioner and metrics-server, installed
through its own HelmChart CRDs. The convention here is plain manifests, no Helm and no operators.
**Options:** (a) keep what k3s ships and write plain `Ingress` and `PersistentVolumeClaim` manifests
against it; (b) disable them and install an ingress controller and a provisioner directly; (c) keep
them and add cert-manager for certificates.
**Choice:** (a). TLS is certbot in standalone mode on the host, with a deploy hook that writes the
certificate into the `gamerec-tls` Secret.
**Trade-off:** the convention is about what *this repo* manages, and (a) keeps every file here a plain
manifest while the distro manages its own components. (c) would automate renewal properly, at the price
of an operator and a CRD set to keep current for one certificate on one host; certbot's timer already
renews, and the hook is fifteen lines that fail loudly. (b) trades a working default for work.

### D41 — The ingest pushes the document store; the sidecar pushes the vectors

**Context:** Two things need to reach R2 on different clocks. The document store is ~180 MB that grows
by appending, once a night. The snapshot is ~310 MB that changes wholesale every minute.
**Options:** (a) one component uploads both; (b) the ingest uploads the corpus at the end of a run and
the sidecar uploads generations as the snapshot changes; (c) incremental uploads of the corpus by byte
offset.
**Choice:** (b), with the corpus pushed whole even when the run failed partway.
**Trade-off:** (a) would put both under one lock and one log, but it also means either uploading 310 MB
of unchanged vectors nightly or leaving the snapshot backed up only as often as the ingest runs. (c) is
possible — the corpus only ever appends — and is not worth the class of bug it invites: an offset agreed
wrongly between two versions of this code yields a corpus that parses and lies. Whole-file upload of
180 MB nightly is cheap, and R2 has no egress fee to make a restore expensive.

## Measurement

### D42 — Benchmarks run inside the image, in three separable modes

**Context:** A latency figure is only useful if a slow one can be attributed. Three things are in the
path of a query — the embedding model, MinDB's scan, FastAPI and the network — and they are each slow
for different reasons. ADR-0006 already requires the architecture and MinDB kernel on every number.
**Options:** (a) one end-to-end benchmark of `/recommend`; (b) three modes measuring the API, MinDB
alone and the model alone, all runnable from the host; (c) (b), but the documented way to run them is
inside the deployed image.
**Choice:** (c), with the kernel label read from `/health` at the start of every run, and a
`first->last tenth` column beside the percentiles.
**Trade-off:** (a) cannot tell a slow model from a slow index, which is the only question a benchmark
gets asked when it looks bad. (b) is what the code does and would still publish contaminated numbers:
on this dev box the host Python embeds a query in ~40 ms and the same fastembed version inside the
image does it in 3.9 ms, so a host-side table would be measuring a Windows Python install nobody
deploys. The drift column is there because percentiles sort the samples and so hide a machine that
degrades under sustained load — this laptop drifts from 52 ms to 265 ms on the host and holds flat in
the container, which is the evidence for the rule rather than an assertion of it.

### D43 — Search is timed against a synthetic 200k store, and recall is not claimed

**Context:** Exact kNN scans every vector, so Search latency is linear in the corpus. The dev corpus
is 200 documents; production is up to 200,000. A number from the first says nothing about the second.
**Options:** (a) time Search against whatever the dev corpus holds and extrapolate; (b) fill a scratch
MinDB with 200k random vectors and time against that; (c) wait for a real full corpus, which is three
days of Steam requests.
**Choice:** (b). `bench/synthetic.py` inserts normalised random vectors into a throwaway store, and
refuses to run against one that already holds more than a thousand.
**Trade-off:** random vectors are exactly right for latency — the scan does the same arithmetic
whatever the vectors mean — and worthless for recall, so the tool does not report recall and the README
says why. (a) understates by an order of magnitude at the scale that matters. (c) is the honest
measurement and is not available yet; when it is, the same three modes produce it. The arm64 table
stays empty until there is an A1 to fill it, because an x86 figure with a caveat attached is the
mistake ADR-0006 exists to prevent.

### D44 — The runbook says whether each failure was exercised or only reasoned about

**Context:** `docs/failure-modes.md` collects what breaks and how it recovers. Some entries were made
to happen and watched; others are read off the code.
**Options:** (a) document all of them in one voice; (b) document only what was exercised; (c) document
both and mark which is which.
**Choice:** (c), with a one-line note under every heading.
**Trade-off:** (a) is the dangerous one: an untested recovery path read in an emergency gets the same
trust as a tested one, and the disk-full and node-reboot entries here are guesses about a VM that does
not exist yet. (b) would leave the failures most likely to happen unwritten, since they are precisely
the ones hardest to stage. The cost of (c) is a runbook that admits its own gaps, which is the point.

## The host, revised

### D45 — The API image is amd64 only, and that is reversible

**Context:** `release.yml` built `linux/amd64,linux/arm64` because the deploy host was going to be an
Ampere A1 (D23). The host is an Azure `Standard_B2als_v2`, which is x86_64, and there is no ARM
hardware in the picture now.
**Options:** (a) keep both platforms; (b) amd64 only; (c) amd64 now and a separate arm64 tag if ARM
hardware ever appears.
**Choice:** (b), stated in the workflow as reversible: one `platforms:` line plus
`docker/setup-qemu-action` brings arm64 back.
**Trade-off:** (a) costs a QEMU cross-build on every release that nothing pulls -- the arm64 leg is
the slow one by a wide margin, and a slow release is a release people avoid cutting. The thing (a)
buys is a guarantee that the arm64 build still works, which matters when there is an arm64 machine to
run it on and not before. MinDB stays multi-arch: it is Go, it cross-compiles rather than emulating,
and it is a separate repo with its own reasons.

### D46 — The host is an Azure B2als_v2, and the ARM premise is retired

**Context:** D23 chose an Oracle Cloud Ampere A1 (arm64, always-free, 4 OCPU / 24 GB). The machine that
actually exists is an Azure `Standard_B2als_v2`: x86_64, 2 vCPU, 4 GiB, burstable, in Central India,
with the hostname on DuckDNS.
**Options:** (a) hold out for the A1 shape, which is famously out of capacity in most regions; (b) take
the Azure box and adjust the manifests to fit 4 GiB; (c) pay for something larger.
**Choice:** (b). ADR-0006 gains a revision section rather than being rewritten, and D23 stays as the
record of what was planned.
**Trade-off:** the good news is arithmetic: MinDB gets its AVX2 int8 kernel, so the deployed service is
faster than the ARM plan and the existing x86 benchmark tables describe the right kernel. The bad news
is also arithmetic: 4 GiB against the A1's 24 GB means the resource requests no longer fit as written,
and 2 burstable vCPU means sustained CPU is a credit balance rather than a constant — the paced ingest
barely notices (35 requests a minute leaves the CPU idle between fetches), while a full reindex is
~176k embeddings back to back and will run on baseline once credits are gone. (a) was waiting for
hardware that may never free up; (c) buys headroom this workload has not yet shown it needs.

### D47 — One API replica, and CPU limits that fit the box

**Context:** The manifests were written for a 24 GB host: two API replicas, and CPU limits of `2` on
MinDB. The host has 4 GiB and 2 burstable vCPU (D46).
**Options:** (a) keep two replicas and let the kernel sort it out; (b) one replica; (c) two replicas
with the memory requests halved so they fit on paper.
**Choice:** (b), plus MinDB's CPU limit cut from `2` to `1500m`. `maxSurge: 1` stays, so a rollout is
still not downtime -- the new pod passes `/readyz` before the old one goes.
**Trade-off:** two replicas bought nothing here. They were never availability: one node means one
kernel, one kubelet and one power button, and a second pod does not survive any of those failing. What
they did buy was ~350 MB of resident ONNX model each, on a host where the sidecar can be holding a
310 MB snapshot in memory while an ingest embeds a batch. (c) is the worst of both: requests that lie
about what a pod needs produce an OOM kill under exactly the load the numbers were meant to survive.
The CPU limit matters for a different reason -- a limit of `2` on a 2-vCPU node is no limit at all, so
a Search could take both cores from the API pod that is waiting on its answer.
