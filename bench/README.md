# Benchmarks

Every number here is labelled with the architecture and the kernel MinDB actually selected,
because MinDB's int8 cascade has an AVX2 kernel that does not exist on ARM (ADR-0006, D23). The
deployed service runs on an Ampere A1, so **none of the figures below describe production** — they
describe the same code on x86, and the gap is the point of labelling them.

```
python -m bench.latency api    --url http://api:8000   -n 200   # what a user waits for
python -m bench.latency search --addr 127.0.0.1:50051  -n 300   # MinDB alone
python -m bench.latency embed                          -n 100   # the model on this CPU
```

Run them **inside the image**, not on the host. On this machine the host Python embeds a query in
~40 ms and the container does it in 3.9 ms — same fastembed version, same quantised ONNX model —
so a host-side number would be measuring a Windows Python install nobody deploys:

```bash
docker run --rm --network gamerec_default -v "$PWD/bench:/app/bench:ro" -w /app \
  ghcr.io/deviousdrops/gamerec-api:0.1.0 python -m bench.latency api --url http://api:8000
```

## x86_64, kernel avx2

13th Gen Intel Core i7-13700H (20 threads), Docker Desktop 29.7.2, `fast_int8=true`, 384 dims.
`-n` samples per row, nearest-rank percentiles, milliseconds.

**MinDB Search, 200,000 vectors** — the deployed corpus size, filled with random unit vectors by
`bench/synthetic.py`. Measured from a container on MinDB's own network namespace.

| what | n | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| Search k=5 | 300 | 4.5 | 4.0 | 6.4 | 15.9 | 16.9 |
| Search k=50 | 300 | 5.3 | 5.0 | 7.1 | 9.5 | 14.7 |
| Stats (empty round trip) | 300 | 0.3 | 0.3 | 0.4 | 0.5 | 0.5 |

An exact-kNN store scans everything, so that 4 ms is linear in the corpus: the same call against
the 200-document dev corpus is ~0.5 ms, and it would be ~8 ms at 400,000. The empty round trip
says how much of it is FlatBuffers and gRPC rather than arithmetic — 0.3 ms, which is why no
effort has gone into batching queries.

**The embedding model** — bge-small-en-v1.5, quantised ONNX, in the image.

| what | n | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| embed_query (1 text) | 100 | 4.1 | 3.9 | 5.5 | 6.9 | 6.9 |
| embed_documents (8 texts) | 10 | 45.0 | 45.3 | 57.6 | 57.6 | 57.6 |

5.6 ms per document batched against 3.9 ms for a single query: batching is worth about 30%, which
is why ingest embeds in batches of 200 and the API does not try to batch across requests.

**The API end to end** — 200-document dev corpus, narration off, measured from a container on the
same Docker network as the API.

| what | n | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| `/recommend?q=` (mood) | 200 | 8.5 | 7.6 | 15.2 | 21.2 | 32.2 |
| `/recommend?seed=` (game) | 200 | 3.7 | 2.7 | 8.3 | 19.5 | 22.6 |

The mood path is 3.9 ms of embedding, ~0.5 ms of Search on a corpus this small, and ~3 ms of
FastAPI, JSON and HTTP. The seed path is faster because it embeds nothing: the name index resolves
in memory, then one Get and one Search. Substituting the 200,000-vector Search puts a mood query
at roughly 11 ms on this CPU — an arithmetic estimate, not a measurement, because the dev corpus
is 200 documents.

Narration is excluded by default. With `--narrate` the number is Groq's queue, measured in
hundreds of milliseconds, and `?narrate=false` exists precisely so retrieval can be measured
without it (D6).

## arm64, kernel pure-go

Not measured yet. It needs the A1, and the honest placeholder is an empty table rather than the
x86 figures with a caveat attached — the whole reason ADR-0006 exists is that quoting one for the
other would be the easiest dishonest thing in this project.

| what | n | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|

## Reading the extra column

`bench.latency` prints a `first->last tenth` column that the tables above leave out: the mean of
the first tenth of samples against the last tenth, in the order the calls happened. Percentiles
sort the samples and so hide a machine that slows down under load, and this laptop does exactly
that — a hundred back-to-back embeddings on the *host* Python drift from 52 ms to 265 ms. In the
image the same run holds at 5.0 -> 3.7 ms, which is what makes the container numbers trustworthy
and the host ones not.

## Filling a scratch MinDB

`bench/synthetic.py` inserts random unit vectors so Search can be timed at the deployed scale.
Random is fine for latency — the scan is the same arithmetic whatever the vectors mean — and
useless for recall, which this tool does not claim to measure.

```bash
docker run -d --rm --name mindb-bench -p 50061:50051 ghcr.io/deviousdrops/mindb:v0.1.0 \
  -dims=384 -capacity=220000 -snapshot=/tmp/bench.snap -snapshot-interval=0 -health-addr=
python -m bench.synthetic --addr 127.0.0.1:50061 --count 200000   # ~4.5 min at ~750 vectors/s
docker stop mindb-bench
```

It refuses to run against a store that already holds more than a thousand vectors, because 200,000
junk vectors in a store whose capacity is 200,000 is a full store.
