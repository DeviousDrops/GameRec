"""The Name Index: lexical seed-game resolution (D11, ADR-0004).

Semantic search is the wrong tool for "the game called Portal" -- embeddings happily return games that
are *like* Portal when the user typed its name. So names are resolved lexically, against every appid
Steam knows about rather than only what made it into the corpus. That is what lets a miss say why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

IN_CORPUS = "in_corpus"
FILTERED_LOW_REVIEWS = "filtered_low_reviews"
NOT_A_GAME = "not_a_game"
PENDING_INGEST = "pending_ingest"

MATCH_THRESHOLD = 90

_EXPLANATIONS = {
    FILTERED_LOW_REVIEWS: "it has too few reviews to be indexed",
    NOT_A_GAME: "it is not a game on Steam (DLC, a demo, a soundtrack or similar)",
    PENDING_INGEST: "it has not been ingested yet",
}


@dataclass
class NameEntry:
    appid: int
    name: str
    status: str


@dataclass
class Resolution:
    entry: NameEntry | None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.entry is not None and self.entry.status == IN_CORPUS


class NameIndex:
    def __init__(self, entries: list[NameEntry]) -> None:
        self._by_appid = {e.appid: e for e in entries}
        self._choices = {e.appid: e.name for e in entries}

    @classmethod
    def load(cls, path: Path) -> "NameIndex":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([NameEntry(**e) for e in raw])

    @staticmethod
    def dump(entries: list[NameEntry], path: Path) -> None:
        Path(path).write_text(
            json.dumps([e.__dict__ for e in entries], separators=(",", ":")), encoding="utf-8"
        )

    def __len__(self) -> int:
        return len(self._by_appid)

    def resolve(self, seed: str | int) -> Resolution:
        """An appid bypasses fuzzy matching entirely -- it is already unambiguous."""
        entry = None
        if isinstance(seed, int) or (isinstance(seed, str) and seed.isdigit()):
            entry = self._by_appid.get(int(seed))
            if entry is None:
                return Resolution(None, f"appid {seed} is not in the Steam catalogue")
        else:
            match = process.extractOne(
                seed, self._choices, scorer=fuzz.WRatio, score_cutoff=MATCH_THRESHOLD
            )
            if match is None:
                return Resolution(None, f"no game named {seed!r} (needs a {MATCH_THRESHOLD}% match)")
            entry = self._by_appid[match[2]]

        if entry.status == IN_CORPUS:
            return Resolution(entry)
        return Resolution(entry, f"{entry.name} is known, but {_EXPLANATIONS[entry.status]}")
