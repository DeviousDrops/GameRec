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

    @property
    def documents_path(self) -> Path:
        return self.corpus_dir / "documents.jsonl"

    @property
    def names_path(self) -> Path:
        return self.corpus_dir / "names.json"
