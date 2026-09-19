"""The query service. Embeds the request, asks MinDB for neighbours, optionally narrates."""

from __future__ import annotations

import json
import logging

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


@app.on_event("startup")
def startup() -> None:
    state["embedder"] = Embedder()
    state["mindb"] = MinDBClient(config.mindb_addr)
    state["mindb"].wait_ready()
    state["names"] = (
        NameIndex.load(config.names_path) if config.names_path.exists() else NameIndex([])
    )
    log.info("ready: %d names, corpus %d", len(state["names"]), state["mindb"].stats().vector_count)


@app.get("/health")
def health() -> dict:
    """Reports the kernel MinDB actually selected, so benchmark numbers can be tied to one."""
    stats = state["mindb"].stats()
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
        "name_index_size": len(state["names"]),
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
    embedder, mindb, names = state["embedder"], state["mindb"], state["names"]

    query_vector, seed_entry, exclude = None, None, set()
    if q:
        query_vector = embedder.embed_query(q)
    if seed:
        resolution = names.resolve(seed)
        if not resolution.usable:
            raise HTTPException(404, resolution.reason or f"unknown game {seed!r}")
        seed_entry = resolution.entry
        seed_id = f"appid:{seed_entry.appid}"
        vectors = mindb.get([seed_id])
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

    hits = mindb.search(query_vector, top_k + len(exclude))
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
