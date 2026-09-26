"""Fill a scratch MinDB with random vectors, so search can be timed at the deployed scale.

    docker run -d --rm --name mindb-bench -p 50061:50051 ghcr.io/deviousdrops/mindb:v0.1.0 \
        -dims=384 -capacity=220000 -snapshot=/tmp/bench.snap -snapshot-interval=0
    python -m bench.synthetic --addr 127.0.0.1:50061 --count 200000
    python -m bench.latency search --addr 127.0.0.1:50061 -n 300

MinDB is an exact-kNN store: every Search scans every vector, so latency is linear in the corpus
size and a benchmark against the 200 documents in a dev corpus measures nothing anyone will
experience. The vectors are random rather than real because the scan cost does not depend on what
they mean -- it is the same arithmetic over the same number of floats either way. Recall does depend
on it, which is why this is a latency tool and says so.

Never point it at a real MinDB. It inserts ids under its own prefix, but 200,000 junk vectors in a
store whose capacity is 200,000 is a full store.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

from gamerec.mindb import MinDBClient

BATCH = 200  # Matches the ingest: ~200 x 384 float32 sits well under MinDB's ~4 MiB message cap.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--addr", default="127.0.0.1:50061",
                        help="a scratch MinDB, not the one holding the corpus")
    parser.add_argument("--count", type=int, default=200_000)
    parser.add_argument("--dims", type=int, default=384)
    parser.add_argument("--seed", type=int, default=7, help="fixed, so a rerun is the same corpus")
    args = parser.parse_args()

    client = MinDBClient(args.addr)
    client.wait_ready()
    stats = client.stats()
    if stats.dims != args.dims:
        print(f"MinDB is {stats.dims}-dimensional, asked for {args.dims}", file=sys.stderr)
        return 1
    if stats.vector_count > 1000:
        print(f"refusing: {stats.vector_count} vectors already here, which does not look scratch",
              file=sys.stderr)
        return 1

    rng = np.random.default_rng(args.seed)
    started = time.perf_counter()
    inserted = 0
    while inserted < args.count:
        size = min(BATCH, args.count - inserted)
        # Normalised, because everything real here is: the embedder returns unit vectors and MinDB's
        # cosine path is only exercised honestly by vectors of the same magnitude.
        block = rng.standard_normal((size, args.dims)).astype(np.float32)
        block /= np.linalg.norm(block, axis=1, keepdims=True)
        client.insert([
            (f"synthetic:{inserted + i}", block[i],
             json.dumps({"appid": inserted + i,
                         "name": f"Synthetic {inserted + i}"}).encode())
            for i in range(size)
        ])
        inserted += size
        if inserted % 20_000 == 0:
            rate = inserted / (time.perf_counter() - started)
            print(f"{inserted:>7,} vectors  {rate:,.0f}/s")

    final = client.stats()
    print(f"done: {final.vector_count:,} vectors of {final.capacity:,} capacity, "
          f"kernel={final.kernel_name} in {time.perf_counter() - started:.1f}s")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
