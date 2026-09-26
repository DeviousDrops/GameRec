"""Rebuild MinDB from the Game Document Store. No Steam requests (D9, D13).

This is what makes the derived-index stance in ADR-0003 affordable rather than theoretical. Anything
that invalidates the vectors -- a new embedding model, a new render template, a lost snapshot -- is
recovered by re-embedding documents that are already on disk, instead of spending days re-fetching
176k games from Steam.

    documents.jsonl --[latest-wins]--> render at current template --> embed --> upsert --> snapshot

Idempotent, because every insert is keyed by appid. Running it twice is a waste of CPU and nothing
else, which is the property that makes it safe to reach for when something has gone wrong.
"""

from __future__ import annotations

import argparse
import logging
import sys

from gamerec.checkpoint import Checkpoint
from gamerec.config import Config
from gamerec.corpus import CorpusStore
from gamerec.documents import TEMPLATE_VERSION
from gamerec.embeddings import Embedder
from gamerec.mindb import MinDBClient

log = logging.getLogger("reindex")

BATCH_SIZE = 200  # ~200 x 384 float32 is well under MinDB's ~4 MiB message cap.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compact",
        action="store_true",
        help="drop superseded lines from documents.jsonl first (rewrites the corpus)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config()
    store = CorpusStore(config.documents_path)

    if args.compact:
        log.info("compacted away %d superseded lines", store.compact())

    documents = store.load()
    if not documents:
        log.error("%s holds no documents; nothing to reindex from", config.documents_path)
        return 1
    log.info("reindexing %d documents from %s", len(documents), config.documents_path)

    embedder = Embedder()
    client = MinDBClient(config.mindb_addr)
    client.wait_ready()

    stats = client.stats()
    if stats.dims != len(embedder.embed_query("probe")):
        log.error("MinDB is %d-dimensional, the model produces %d; capacity is fixed at startup, so "
                  "this needs a MinDB restarted with -dims set to match",
                  stats.dims, len(embedder.embed_query("probe")))
        return 1
    if stats.capacity < len(documents):
        log.error("MinDB holds %d vectors at most and the corpus has %d documents; restart it with a "
                  "larger -capacity (D21)", stats.capacity, len(documents))
        return 1

    # The old checkpoint describes a corpus that is about to stop existing, so it is replaced
    # wholesale rather than merged: a reindex touches every appid by definition.
    checkpoint = Checkpoint(embedder.model_stamp, TEMPLATE_VERSION)
    inserted = 0
    for batch_number, start in enumerate(range(0, len(documents), BATCH_SIZE), start=1):
        batch = documents[start : start + BATCH_SIZE]
        vectors = embedder.embed_documents([d.render() for d in batch])
        inserted += client.insert(
            [(d.vector_id, v, d.payload(embedder.model_stamp)) for d, v in zip(batch, vectors)]
        )
        log.info("upserted %d/%d", inserted, len(documents))

        if batch_number % config.snapshot_every_batches == 0:
            done = documents[: start + len(batch)]
            checkpoint.mark_pending(config.checkpoint_path, {d.appid for d in done})
            ok, message = client.snapshot()
            log.info("snapshot ok=%s %s", ok, message)

    checkpoint.mark_pending(config.checkpoint_path, {d.appid for d in documents})
    ok, message = client.snapshot()
    log.info("final snapshot ok=%s %s", ok, message)
    if not ok:
        log.error("the corpus is rebuilt in memory but not on disk; leaving the checkpoint pending")
        client.close()
        return 1

    checkpoint.mark_complete(config.checkpoint_path)
    log.info("reindexed to %s; corpus now holds %d vectors",
             f"{embedder.model_stamp} template v{TEMPLATE_VERSION}", client.stats().vector_count)
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
