# GameRec

GameRec recommends Steam games from a description of what someone feels like playing, and explains why each suggestion fits. This glossary fixes the vocabulary; it deliberately contains no implementation detail.

## Language

### Retrieval

**Game Document**:
The single block of text that stands for one game during retrieval — what it is, what genre vocabulary surrounds it, and how players describe playing it.
_Avoid_: record, entry, chunk, blurb, doc

**Mood Query**:
A free-text description of the experience someone is after, as opposed to a named example of it.
_Avoid_: prompt, search string, question, query text

**Seed Game**:
A game offered as an example of what someone wants more of. A request may carry a Mood Query, a Seed Game, or both.
_Avoid_: anchor, reference game, source game, similar-to

**Recommendation**:
One game returned in answer to a request, ranked by how close its Game Document sits to what was asked for.
_Avoid_: result, hit, match, suggestion

**Narration**:
The prose explaining why the Recommendations fit the request. It describes the ranking; it never decides it.
_Avoid_: explanation, summary, blurb, commentary, reasoning

**Name Index**:
The published mapping from game names to appids and their standing in the Corpus. It answers "which game did they mean", a lexical question that similarity between Game Documents cannot answer.
_Avoid_: lookup table, alias map, name cache

### Corpus

**Steam Catalogue**:
Everything Steam lists, the majority of which is not a game — downloadable content, soundtracks, videos, demos and tools.
_Avoid_: app list, Steam library, catalog

**Corpus**:
The set of games GameRec is willing to recommend: the portion of the Steam Catalogue that passed the Scope Filter.
_Avoid_: index, dataset, database, collection

**Scope Filter**:
The rule deciding which of the Steam Catalogue's apps earn a place in the Corpus.
_Avoid_: whitelist, criteria, inclusion rules

### Ingest and durability

**Ingest Run**:
One execution of the nightly job that brings the Corpus up to date with the Steam Catalogue.
_Avoid_: sync, crawl, refresh, job, import

**Checkpoint**:
The durable record of how far the last Ingest Run got — which apps are already represented in the Corpus, and under which Model Stamp.
_Avoid_: cursor, watermark, state file, last-synced

**Model Stamp**:
The identity of the embedding model that produced a vector: its name together with its version. Two vectors are only comparable if their Model Stamps agree.
_Avoid_: model version, model metadata, embedder id

**Reindex**:
Rebuilding every vector in the Corpus because the Model Stamp changed. Distinct from an Ingest Run, which only adds what is new.
_Avoid_: rebuild, backfill, migration, re-embed

**Snapshot**:
The vector store's own on-disk copy of everything it holds.
_Avoid_: dump, export, save file

**Backup Generation**:
A Snapshot and the Checkpoint that matches it, kept and restored as one unit. Restoring half of one is what this term exists to prevent.
_Avoid_: backup, restore point, snapshot (when the Checkpoint is included)

**Game Document Store**:
The durable, append-only record of every Game Document ever built. The source of truth from which the Corpus can be rebuilt without asking Steam again.
_Avoid_: archive, cache, document dump, raw store

**Derived Index**:
Anything rebuildable from the Game Document Store, and therefore allowed to be lost. The vector store is one.
_Avoid_: replica, cache, secondary

**Template Version**:
The identity of the recipe used to build a Game Document. Changing it invalidates vectors just as surely as changing the Model Stamp does.
_Avoid_: schema version, format version, doc version

**Initial Fill**:
The one-off, resumable population of an empty Corpus, measured in days rather than the nightly Ingest Run's minutes.
_Avoid_: bootstrap, backfill, seeding, cold start

**Seeded Document**:
A Game Document built from third-party bulk data rather than from Steam, and replaced by a real fetch when one arrives.
_Avoid_: stub, placeholder, imported doc

**Ingest Lease**:
The exclusive right to run an Ingest Run. Held by exactly one job at a time, so that request pacing means something.
_Avoid_: lock, mutex, semaphore, token
