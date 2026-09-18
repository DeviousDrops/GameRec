# MinDB API surface

MinDB (`github.com/typicallhavok/mindb`) is **not** an LSM engine: there is no WAL, no memtable,
no immutable segment, and no compaction anywhere in the source. It is a single flat, in-memory,
fixed-capacity, struct-of-arrays store of unit-normalized float32 vectors guarded by one
`sync.RWMutex`, searched by an **exact brute-force scan** (optionally accelerated by a provably
lossless int8 "bound-and-refine" cascade with an Avo-generated AVX2 kernel). Persistence is a
single whole-file snapshot written `tmp -> fsync -> rename -> fsync(dir)` with a CRC32 trailer.
The wire surface is four unary gRPC RPCs carrying FlatBuffers payloads through
`flatbuffers.FlatbuffersCodec` — no `.proto` file exists in the repo; codegen is `flatc --go --grpc`.

Investigated against the working tree of `D:\Projects\mindb` on 2026-09-17; `go build ./...` succeeds.
All paths below are relative to the mindb repo root. Total non-test Go: ~1,400 lines, 4 packages.

---

## Capability table

| # | Capability | Verdict | Anchor |
|---|---|---|---|
| 1 | Upsert keyed by an external id | **supported** (string keys only, silent overwrite, no duplicates on read) | `pkg/core/engine.go:168` — `(*Engine).store` |
| 2 | Batch insert in one call | **partially supported** (repeated `Vector` in one *unary* RPC; no client streaming; not atomic; 4 MiB default recv cap) | `fbs/mindb.fbs:9` — `InsertRequest.vectors`; `pkg/mindb/VectorService_grpc.go:167` — empty `Streams` |
| 3 | Top-k similarity + metadata filter | **partially supported** (top-k yes, cosine only; **no filter/predicate argument anywhere**) | `pkg/core/engine.go:242` — `(*Engine).Search(query []float32, k int)` |
| 4 | Arbitrary metadata per vector | **partially supported** (one opaque `[ubyte]` blob, returned with hits; no typed fields, not indexed, not queryable) | `fbs/mindb.fbs:6` — `Vector.payload: [ubyte]` |
| 5 | Snapshot / backup | **supported** (RPC + Go API, atomic, CRC-checked, point-in-time; no WAL, so post-snapshot writes are lost on crash) | `pkg/core/snapshot.go:51` — `(*Engine).Save(path string) error` |
| 6 | Concurrent reads during writes | **supported** (reads run concurrently with each other; reads and writes are mutually exclusive — a write waits for in-flight scans and vice versa) | `pkg/core/engine.go:52` — `mu sync.RWMutex` |

---

## gRPC service listing

Service `mindb.VectorService`, declared in `fbs/mindb.fbs:46` and generated into
`pkg/mindb/VectorService_grpc.go` by `flatc --go --grpc` (`Makefile:6`).

**Every RPC is unary.** `pkg/mindb/VectorService_grpc.go:167` — `Streams: []grpc.StreamDesc{}` is
empty, so there is no client-streaming, server-streaming or bidi RPC in the service descriptor.

| Full method | Request | Response | Kind |
|---|---|---|---|
| `/mindb.VectorService/Insert` | `InsertRequest` | `InsertResponse` | unary |
| `/mindb.VectorService/Search` | `SearchRequest` | `SearchResponse` | unary |
| `/mindb.VectorService/Delete` | `DeleteRequest` | `DeleteResponse` | unary |
| `/mindb.VectorService/Snapshot` | `SnapshotRequest` | `SnapshotResponse` | unary |

Generated Go interfaces (`pkg/mindb/VectorService_grpc.go:15` — `VectorServiceClient`;
`pkg/mindb/VectorService_grpc.go:67` — `VectorServiceServer`):

```go
// client — the request side is an opaque *flatbuffers.Builder, not a typed message
type VectorServiceClient interface {
  Insert(ctx context.Context, in *flatbuffers.Builder, opts ...grpc.CallOption) (*InsertResponse, error)
  Search(ctx context.Context, in *flatbuffers.Builder, opts ...grpc.CallOption) (*SearchResponse, error)
  Delete(ctx context.Context, in *flatbuffers.Builder, opts ...grpc.CallOption) (*DeleteResponse, error)
  Snapshot(ctx context.Context, in *flatbuffers.Builder, opts ...grpc.CallOption) (*SnapshotResponse, error)
}

type VectorServiceServer interface {
  Insert(context.Context, *InsertRequest) (*flatbuffers.Builder, error)
  Search(context.Context, *SearchRequest) (*flatbuffers.Builder, error)
  Delete(context.Context, *DeleteRequest) (*flatbuffers.Builder, error)
  Snapshot(context.Context, *SnapshotRequest) (*flatbuffers.Builder, error)
}
```

**How gRPC and FlatBuffers fit together:** there is no `.proto` and no bytes-typed proto field.
`flatc`'s `--grpc` backend emits the service descriptor directly from the `rpc_service` block in the
`.fbs`, and the FlatBuffers buffer *is* the gRPC message body. That only works because the server
installs the FlatBuffers codec globally — `cmd/mindb-server/main.go:58`:

```go
srv := grpc.NewServer(grpc.ForceServerCodec(flatbuffers.FlatbuffersCodec{}))
```

with the source comment "Registering it here is not optional." Clients must mirror it:
`pkg/api/grpc_server_test.go:127` — `grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDefaultCallOptions(grpc.ForceCodec(flatbuffers.FlatbuffersCodec{})))`.

No gRPC health service, no server reflection, no interceptors and no TLS credentials are registered
anywhere in `cmd/` or `pkg/` (the only credentials call in the repo is `insecure.NewCredentials()` in a test).

---

## FlatBuffers schema listing

`fbs/mindb.fbs`, namespace `mindb` (`fbs/mindb.fbs:1`). Generated accessors live in `pkg/mindb/*.go`,
headed "Generated by gRPC Go plugin / If you make any local changes, they will be lost".

| Table | Field | Type | Notes |
|---|---|---|---|
| `Vector` (`fbs/mindb.fbs:3`) | `id` | `string` | the external key; absent ⇒ `Id()` returns `nil` (`pkg/mindb/Vector.go:29`) ⇒ engine rejects with `ErrEmptyID` |
| | `values` | `[float32]` | the embedding; length must equal engine `dims` |
| | `payload` | `[ubyte]` | opaque user blob — the only metadata channel |
| `InsertRequest` (`fbs/mindb.fbs:9`) | `vectors` | `[Vector]` | the batch |
| `InsertResponse` (`fbs/mindb.fbs:13`) | `inserted_count` | `int32` | count actually stored |
| `SearchRequest` (`fbs/mindb.fbs:17`) | `query_vector` | `[float32]` | |
| | `top_k` | `int32` | absent ⇒ `TopK()` returns `0` (`pkg/mindb/SearchRequest.go:55`) ⇒ empty result set, **not** an error |
| `SearchResult` (`fbs/mindb.fbs:22`) | `id` | `string` | |
| | `score` | `float32` | cosine similarity, higher is better |
| | `payload` | `[ubyte]` | echoed back from insert; omitted when empty (`pkg/api/grpc_server.go:106`) |
| `SearchResponse` (`fbs/mindb.fbs:28`) | `results` | `[SearchResult]` | already in rank order on the wire (`pkg/api/grpc_server.go:113-118`) |
| `DeleteRequest` (`fbs/mindb.fbs:32`) | `ids` | `[string]` | |
| `DeleteResponse` (`fbs/mindb.fbs:36`) | `deleted_count` | `int32` | ids that did not exist are silently not counted |
| `SnapshotRequest` (`fbs/mindb.fbs:40`) | *(no fields)* | | **no path argument** — the server's configured path is used |
| `SnapshotResponse` (`fbs/mindb.fbs:41`) | `success` | `bool` | failure is reported in-band, not as a gRPC status |
| | `message` | `string` | |

There are no enums, unions, structs, `int64`/`uint64` fields, nested tables beyond the above, and no
map/key-value construct in the schema. `docs/README.md:245` labels the file "the wire contract (frozen)".

---

## Public Go API by package

### `package core` — `pkg/core` ("MinDB's in-memory vector store", `pkg/core/engine.go:1`)

```go
type Result struct { ID string; Score float32; Payload []byte }   // pkg/core/engine.go:33
type Engine struct{ ... }                                         // pkg/core/engine.go:48 (zero value unusable)

func New(dims, capacity int) (*Engine, error)                     // pkg/core/engine.go:79
func Load(path string, capacity int) (*Engine, error)             // pkg/core/snapshot.go:147

func (e *Engine) Insert(id string, vec []float32, payload []byte) error   // pkg/core/engine.go:143
func (e *Engine) Delete(id string) bool                                   // pkg/core/engine.go:218
func (e *Engine) Search(query []float32, k int) ([]Result, error)         // pkg/core/engine.go:242
func (e *Engine) Save(path string) error                                  // pkg/core/snapshot.go:51
func (e *Engine) Dims() int                                               // pkg/core/engine.go:124
func (e *Engine) Cap() int                                                // pkg/core/engine.go:127
func (e *Engine) Len() int                                                // pkg/core/engine.go:130
func (e *Engine) SetCascade(on bool)                                      // pkg/core/engine.go:110
func (e *Engine) Cascade() bool                                           // pkg/core/engine.go:117
```

Exported sentinel errors: `ErrDimensionMismatch`, `ErrCapacityExceeded`, `ErrZeroVector`, `ErrEmptyID`
(`pkg/core/engine.go:21-26`); `ErrBadMagic`, `ErrBadVersion`, `ErrBadChecksum`, `ErrSnapshotSize`
(`pkg/core/snapshot.go:38-43`).

That is the entire embeddable API. There is **no** `Get(id)`, `Has(id)`, `List`, `Stats()`, `Close()`,
iterator, or `Update` distinct from `Insert`.

### `package api` — `pkg/api` ("FlatBuffers gRPC surface over the core engine", `pkg/api/grpc_server.go:1`)

```go
type Server struct{ ... }                                     // pkg/api/grpc_server.go:19
func New(engine *core.Engine, snapshotPath string) *Server    // pkg/api/grpc_server.go:26
func (s *Server) Insert(_ context.Context, req *mindb.InsertRequest) (*flatbuffers.Builder, error)   // :35
func (s *Server) Search(_ context.Context, req *mindb.SearchRequest) (*flatbuffers.Builder, error)   // :75
func (s *Server) Delete(_ context.Context, req *mindb.DeleteRequest) (*flatbuffers.Builder, error)   // :127
func (s *Server) Snapshot(_ context.Context, _ *mindb.SnapshotRequest) (*flatbuffers.Builder, error) // :143
```

All four handlers discard the `context.Context` (`_ context.Context`), so **client deadlines and
cancellation do not abort an in-flight scan or snapshot.**

### `package math` — `pkg/math` (hot-path kernels)

```go
func Dot(a, b []float32) float32                                  // pkg/math/distance.go:23 (panics on length mismatch)
func Norm(v []float32) float32                                    // pkg/math/distance.go:55
func Normalize(v []float32) float32                               // pkg/math/distance.go:68 (in place; returns original norm)
func Quantize(v []float32, code []int8) (scale, residual float32) // pkg/math/quantize.go:23
func DotInt8(q []float32, code []int8) float32                    // pkg/math/quantize.go:76
func HasFastInt8() bool                                           // pkg/math/kernel.go:11
func KernelName() string                                          // pkg/math/kernel.go:15
```

`pkg/math/dotint8_avx2_amd64.s` and `pkg/math/dotint8_avx2_amd64_stub.go` are Avo-generated from
`pkg/math/avo/asm.go`; dispatch is chosen once in `init()` at `pkg/math/kernel_amd64.go:16` on
`cpu.X86.HasAVX2 && cpu.X86.HasFMA && cpu.X86.HasAVX`.

### `package mindb` — `pkg/mindb` (flatc-generated; do not hand-edit)

Table accessors/builders for every table above, plus `NewVectorServiceClient(cc *grpc.ClientConn)`
(`pkg/mindb/VectorService_grpc.go:30`) and
`RegisterVectorServiceServer(s *grpc.Server, srv VectorServiceServer)` (`pkg/mindb/VectorService_grpc.go:74`).

### `package main` — `cmd/mindb-server`

Flags only (`cmd/mindb-server/main.go:24-30`); see the connection section below.

---

## Per-question detail

### 1. Upsert keyed by an external id — **supported**

- **Key type is `string` only.** `pkg/core/engine.go:143` —
  `func (e *Engine) Insert(id string, vec []float32, payload []byte) error`, backed by
  `idMap map[string]uint32` (`pkg/core/engine.go:60`). On the wire the key is `Vector.id: string`
  (`fbs/mindb.fbs:4`). There is no integer-keyed path, so a Steam appid must be stringified (`"570"`).
- **Semantics are insert-or-replace in place**, decided by an `idMap` hit under the write lock
  (`pkg/core/engine.go:178`):

  ```go
  slot, existing := e.idMap[id]
  if !existing {
      if slot, err = e.allocSlot(); err != nil { return err }
      e.idMap[id] = slot; e.externalID[slot] = id; e.live[slot] = true; e.count++
  }
  base := int(slot) * e.dims
  copy(e.vectors[base:], buf)
  e.scales[slot], e.residuals[slot] = math.Quantize(buf, e.codes[base:base+e.dims])
  e.payloads[slot] = pay
  ```

  Re-inserting an existing id is **not** an error, **not** a duplicate, and consumes no new capacity:
  vector, int8 code, scale, residual and payload are all overwritten in the same slot. The doc comment
  at `pkg/core/engine.go:136` states it — "Insert stores vec under id, **replacing** any existing
  vector with that id."
- **The read path returns exactly one copy.** A replaced id occupies one slot, so `Search` can only
  surface the new value. Asserted by `pkg/core/engine_test.go:149` — `TestInsertReplacesExistingID`:
  after two `Insert("a", ...)` calls it checks `e.Len() != 1` ("replace grew the engine"), then that
  `Search` returns a single hit scoring ≈ 1 against the *second* vector with payload `"two"`.
- **Caveat:** replacement also replaces metadata. Passing an empty payload on re-insert **clears** the
  stored payload — `pkg/core/engine.go:169-173` leaves `pay == nil` when `len(payload) == 0`, and
  `e.payloads[slot] = pay` then overwrites. There is no "update the vector, keep the metadata" operation.
- Deletes are tombstone-free: the slot returns to a free list for immediate reuse
  (`pkg/core/engine.go:218` — `Delete`; `pkg/core/engine.go:198` — `allocSlot`).

### 2. Batch insert — **partially supported**

- **One unary RPC carries many vectors:** `fbs/mindb.fbs:10` — `vectors: [Vector]`, consumed by the
  loop at `pkg/api/grpc_server.go:40` (`for i := 0; i < n; i++ { ... s.engine.Insert(...) }`).
- **No client-streaming RPC exists.** `pkg/mindb/VectorService_grpc.go:167` —
  `Streams: []grpc.StreamDesc{}` is empty, and the generated client has no stream method. A large
  ingest must be chunked client-side into many unary calls.
- **Not atomic, and partial failure is observable.** `pkg/api/grpc_server.go:31-34`: "Not atomic: on
  the first rejected vector the RPC fails, and vectors earlier in the batch are already stored." The
  error carries the count — `pkg/api/grpc_server.go:71`:
  `status.Errorf(code, "vector %d: %v (%d earlier vectors in this batch were stored)", i, err, inserted)`.
  Status mapping (`pkg/api/grpc_server.go:61` — `insertError`): `ErrDimensionMismatch` / `ErrZeroVector` /
  `ErrEmptyID` → `InvalidArgument`; `ErrCapacityExceeded` → `ResourceExhausted`; anything else → `Internal`.
  An empty `values` vector is rejected before reaching the engine (`pkg/api/grpc_server.go:45`).
- **Message-size limits.** MinDB sets no `MaxRecvMsgSize`/`MaxSendMsgSize` —
  `cmd/mindb-server/main.go:58` passes only `grpc.ForceServerCodec(...)`. grpc-go's default therefore
  applies: `google.golang.org/grpc@v1.82.0/server.go:61` — `defaultServerMaxReceiveMessageSize = 1024 * 1024 * 4`
  (4 MiB receive; send is `math.MaxInt32`). At 768 dims each `Vector` is ≥ 3072 bytes of values plus
  id, payload and table overhead, so one `Insert` tops out near ~1,300 vectors with empty payloads and
  fewer with metadata. MinDB itself imposes no batch-count limit.
- **Separate hard ceiling:** `-capacity` is allocated eagerly at boot (`pkg/core/engine.go:79` — `New`,
  "Allocation is eager") and exceeding it returns `ErrCapacityExceeded` (`pkg/core/engine.go:204`).
  Capacity cannot grow at runtime.

### 3. Top-k vector similarity search, and metadata filtering — **partially supported (filter missing)**

- **k is a first-class parameter:** `pkg/core/engine.go:242` —
  `func (e *Engine) Search(query []float32, k int) ([]Result, error)`; on the wire
  `SearchRequest.top_k: int32` (`fbs/mindb.fbs:19`), passed through at `pkg/api/grpc_server.go:81`
  (`s.engine.Search(query, int(req.TopK()))`).
  - `k <= 0` returns `nil, nil` — an **empty result set, not an error** (`pkg/core/engine.go:246-248`).
    Since an absent `top_k` decodes to `0` (`pkg/mindb/SearchRequest.go:55`), forgetting to set it
    silently yields zero hits.
  - `k` is clamped to the live count (`pkg/core/engine.go:262`).
- **One metric: cosine similarity, computed as a plain dot product** over unit-normalized vectors.
  `pkg/core/engine.go:39-45`: "Vectors are normalized at insert, which makes cosine similarity equal
  the dot product." Scoring is `math.Dot(q, e.vectors[base:base+dims])` (`pkg/core/engine.go:356`;
  `pkg/core/cascade.go:85`), and the query is normalized on entry (`pkg/core/engine.go:250-254`).
  **No L2/Euclidean, no raw inner product, no metric parameter** exists — `pkg/math/distance.go`
  exports only `Dot`, `Norm`, `Normalize`. Results are sorted descending with an ID tiebreak
  (`pkg/core/engine.go:289-294`).
- **No predicate or filter argument of any kind.** `SearchRequest` has exactly two fields
  (`fbs/mindb.fbs:17-20`) and `Engine.Search` takes only `(query, k)`. There is no pre-filter,
  post-filter, id allowlist, payload predicate or namespace selector in the source. The word "filter"
  in the docs (`docs/README.md:3`, `FEATURES.md:282`) refers to the *quantization pruning* pass, not
  metadata filtering.
- **Retrieval is exact**; every live slot is scored. Two interchangeable paths:
  the plain float32 scan (`pkg/core/engine.go:300` — `scan`; `pkg/core/engine.go:347` — `scanRange`),
  parallel across `GOMAXPROCS` above `parallelScanThreshold = 8192` (`pkg/core/engine.go:30`), and the
  int8 bound-and-refine cascade (`pkg/core/cascade.go:47` — `searchCascade`), selected at construction
  by `useCascade: math.HasFastInt8()` (`pkg/core/engine.go:98`). The cascade prunes with the provable
  bound `q·v̂ ± ρ` (`pkg/math/quantize.go:5-20`) and falls back to a full scan when survivors exceed
  `guardFraction = 0.25` (`pkg/core/cascade.go:29,75`). Both paths return identical results
  (differential test: `pkg/core/cascade_test.go:76` — `TestCascadeIsExact`).

### 4. Arbitrary metadata stored alongside a vector — **partially supported**

- **The only metadata channel is one opaque byte blob:** `fbs/mindb.fbs:6` — `payload: [ubyte]` on
  `Vector`, reaching the engine as `payload []byte` (`pkg/core/engine.go:143`) and copied on insert
  (`pkg/core/engine.go:169-173`). Storage is `payloads [][]byte` indexed by slot (`pkg/core/engine.go:58`).
- **Any bytes are allowed; MinDB never interprets them.** No schema, no map type, no typed fields, no
  size check on insert, no content validation. (The only length guard found is on snapshot *load*:
  `pkg/core/snapshot.go:235` rejects a record length above `1<<28`.) Storing
  `{"model":"bge-large-en-v1.5","rev":"2"}` as JSON bytes works and survives a snapshot round-trip —
  the record format is `{idLen, id, payloadLen, payload, dims×float32}` (`pkg/core/snapshot.go:23`).
- **It is returned with search results:** `pkg/core/engine.go:283` — `Payload: e.payloads[c.slot]`,
  serialized at `pkg/api/grpc_server.go:99-108` into `SearchResult.payload` (the field is omitted
  entirely when empty). Verified end to end by `pkg/api/grpc_server_test.go:146` — `TestEndToEnd`.
- **Aliasing caveat for embedded (in-process) users:** `pkg/core/engine.go:278-282` — the returned
  `Payload` "aliases engine-owned memory rather than being copied … the worst case is a stale read,
  never a use-after-free. Callers that retain it past the call must copy." gRPC clients are unaffected;
  bytes are serialized before the handler returns.
- **Not queryable.** The payload is never read by the scan, never indexed, and cannot be filtered on
  (see Q3). There is also no fetch-by-id, so similarity search is the only way to read a payload back.

### 5. Snapshot / backup — **supported**

- **Go API:** `pkg/core/snapshot.go:51` — `func (e *Engine) Save(path string) error`.
  **RPC:** `/mindb.VectorService/Snapshot` (`fbs/mindb.fbs:50`), handled at `pkg/api/grpc_server.go:143`.
  `SnapshotRequest` is empty (`fbs/mindb.fbs:40`) — the client cannot choose a path; the server writes
  to the `-snapshot` path it was started with (`pkg/api/grpc_server.go:147` — `s.engine.Save(s.snapshotPath)`).
  With no `-snapshot` configured the RPC returns `success=false`,
  `message="snapshots are disabled: server started without -snapshot"` (`pkg/api/grpc_server.go:145-149`);
  save failures likewise return `success=false` with the error text. **Clients must check `Success()`,
  not just the gRPC status.**
- **Guarantees actually implemented:**
  - *Point-in-time consistency* — `pkg/core/snapshot.go:83-85` — `writeTo` holds `e.mu.RLock()` for the
    entire serialization, so no writer can interleave; only live slots are written
    (`pkg/core/snapshot.go:103-106`).
  - *Atomic replacement* — `pkg/core/snapshot.go:45-50`: "tmp -> fsync -> rename -> fsync(parent dir)",
    with temp-file cleanup on any failure (`pkg/core/snapshot.go:59-65`).
  - *Corruption detection* — CRC32-IEEE over the whole file, written last (`pkg/core/snapshot.go:136`)
    and verified on load (`pkg/core/snapshot.go:215` → `ErrBadChecksum`); magic `"MINDBSNP"` and
    `version = 1` checked first (`pkg/core/snapshot.go:163-168`).
  - *Fail-fast startup* — a corrupt snapshot refuses to boot the server rather than starting empty
    (`cmd/mindb-server/main.go:109-114`).
  - *Windows gap, stated in the source* — `pkg/core/snapshot.go:252`: `if runtime.GOOS == "windows" { return nil }`,
    with the comment "there the rename's durability is whatever the filesystem gives us."
- **What is NOT guaranteed:** there is no WAL and no journal, so **every write since the last snapshot
  is lost on a crash or `kill -9`.** Snapshots happen only on demand (RPC), on a timer
  (`cmd/mindb-server/main.go:117` — `startPeriodicSnapshots`, which requires both `-snapshot-interval`
  and `-snapshot`, enforced at `cmd/mindb-server/main.go:46`), or on clean shutdown
  (`cmd/mindb-server/main.go:83-88`, run after `GracefulStop` "so no in-flight write is missed").
  Each save rewrites the entire dataset — no incremental or delta snapshot — roughly 293 MB of vector
  bytes at 100k × 768.
- **Restore** is `core.Load(path, capacity)` (`pkg/core/snapshot.go:147`) at process start only; there
  is no runtime restore/import RPC. Slot indices are not stable across a reload, external ids are
  (`pkg/core/snapshot.go:145-146`). int8 codes are recomputed on load rather than persisted
  (`pkg/core/snapshot.go:26-30`). `Load` fails with `ErrSnapshotSize` when `count > capacity`
  (`pkg/core/snapshot.go:175-177`).

### 6. Concurrent reads during writes — **supported, with these exact semantics**

One lock governs everything: `pkg/core/engine.go:52` — `mu sync.RWMutex` in `Engine`. The package doc
(`pkg/core/engine.go:3-8`) states the strategy: "one RWMutex held across the whole scan … RLock costs
~20 ns and a scan costs ~6,000,000 ns, so the lock is 0.0003% of the operation."

| Operation | Lock | Site |
|---|---|---|
| `Search` | `RLock` **for the whole scan** | `pkg/core/engine.go:256` — `e.mu.RLock(); defer e.mu.RUnlock()` |
| `Insert` → `store` | `Lock` | `pkg/core/engine.go:175` |
| `Delete` | `Lock` | `pkg/core/engine.go:219` |
| `Save` → `writeTo` | `RLock` for the entire file write | `pkg/core/snapshot.go:84` |
| `Len` | `RLock` | `pkg/core/engine.go:131` |
| `Cascade` / `SetCascade` | `RLock` / `Lock` | `pkg/core/engine.go:120` / `pkg/core/engine.go:111` |

Consequences, precisely:

- **Reads never block reads.** Any number of `Search` calls proceed concurrently, including the
  per-query worker fan-out: `pkg/core/engine.go:300` — `scan` spawns `GOMAXPROCS` goroutines under the
  caller's read lock, and workers write disjoint ranges (`pkg/core/cascade.go:130-132`).
- **Writes block reads and reads block writes.** A write waits for all in-flight scans; a scan starting
  mid-write waits for the writer. `docs/README.md:256` puts it plainly: "Writes block behind in-flight scans."
- **A snapshot does not block reads but blocks all writes for its full duration**, because `writeTo`
  holds `RLock` while streaming the whole file to disk (`pkg/core/snapshot.go:84`).
- **No MVCC, no snapshot isolation, no versioning, no copy-on-write, no sharded locking.**
  `DECISIONS.md:54` records the removal of the RCU design ("replace RCU compaction with a free list"),
  and `pkg/core/engine.go:5-8` says the lock-free version "was paying for that rounding error with a
  hard crash, a silent-corruption bug and a lost-write bug."
- The only deliberate lock-free aliasing is `Result.Payload` (`pkg/core/engine.go:278-282`), documented
  as a possible *stale* read, never a use-after-free.
- Exercised under `-race` by `pkg/core/engine_test.go:309` — `TestConcurrentHammer` (8 readers,
  4 writers, 300 rounds), asserting no NaN scores, no scores outside [-1, 1], and no empty-id results
  (i.e. no torn vector read and no slot recycled mid-scan).
- Caveat: handlers ignore `context.Context` (`pkg/api/grpc_server.go:35,75,127,143`), so a client
  cancelling a slow query does not release the lock any sooner.

---

## Additional findings

**Vector dimensionality — fixed for the whole process, at open time.** `core.New(dims, capacity)`
(`pkg/core/engine.go:79`) eagerly allocates `capacity*dims` and stores `dims` immutably (`Dims()` at
`pkg/core/engine.go:124` reads the field without a lock). The server flag `-dims` defaults to `768`
(`cmd/mindb-server/main.go:26`). When a snapshot is loaded, **the snapshot's header `dims` wins and the
flag is only logged** (`cmd/mindb-server/main.go:100-103`; header parsed at `pkg/core/snapshot.go:169`).
Any insert of a different length is rejected with `ErrDimensionMismatch` (`pkg/core/engine.go:147`).

**Collections / namespaces — not present in the source.** One flat index per process. `namespace`
appears only as the FlatBuffers namespace declaration (`fbs/mindb.fbs:1`). No collection, tenant,
index-name or partition field exists in the schema, the engine or the CLI. Multiple logical corpora
require multiple server processes, or an id-prefix convention plus client-side filtering of results.

**Index type — brute-force exact scan plus a lossless int8 pre-filter. No ANN index.**
- Plain path: `pkg/core/engine.go:300` — `(*Engine).scan`; `pkg/core/engine.go:347` — `scanRange`.
- Cascade path: `pkg/core/cascade.go:47` — `(*Engine).searchCascade` (pass 1 `scanBounds` in int8 →
  threshold τ → pass 2 survivor filter → pass 3 exact float32 rescore), with the AVX2 kernel in
  `pkg/math/dotint8_avx2_amd64.s` generated by `pkg/math/avo/asm.go`.
- **No HNSW, no IVF, no PQ, no graph, no clustering** anywhere in the repo. `docs/README.md:251`:
  "MinDB is **not** a replacement for HNSW at scale." Query cost is O(N × dims).

**How a client would connect.**
- Default address `:50051` — `cmd/mindb-server/main.go:25`:
  `flag.String("addr", ":50051", "gRPC listen address")`; plain TCP via `net.Listen("tcp", addr)`
  (`cmd/mindb-server/main.go:50`).
- **No TLS and no authentication of any kind.** `grpc.NewServer` is built with `ForceServerCodec` only
  (`cmd/mindb-server/main.go:58`) — no `grpc.Creds`, no interceptor, no token or metadata check in
  `pkg/api`. Treat it as a trusted-network sidecar.
- Clients **must** force the FlatBuffers codec on every call or requests fail at request time; the
  working pattern is `pkg/api/grpc_server_test.go:122-136`.
- Full flag set (`cmd/mindb-server/main.go:24-30`): `-addr :50051`, `-dims 768`, `-capacity 100000`,
  `-snapshot ""` (empty disables persistence), `-snapshot-interval 0` (0 disables; requires
  `-snapshot`, else startup fails with "-snapshot-interval requires -snapshot",
  `cmd/mindb-server/main.go:46-48`).
- Build: `make build` → `go build -o bin/mindb-server cmd/mindb-server/main.go` (`Makefile:8-9`).
  Regenerating bindings needs the `flatc` binary: `flatc --go --grpc -o pkg/ fbs/mindb.fbs` (`Makefile:5-6`).

**Existing generated clients.** Go only — `pkg/mindb/VectorService_grpc.go:30` —
`NewVectorServiceClient(cc *grpc.ClientConn)`. No Python, TypeScript, Rust, Java or C++ bindings are
checked in and no codegen target for them exists in the `Makefile`. A non-Go client would need
`flatc --<lang> --grpc` against `fbs/mindb.fbs` plus a FlatBuffers gRPC codec for that language.

**Health / readiness — not present in the source.** No gRPC health-checking service, no HTTP endpoint,
no metrics, no reflection service is registered. The only liveness signals are the startup logs
`"engine ready: dims=%d capacity=%d loaded=%d approx_ram=%s"` (`cmd/mindb-server/main.go:43`) and
`"listening on %s"` (`cmd/mindb-server/main.go:68`), plus a non-zero exit on a bad snapshot.
**"Segments are loaded" has no analogue — there are no segments.** The closest equivalent is
"`core.Load` completed and `engine.Len() > 0`", observable only in the log, not over the wire. A
readiness probe today must be a TCP connect to `:50051` or a dummy `Search`.

**Doc vs. code disagreements (code wins).**
- The "LSM-style engine with write buffer → immutable segment → WAL flush/compaction" framing does not
  match the code, and the repo's own docs agree with the code: `pkg/core/engine.go:214-217` — "There is
  no compaction: capacity is preallocated"; `free []uint32` is "recycled slots, replacing compaction
  entirely" (`pkg/core/engine.go:61`). No WAL, memtable or segment type exists in any file.
- `docs/README.md:189` and `docs/ARCHITECTURE.md:342` claim "Go 1.21+"; `go.mod:3` says `go 1.25.0`.
- `docs/README.md:190` and `docs/ARCHITECTURE.md:345` claim that without AVX2 MinDB "warns loudly at
  startup". **It does not.** `math.KernelName()` (`pkg/math/kernel.go:15`) is referenced only by a test;
  `cmd/mindb-server/main.go` never logs the kernel or emits a warning. The fallback is silent —
  `HasFastInt8()` is consumed only to set `useCascade` (`pkg/core/engine.go:98`).
- `docs/ARCHITECTURE.md:372` references `make validate` for pruning-ratio validation against
  `glove-100-angular` / `gist-960-euclidean`. **No `validate` target exists** — the `Makefile` has only
  `all`, `flatc` and `build`, and no dataset-validation code is checked in.
- Otherwise `FEATURES.md` and `DECISIONS.md` match the code closely on upsert semantics, batching,
  non-transactional inserts, snapshot sequencing and the lock model.

---

## Gaps

For a RAG ingest/query workload, with an estimate of how large each change would be.

1. **Metadata filtering on search — missing. Medium change.** No filter field in `SearchRequest`
   (`fbs/mindb.fbs:17`) and no predicate in `Engine.Search` (`pkg/core/engine.go:242`). A naive
   post-filter is mechanically small (schema field + decode + skip non-matching slots in `scanRange`),
   but it touches the "frozen" wire contract, and it interacts with the cascade's τ bound
   (`pkg/core/cascade.go:47`) — filtering must happen *before* the heap push or the top-k guarantee
   breaks. The payload is an opaque blob, so the engine would also need a parsed/indexed metadata
   representation to filter on. *Workaround today:* over-fetch (`k * 5`) and filter client-side by
   parsing `SearchResult.payload`.
2. **No point lookup by id — missing. Small change.** No `Get(id)` in `pkg/core` and no matching RPC,
   so "is appid 570 already ingested, and with which embedding model version?" cannot be answered
   without a similarity search. `idMap` (`pkg/core/engine.go:60`) makes the engine method ~15 lines;
   the cost is a schema change plus regeneration.
3. **No client-streaming ingest, 4 MiB message ceiling — small change.** Client-side chunking works
   today (~1,300 × 768-dim vectors per call); raising the limit is one `grpc.MaxRecvMsgSize` option at
   `cmd/mindb-server/main.go:58`. A true streaming `Insert` needs a streaming `rpc_service` entry in
   `fbs/mindb.fbs` and regenerated flatc gRPC output.
4. **Non-atomic batches — medium/large change.** `pkg/api/grpc_server.go:31-34` documents that a failed
   batch leaves earlier vectors committed. True atomicity needs a transaction boundary the engine does
   not have; a validate-all-then-apply pre-pass (dims, empty id, zero vector, free capacity) would
   remove most partial-failure cases cheaply but not capacity races. *Mitigation:* inserts are upserts
   (Q1), so retrying a whole failed batch is idempotent and safe.
5. **Fixed capacity and fixed dims at boot — large change.** `-capacity` is reserved eagerly
   (`pkg/core/engine.go:79`) and cannot grow; overflow returns `ResourceExhausted`. Switching embedding
   model to a different dimensionality requires a restart with a new `-dims` and a fresh snapshot file.
   Plan headroom (e.g. `-capacity` = 2× expected corpus).
6. **No collections/namespaces — large change.** One flat index per process. Keeping, say, game
   descriptions separate from review chunks means two processes/ports, or an id-prefix convention with
   client-side filtering (which compounds gap 1).
7. **No crash durability between snapshots — large change.** `pkg/core/snapshot.go` is the entire
   persistence layer; there is no WAL. Mitigation today is `-snapshot-interval` plus an explicit
   `Snapshot` RPC after each ingest batch; a `kill -9` still loses everything since the last save, and
   each save rewrites the whole corpus, which caps snapshot frequency.
8. **No health/readiness endpoint — small change.** Registering `google.golang.org/grpc/health` and
   reflection at `cmd/mindb-server/main.go:58` is a few lines; `Engine.Len()`/`Engine.Cap()` already
   exist and are lock-safe.
9. **No observability — small/medium change.** No stats RPC, no metrics, no latency counters, and no
   wire-visible signal of which search path ran (`Engine.Cascade()` exists in Go only, and
   `searchCascade`'s survivor count is discarded at `pkg/core/engine.go:268`).
10. **No context/deadline propagation — small change.** All four handlers take `_ context.Context`
    (`pkg/api/grpc_server.go:35,75,127,143`), so client timeouts cannot abort work already holding the lock.
11. **Cosine only — medium change.** Vectors are normalized at insert (`pkg/core/engine.go:152-155`) and
    the cascade's error bound depends on `‖q‖ = 1` (`pkg/math/quantize.go:12`). Supporting raw dot
    product or L2 means storing norms and reworking the bound. For RAG with normalized embeddings this
    is a non-issue.
12. **Scaling ceiling.** Exact O(N × dims) scan; `docs/README.md:251` recommends HNSW past roughly a
    million vectors. A Steam-sized catalog (~100k–250k items × 768 dims) is inside the design target,
    but chunk-level RAG (many chunks per game) could push past it.

## Open questions

- **Actual query latency on target hardware.** `docs/ARCHITECTURE.md:185` explicitly retires the
  "sub-millisecond at 100k × 768 on consumer hardware" claim, and the tables in `docs/README.md:57-69`
  are the author's own measurements, not derivable from source. `BenchmarkSearch`
  (`pkg/core/engine_test.go:438`) and `BenchmarkSearchPaths` (`pkg/core/cascade_test.go:356`) exist but
  were not run during this review.
- **Practical maximum payload size.** Nothing limits payload length on insert; the only bounds found are
  the 4 MiB gRPC receive default and `1<<28` on snapshot *load* (`pkg/core/snapshot.go:235`). Whether
  large payloads degrade `Save` throughput or GC behaviour is unmeasured.
- **Behaviour when `-capacity` memory cannot be allocated.** `New` relies on `make` succeeding
  (`pkg/core/engine.go:90-96`), so the failure mode is a Go runtime OOM rather than a returned error;
  the "the process either has the memory or fails here" claim (`pkg/core/engine.go:76-78`) was not
  tested here.
- **Longevity of `grpc.Invoke`**, the deprecated call used by the flatc-generated client
  (`pkg/mindb/VectorService_grpc.go:37`). It exists in v1.82.0
  (`google.golang.org/grpc@v1.82.0/call.go:59`) and the module builds today, but a future removal would
  require patching generated code the repo says must not be hand-edited.
- **Overlapping `Save` calls** (periodic timer firing while a `Snapshot` RPC runs): both take `RLock`
  and both create distinct temp files via `os.CreateTemp(dir, base+".tmp*")` before renaming over the
  same path, so last rename wins. Whether any observer can see a torn state was not reasoned through
  beyond "each file is individually complete and CRC-valid."
- **Whether `fbs/mindb.fbs` is genuinely frozen** (`docs/README.md:245` calls it "the wire contract
  (frozen)"), which determines how acceptable the schema changes behind gaps 1–3 are.
