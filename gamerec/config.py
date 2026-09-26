"""Configuration is environment only. Secrets never live in the repo (requirement 6)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    mindb_addr: str = os.environ.get("MINDB_ADDR", "127.0.0.1:50051")
    corpus_dir: Path = Path(os.environ.get("CORPUS_DIR", "data"))
    top_k: int = int(os.environ.get("TOP_K", "5"))
    # Blends mood-query similarity with seed-game similarity when both are given (D10).
    mood_weight: float = float(os.environ.get("MOOD_WEIGHT", "0.6"))
    review_floor: int = int(os.environ.get("REVIEW_FLOOR", "50"))
    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
    groq_model: str = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

    # Ingest pacing. 35/min against Steam's ~40/min refill is the measured default from D17 --
    # deliberately under the ceiling, because the penalty for guessing high is a 429 that costs a
    # 20-30s backoff and makes the whole run slower than pacing correctly would have.
    steam_requests_per_min: float = float(os.environ.get("STEAM_REQUESTS_PER_MIN", "35"))
    steam_burst: int = int(os.environ.get("STEAM_BURST", "5"))
    # New appids come first; refreshes of games already in the corpus are capped so a big Steam
    # change day cannot starve them out (D15).
    max_updates_per_run: int = int(os.environ.get("MAX_UPDATES_PER_RUN", "500"))
    # Snapshot cadence bounds how much work a crash destroys (D7).
    snapshot_every_batches: int = int(os.environ.get("SNAPSHOT_EVERY_BATCHES", "10"))

    @property
    def documents_path(self) -> Path:
        return self.corpus_dir / "documents.jsonl"

    @property
    def names_path(self) -> Path:
        return self.corpus_dir / "names.json"

    @property
    def checkpoint_path(self) -> Path:
        return self.corpus_dir / "checkpoint.json"
