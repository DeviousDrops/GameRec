"""The query service. Embeds the request, asks MinDB for neighbours, optionally narrates."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import grpc
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from api.narrate import narrate
from gamerec import negation
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

# How many candidates to ask MinDB for when a query carries a negation, as a multiple of the page.
# Hits that look more like the negated span than the wanted one are dropped after the search, and
# without headroom a page of five could come back as a page of two (D63).
NEGATION_HEADROOM = 3


def _unavailable(error: grpc.RpcError) -> HTTPException:
    """503 with Retry-After, not 500: MinDB restarting is expected, and a 500 reads as a bug."""
    code = error.code().name.lower() if error.code() else "unknown"
    log.warning("MinDB call failed: %s", code)
    return HTTPException(
        503, f"MinDB is unavailable ({code}); it may be restarting",
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def _negated_out(mindb: MinDBClient, hits: list, query_vector: np.ndarray,
                 negated_vector: np.ndarray) -> set[str]:
    """Hit ids that look more like what the user ruled out than like what they asked for.

    Cutting the negated clause out of the query is most of the fix, but it cannot help when what is
    left implies what was removed: "a soulslike game but not dark souls" leaves "a soulslike game",
    which ranks Dark Souls first on merit. So every candidate is scored against both directions and
    dropped when the unwanted one wins.

    Relative, not a threshold: cosines against "dark souls" and against "combat" live on completely
    different scales, so any fixed cut-off would have to be tuned per query. Asking which of the two
    the document is closer to needs no tuning, and it stays silent when the negated span is a broad
    concept that nothing in the corpus resembles especially closely -- measured, it fires on the
    Souls titles for "a soulslike game" and on nothing at all for "a relaxing farming game" (D63).
    """
    vectors = mindb.get([hit.id for hit in hits])
    dropped = set()
    for hit in hits:
        vector = vectors.get(hit.id)
        if vector is None:
            # MinDB knows the id -- it just returned it -- so this is a delete between the two
            # calls. Keeping the hit is the safe way to be wrong.
            continue
        vector = vector / np.linalg.norm(vector)
        if float(vector @ negated_vector) > float(vector @ query_vector):
            dropped.add(hit.id)
    return dropped


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
    """Whether the service is working. What it is built from only when HEALTH_DETAIL is set.

    The split is not about secrecy -- everything behind it is in a public repo. It is that this
    endpoint answers the open internet, and an attacker reads `kernel=avx2 goarch=amd64 capacity=200000`
    as a free fingerprint while an operator can get the same from inside the cluster. `dims` and the WAL
    flags stay on both sides because smoke.sh fails the deploy on them (D56).
    """
    try:
        stats = state["mindb"].stats()
    except grpc.RpcError as error:
        raise _unavailable(error) from error
    body = {
        "status": "ok",
        "corpus_size": stats.vector_count,
        "dims": stats.dims,
        "name_index_size": len(_names()),
        "wal_healthy": stats.wal_healthy if stats.wal_enabled else None,
    }
    if not config.health_detail:
        return body
    return body | {
        "capacity": stats.capacity,
        "model_stamp": MODEL_STAMP,
        "template_version": TEMPLATE_VERSION,
        "mindb": {
            "kernel": stats.kernel_name,
            "goarch": stats.goarch,
            "fast_int8": stats.fast_int8,
            "wal_enabled": stats.wal_enabled,
            "wal_healthy": stats.wal_healthy,
        },
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
    negated_vector, parsed = None, None
    if q:
        # The model has no representation for "not", so the negation is taken out of the text before
        # it ever reaches the model, and turned into a direction to push results away from (D63).
        parsed = negation.split(q)
        query_vector = embedder.embed_query(parsed.wanted)
        if parsed.negated:
            negated_vector = embedder.embed_query(parsed.negated)
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

    want = top_k + len(exclude)
    try:
        hits = mindb.search(query_vector, want * NEGATION_HEADROOM if negated_vector is not None
                            else want)
        if negated_vector is not None:
            exclude |= _negated_out(mindb, hits, query_vector, negated_vector)
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
        # What the negation was read as, so a query that comes back short or surprising can be
        # explained without guessing -- same reason template_version is per result (D22).
        "negated": parsed.negated if parsed else None,
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


# The frontend, out of this same image and behind this same Ingress (D50). Two things about where this
# sits: a Mount at "/" matches every path, so it has to be registered after the routes above or it
# would shadow all of them; and it is conditional because the image that runs the ingest is this image,
# and a checkout or a container without web/ should still serve the API rather than fail to import.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:
    log.warning("no web/ at %s; serving the API without a frontend", WEB_DIR)
