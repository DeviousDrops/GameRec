"""The query service. Embeds the request, asks MinDB for neighbours, optionally narrates."""

from __future__ import annotations

import json
import logging

import grpc
import numpy as np
from fastapi import FastAPI, HTTPException, Query

from api.narrate import narrate
from gamerec.config import Config
from gamerec.documents import TEMPLATE_VERSION
from gamerec.embeddings import MODEL_STAMP, Embedder
from gamerec.mindb import MinDBClient
from gamerec.names import NameIndex

log = logging.getLogger("api")

config = Config()
app = FastAPI(title="GameRec", version="0.1.0")

state: dict = {}

# MinDB deploys with the Recreate strategy, so a rollout is visible downtime measured in seconds
# (D7). Five is long enough to be worth honouring and short enough that a client retrying on it
# feels like a pause rather than an outage.
RETRY_AFTER_SECONDS = 5


def _unavailable(error: grpc.RpcError) -> HTTPException:
    """503 with Retry-After, not 500: MinDB restarting is expected, and a 500 reads as a bug."""
    code = error.code().name.lower() if error.code() else "unknown"
    log.warning("MinDB call failed: %s", code)
    return HTTPException(
        503, f"MinDB is unavailable ({code}); it may be restarting",
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def _names() -> NameIndex:
    """The name index, reloaded when the file underneath it changes.

    The nightly ingest rewrites names.json while the API keeps running (D14), so an index read once
    at startup would go stale by exactly the games a user is most likely to ask about -- the new
    ones. One stat() per request against a file that changes once a night is a fair trade.
    """
    try:
        mtime = config.names_path.stat().st_mtime_ns
    except FileNotFoundError:
        return state["names"]
    if mtime != state.get("names_mtime"):
        state["names"] = NameIndex.load(config.names_path)
        state["names_mtime"] = mtime
        log.info("name index loaded: %d entries", len(state["names"]))
    return state["names"]


@app.on_event("startup")
def startup() -> None:
    state["embedder"] = Embedder()
    state["mindb"] = MinDBClient(config.mindb_addr)
    state["names"] = NameIndex([])
    try:
        state["mindb"].wait_ready()
        log.info("ready: %d names, corpus %d", len(_names()), state["mindb"].stats().vector_count)
    except (grpc.FutureTimeoutError, grpc.RpcError):
        # Deliberately not fatal. Exiting here means a CrashLoopBackOff whose backoff outlasts the
        # MinDB restart that caused it, so the API would still be down after MinDB came back.
        # /readyz keeps traffic away until MinDB answers, which is what readiness is for.
        log.warning("MinDB at %s is not answering yet; serving nothing until it does",
                    config.mindb_addr)


@app.get("/livez")
def livez() -> dict:
    """Liveness: is this process working? It must not touch MinDB.

    A liveness probe that calls a dependency converts that dependency being down into this pod
    being killed -- restarting the one component that was still fine.
    """
    return {"status": "alive"}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness: can this process answer a query? That needs MinDB, so it is checked."""
    try:
        stats = state["mindb"].stats()
    except grpc.RpcError as error:
        raise _unavailable(error) from error
    return {"status": "ready", "corpus_size": stats.vector_count}


@app.get("/health")
def health() -> dict:
    """Reports the kernel MinDB actually selected, so benchmark numbers can be tied to one."""
    try:
        stats = state["mindb"].stats()
    except grpc.RpcError as error:
        raise _unavailable(error) from error
    return {
        "status": "ok",
        "corpus_size": stats.vector_count,
        "capacity": stats.capacity,
        "dims": stats.dims,
        "model_stamp": MODEL_STAMP,
        "template_version": TEMPLATE_VERSION,
        "mindb": {
            "kernel": stats.kernel_name,
            "goarch": stats.goarch,
            "fast_int8": stats.fast_int8,
            "wal_enabled": stats.wal_enabled,
            "wal_healthy": stats.wal_healthy,
        },
        "name_index_size": len(_names()),
    }


@app.get("/recommend")
def recommend(
    q: str | None = Query(None, description="free-text mood query"),
    seed: str | None = Query(None, description="a game name or a raw Steam appid"),
    k: int | None = Query(None, ge=1, le=50),
    narrate_results: bool = Query(True, alias="narrate"),
) -> dict:
    if not q and not seed:
        raise HTTPException(400, "give a mood query, a seed game, or both")

    top_k = k or config.top_k
    embedder, mindb, names = state["embedder"], state["mindb"], _names()

    query_vector, seed_entry, exclude = None, None, set()
    if q:
        query_vector = embedder.embed_query(q)
    if seed:
        resolution = names.resolve(seed)
        if not resolution.usable:
            raise HTTPException(404, resolution.reason or f"unknown game {seed!r}")
        seed_entry = resolution.entry
        seed_id = f"appid:{seed_entry.appid}"
        try:
            vectors = mindb.get([seed_id])
        except grpc.RpcError as error:
            raise _unavailable(error) from error
        if seed_id not in vectors:
            raise HTTPException(404, f"{seed_entry.name} is in the name index but not in MinDB")
        exclude.add(seed_id)
        seed_vector = vectors[seed_id]
        if query_vector is None:
            query_vector = seed_vector
        else:
            # Ranking by cosine against a normalised blend is exactly ranking by the weighted sum
            # of the two cosines, because the blend's magnitude is constant across documents (D10).
            blend = config.mood_weight * query_vector + (1 - config.mood_weight) * seed_vector
            query_vector = blend / np.linalg.norm(blend)

    try:
        hits = mindb.search(query_vector, top_k + len(exclude))
    except grpc.RpcError as error:
        raise _unavailable(error) from error
    results = []
    for hit in hits:
        if hit.id in exclude:
            continue
        payload = json.loads(hit.payload) if hit.payload else {}
        results.append(
            {
                "appid": payload.get("appid"),
                "name": payload.get("name", hit.id),
                "score": round(float(hit.score), 4),
                "short_description": payload.get("short_description", ""),
                "genres": payload.get("genres", []),
                # Surfaced per result so a mixed v1/v2 corpus is visible, not hidden (D22).
                "template_version": payload.get("template_version"),
            }
        )
        if len(results) == top_k:
            break

    response = {
        "query": q,
        "seed": {"appid": seed_entry.appid, "name": seed_entry.name} if seed_entry else None,
        "results": results,
        "narration": None,
    }
    if narrate_results and results:
        request_text = q or f"games like {seed_entry.name}"
        response["narration"] = narrate(
            request_text, results, config.groq_api_key, config.groq_model
        )
    return response
