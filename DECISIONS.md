# Decisions

A learning journal: every non-obvious choice as **context → options → choice → trade-off**.
Architecturally load-bearing entries also have an ADR in `docs/adr/`; the link is noted where one exists.

Phase 0 established a fact that shapes almost everything below: **MinDB is not LSM-structured.**
It is an in-memory, fixed-capacity, exact-kNN store with whole-file snapshot durability and no
write-ahead log. See `docs/research/mindb-api-surface.md`.

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
**Choice:** Separate, and now **handed off to the MinDB repo**. The design in
`docs/proposals/mindb-wal.md` is kept only as the record of what GameRec asked for. Until a tagged
release includes the WAL, the derived-index stance of D13/ADR-0003 holds unchanged.
**Trade-off:** Keeps two repos' schedules uncoupled. The risk is that the WAL design is settled without
a live consumer exercising it, which is why the proposal leads with its test plan.

---

## Deployment

### D19 — MinDB arrives as a tagged multi-arch image from GHCR

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
