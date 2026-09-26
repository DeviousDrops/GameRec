"""Ingest: fetch a popularity-ordered slice of the catalogue, embed it, upsert it into MinDB.

Hardened per Phase 2. The properties that hold, and why each one is load-bearing:

    resumable      Every appid the run sees goes into the checkpoint, so the next run starts where
                   this one stopped rather than re-paying for the same fetches (D14, ADR-0005).
    idempotent     Insert is keyed by appid and the corpus is append-with-latest-wins, so a rerun --
                   including one that crashed halfway -- converges instead of duplicating (D13).
    paced          One token bucket for the whole run, sized by config, so the request rate is a
                   budget rather than a hope (D17).
    bounded        New appids first, then refreshes up to max_updates_per_run. Without the cap a big
                   Steam change day starves out new games, which is what users actually notice (D15).
    honest         The checkpoint is marked complete only after Snapshot succeeds, so a restore never
                   pairs a snapshot with a checkpoint claiming more than it holds (ADR-0002).

The Ingest Lease that makes "one ingest at a time" enforceable rather than conventional (D17) needs
object storage, so it lands with R2 in Phase 4. Until then two concurrent runs would each pace
correctly and jointly exceed the budget.
"""

from __future__ import annotations

import argparse
import logging
import sys

import httpx

from gamerec.checkpoint import Checkpoint
from gamerec.config import Config
from gamerec.corpus import CorpusStore
from gamerec.documents import TEMPLATE_VERSION, GameDocument
from gamerec.embeddings import Embedder
from gamerec.mindb import MinDBClient
from gamerec.names import (
    FILTERED_LOW_REVIEWS, IN_CORPUS, NOT_A_GAME, NameEntry, NameIndex,
)
from gamerec.stamp import Stamp, StampMismatch, assert_ingestable
from ingest.steam import Popular, fetch_details, fetch_popular
from ingest.throttle import RateLimiter

log = logging.getLogger("ingest")

BATCH_SIZE = 200  # ~200 x 384 float32 is well under MinDB's ~4 MiB message cap.


def plan(candidates: list[Popular], seen: set[int], max_updates: int) -> list[Popular]:
    """New appids first, then refreshes of ones already seen, capped (D15).

    The order matters more than the cap does. A run that spends its budget refreshing games already
    in the corpus looks busy and adds nothing anyone asked for.
    """
    fresh = [c for c in candidates if c.appid not in seen]
    stale = [c for c in candidates if c.appid in seen]
    if len(stale) > max_updates:
        log.info("deferring %d refreshes to the next run (cap %d)",
                 len(stale) - max_updates, max_updates)
        stale = stale[:max_updates]
    return fresh + stale


def fetch_one(
    popular: Popular, client: httpx.Client, limiter: RateLimiter, review_floor: int
) -> tuple[GameDocument | None, NameEntry]:
    """Returns (document | None, name entry), so filtered games still reach the name index.

    The review floor is applied before the fetch because SteamSpy supplies the count -- that is the
    one filter that saves a request rather than only saving index space (D4).
    """
    if popular.review_count < review_floor:
        return None, NameEntry(popular.appid, popular.name, FILTERED_LOW_REVIEWS)

    details = fetch_details(popular.appid, client, limiter=limiter)
    if details is None or details.get("type") != "game":
        return None, NameEntry(popular.appid, popular.name, NOT_A_GAME)

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
    return document, NameEntry(popular.appid, document.name, IN_CORPUS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200,
                        help="how many popularity-ordered games to consider this run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config()
    config.corpus_dir.mkdir(parents=True, exist_ok=True)

    embedder = Embedder()
    current = Stamp(embedder.model_stamp, TEMPLATE_VERSION)
    recorded = Checkpoint.load(config.checkpoint_path)
    try:
        assert_ingestable(current, Stamp(recorded.model_stamp, recorded.template_version)
                          if recorded else None)
    except StampMismatch as mismatch:
        log.error("%s", mismatch)
        return 1

    client = MinDBClient(config.mindb_addr)
    client.wait_ready()
    stats = client.stats()
    if stats.dims != len(embedder.embed_query("probe")):
        log.error("MinDB is %d-dimensional, the model produces %d. Reindex required.",
                  stats.dims, len(embedder.embed_query("probe")))
        client.close()
        return 1

    store = CorpusStore(config.documents_path)
    # The checkpoint is the record of what was ingested; the corpus is what actually exists. They
    # can disagree if a run died between appending and checkpointing, so resumption trusts the union
    # -- re-fetching is only wasted time, while skipping a gap leaves a hole nothing will fill.
    checkpoint = recorded or Checkpoint(current.model_stamp, current.template_version)
    checkpoint.appids |= store.appids()

    limiter = RateLimiter(config.steam_requests_per_min, config.steam_burst)
    candidates = fetch_popular(args.limit, limiter=limiter)
    targets = plan(candidates, checkpoint.appids, config.max_updates_per_run)
    log.info("%d candidates, %d to fetch this run (%d already seen)",
             len(candidates), len(targets), len(checkpoint.appids))

    with httpx.Client(timeout=30, headers={"User-Agent": "GameRec/0.1"}) as http:
        for batch_number, start in enumerate(range(0, len(targets), BATCH_SIZE), start=1):
            batch = targets[start : start + BATCH_SIZE]
            documents, entries = [], []
            for i, popular in enumerate(batch, start=start + 1):
                document, entry = fetch_one(popular, http, limiter, config.review_floor)
                entries.append(entry)
                if document is not None:
                    documents.append(document)
                    log.info("[%d/%d] %s", i, len(targets), document.name)

            # Corpus first, always: it is the source of truth, and a vector whose document was never
            # written is a vector no reindex can rebuild (D13).
            store.append(documents)
            NameIndex.merge(config.names_path, entries)

            if documents:
                vectors = embedder.embed_documents([d.render() for d in documents])
                upserted = client.insert(
                    [(d.vector_id, v, d.payload(embedder.model_stamp))
                     for d, v in zip(documents, vectors)]
                )
                log.info("upserted %d vectors", upserted)

            # Claimed before the snapshot, so the checkpoint can only ever lag it (ADR-0002).
            checkpoint.mark_pending(config.checkpoint_path, {p.appid for p in batch})
            if batch_number % config.snapshot_every_batches == 0:
                ok, message = client.snapshot()
                log.info("snapshot ok=%s %s", ok, message)

    ok, message = client.snapshot()
    log.info("final snapshot ok=%s %s", ok, message)
    if not ok:
        # Leaving it pending is the honest outcome: the work is in memory and in the corpus, but no
        # snapshot pairs with it, so no restore should trust this checkpoint.
        log.error("snapshot failed; checkpoint left pending")
        client.close()
        return 1

    checkpoint.mark_complete(config.checkpoint_path)
    log.info("corpus holds %d documents, %d vectors; checkpoint covers %d appids",
             store.line_count(), client.stats().vector_count, len(checkpoint.appids))
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
