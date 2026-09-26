"""Latency benchmark, labelled by the kernel MinDB actually selected (ADR-0006, D23).

    python -m bench.latency api    --url http://127.0.0.1:8000 -n 200
    python -m bench.latency search --addr 127.0.0.1:50051      -n 500
    python -m bench.latency embed                              -n 200

Three modes because one number hides the answer. `api` is what a user waits for; `embed` is the
model on this CPU; `search` is MinDB alone over the wire. api - embed - search is the framing,
JSON and name-resolution overhead, and that is the part worth being suspicious of.

Every report carries the architecture, MinDB's kernel and the corpus size, because none of the
numbers mean anything without them: MinDB's int8 cascade has an AVX2 kernel that does not exist on
ARM, so the deployed service is slower than any x86 figure for the same code. Quoting one for the
other would be the easiest dishonest thing in this project, which is why the label comes from
`/health` rather than from whoever is writing the README.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time
from dataclasses import dataclass

import httpx

# Deliberately ordinary. A benchmark on hand-picked queries that happen to hit the same neighbours
# measures the cache nobody has, so these are the kinds of thing the corpus is actually asked.
QUERIES = [
    "relaxing farming game with no combat",
    "hard soulslike with tight combat",
    "co-op survival crafting in space",
    "short narrative game about grief",
    "roguelike deckbuilder",
    "open world driving with good physics",
    "detective game where you read documents",
    "colony sim that gets out of hand",
]


@dataclass
class Sample:
    label: str
    seconds: list[float]

    def percentile(self, p: float) -> float:
        # Nearest-rank on sorted samples: no interpolation, so every reported number is a run that
        # actually happened.
        ordered = sorted(self.seconds)
        index = min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1)
        return ordered[index]

    def drift(self) -> tuple[float, float]:
        """Mean of the first tenth against the last tenth, in the order the calls happened.

        Percentiles hide a machine that slows down under sustained load, and a laptop does exactly
        that: on this dev box a hundred back-to-back embeddings run roughly five times slower than
        the first twenty. Sorting the samples turns that into a wide p95 and a plausible-looking
        p50, so the drift is reported separately -- if these two numbers differ, the benchmark is
        partly measuring power management rather than code.
        """
        tenth = max(1, len(self.seconds) // 10)
        return (statistics.mean(self.seconds[:tenth]) * 1000,
                statistics.mean(self.seconds[-tenth:]) * 1000)

    def row(self) -> str:
        ms = [s * 1000 for s in self.seconds]
        first, last = self.drift()
        return (f"| {self.label} | {len(ms)} | {statistics.mean(ms):.1f} | "
                f"{self.percentile(50) * 1000:.1f} | {self.percentile(95) * 1000:.1f} | "
                f"{self.percentile(99) * 1000:.1f} | {max(ms):.1f} | {first:.1f} -> {last:.1f} |")


def report(samples: list[Sample], context: dict) -> None:
    print()
    for key, value in context.items():
        print(f"{key}: {value}")
    print()
    print("| what | n | mean ms | p50 | p95 | p99 | max | first->last tenth |")
    print("|---|---|---|---|---|---|---|---|")
    for sample in samples:
        print(sample.row())


def host_context() -> dict:
    return {"host": f"{platform.machine()} / {platform.system()} / "
                    f"python {platform.python_version()}"}


def mindb_context(health: dict) -> dict:
    mindb = health.get("mindb", {})
    return {
        "mindb": f"kernel={mindb.get('kernel')} goarch={mindb.get('goarch')} "
                 f"fast_int8={mindb.get('fast_int8')}",
        "corpus": f"{health.get('corpus_size')} vectors of {health.get('capacity')} capacity, "
                  f"{health.get('dims')} dims",
        "model": health.get("model_stamp"),
    }


def time_calls(label: str, call, n: int, warmup: int) -> Sample:
    """Warm up outside the sample. The first call pays for a connection, a lazily built kernel
    selection and a cold page cache, and reporting that as a percentile misrepresents every
    subsequent one."""
    for _ in range(warmup):
        call(0)
    seconds = []
    for i in range(n):
        start = time.perf_counter()
        call(i)
        seconds.append(time.perf_counter() - start)
    return Sample(label, seconds)


def bench_api(args) -> int:
    with httpx.Client(base_url=args.url, timeout=30) as http:
        health = http.get("/health").raise_for_status().json()
        context = host_context() | mindb_context(health) | {
            "endpoint": f"{args.url}/recommend?narrate={'true' if args.narrate else 'false'}",
        }

        def mood(i: int) -> None:
            response = http.get("/recommend", params={"q": QUERIES[i % len(QUERIES)],
                                                      "narrate": args.narrate})
            response.raise_for_status()

        samples = [time_calls("/recommend?q= (mood)", mood, args.n, args.warmup)]

        # A seed query costs an extra Get before the Search, so it is a different number, not the
        # same one with noise.
        first = http.get("/recommend", params={"q": QUERIES[0], "narrate": False}).json()
        seeds = [r["name"] for r in first["results"]]
        if seeds:
            def seeded(i: int) -> None:
                http.get("/recommend", params={"seed": seeds[i % len(seeds)],
                                               "narrate": args.narrate}).raise_for_status()
            samples.append(time_calls("/recommend?seed= (game)", seeded, args.n, args.warmup))

    report(samples, context)
    return 0


def bench_search(args) -> int:
    """MinDB alone: embed once, then search that same vector repeatedly.

    Reusing one query vector is the point -- it takes the model out of the measurement entirely, so
    what is left is FlatBuffers encoding, the gRPC round trip and the scan.
    """
    from gamerec.embeddings import Embedder
    from gamerec.mindb import MinDBClient

    client = MinDBClient(args.addr)
    client.wait_ready()
    stats = client.stats()
    vector = Embedder().embed_query(QUERIES[0])

    samples = [time_calls(f"Search k={k}", lambda _i, k=k: client.search(vector, k),
                          args.n, args.warmup)
               for k in (5, 50)]
    samples.append(time_calls("Stats (empty round trip)", lambda _i: client.stats(),
                              args.n, args.warmup))
    client.close()

    report(samples, host_context() | {
        "mindb": f"kernel={stats.kernel_name} goarch={stats.goarch} fast_int8={stats.fast_int8}",
        "corpus": f"{stats.vector_count} vectors of {stats.capacity} capacity, {stats.dims} dims",
        "addr": args.addr,
    })
    return 0


def bench_embed(args) -> int:
    """The model on this CPU, which is the floor under every API number.

    Queries and documents are timed separately because BGE is asymmetric -- a query carries an
    instruction prefix and a document does not (ADR-0001) -- and because documents are embedded in
    batches of 200 during ingest, where per-item cost is what matters.
    """
    from gamerec.embeddings import Embedder

    embedder = Embedder()
    documents = [f"{q}. A game about {q}, with genres and categories listed here." for q in QUERIES]

    samples = [
        time_calls("embed_query (1)", lambda i: embedder.embed_query(QUERIES[i % len(QUERIES)]),
                   args.n, args.warmup),
        time_calls(f"embed_documents ({len(documents)})",
                   lambda _i: embedder.embed_documents(documents),
                   max(1, args.n // 10), min(args.warmup, 1)),
    ]
    report(samples, host_context() | {"model": embedder.model_stamp})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("api", "search", "embed"))
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--addr", default="127.0.0.1:50051")
    parser.add_argument("-n", type=int, default=200, help="samples per row")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--narrate", action="store_true",
                        help="include Groq narration, which measures someone else's free tier")
    args = parser.parse_args()

    return {"api": bench_api, "search": bench_search, "embed": bench_embed}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
