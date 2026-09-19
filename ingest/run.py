"""Phase 1 ingest: fetch a popularity-ordered sample, embed it, upsert it into MinDB.

Deliberately not hardened. Checkpoints, the ingest lease, configurable rate limits and the model
stamp refusal are Phase 2. What is already true here: the upsert is keyed by appid, so re-running
this is safe, and the corpus is written to JSONL first so a reindex never needs Steam again (D13).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import httpx

from gamerec.config import Config
from gamerec.documents import GameDocument
from gamerec.embeddings import Embedder
from gamerec.mindb import MinDBClient
from gamerec.names import (
    FILTERED_LOW_REVIEWS, IN_CORPUS, NOT_A_GAME, NameEntry, NameIndex,
)
from ingest.steam import DEFAULT_DELAY, fetch_details, fetch_popular

log = logging.getLogger("ingest")

BATCH_SIZE = 200  # ~200 x 384 float32 is well under MinDB's ~4 MiB message cap.


def build_documents(limit: int, review_floor: int, delay: float):
    """Yields (document | None, name entry) so that filtered games still reach the name index."""
    with httpx.Client(timeout=30, headers={"User-Agent": "GameRec/0.1"}) as client:
        for i, popular in enumerate(fetch_popular(limit, client), start=1):
            if popular.review_count < review_floor:
                yield None, NameEntry(popular.appid, popular.name, FILTERED_LOW_REVIEWS)
                continue

            details = fetch_details(popular.appid, client)
            time.sleep(delay)
            if details is None or details.get("type") != "game":
                yield None, NameEntry(popular.appid, popular.name, NOT_A_GAME)
                continue

            release = (details.get("release_date") or {}).get("date", "")
            document = GameDocument(
                appid=popular.appid,
                name=details.get("name") or popular.name,
                short_description=details.get("short_description", ""),
                genres=[g["description"] for g in details.get("genres", [])],
                categories=[c["description"] for c in details.get("categories", [])][:8],
                developers=details.get("developers", []),
                release_year=release.split(",")[-1].strip() if release else "",
                review_count=popular.review_count,
            )
            log.info("[%d/%d] %s", i, limit, document.name)
            yield document, NameEntry(popular.appid, document.name, IN_CORPUS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config()
    config.corpus_dir.mkdir(parents=True, exist_ok=True)

    embedder = Embedder()
    client = MinDBClient(config.mindb_addr)
    client.wait_ready()

    stats = client.stats()
    if stats.dims != len(embedder.embed_query("probe")):
        log.error("MinDB is %d-dimensional, the model produces %d. Reindex required.",
                  stats.dims, len(embedder.embed_query("probe")))
        return 1

    documents, names = [], []
    for document, entry in build_documents(args.limit, config.review_floor, args.delay):
        names.append(entry)
        if document is not None:
            documents.append(document)

    # JSONL first: the corpus is the source of truth, MinDB is the derived index (D13).
    with config.documents_path.open("w", encoding="utf-8") as handle:
        for document in documents:
            handle.write(document.to_jsonl() + "\n")
    NameIndex.dump(names, config.names_path)
    log.info("wrote %d documents and %d name entries", len(documents), len(names))

    inserted = 0
    for start in range(0, len(documents), BATCH_SIZE):
        batch = documents[start : start + BATCH_SIZE]
        vectors = embedder.embed_documents([d.render() for d in batch])
        inserted += client.insert(
            [(d.vector_id, v, d.payload(embedder.model_stamp)) for d, v in zip(batch, vectors)]
        )
        log.info("upserted %d/%d", inserted, len(documents))

    ok, message = client.snapshot()
    log.info("snapshot ok=%s %s", ok, message)
    log.info("corpus now holds %d vectors", client.stats().vector_count)
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
