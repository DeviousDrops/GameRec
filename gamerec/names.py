"""The Name Index: lexical seed-game resolution (D11, ADR-0004).

Semantic search is the wrong tool for "the game called Portal" -- embeddings happily return games that
are *like* Portal when the user typed its name. So names are resolved lexically, against every appid
Steam knows about rather than only what made it into the corpus. That is what lets a miss say why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import re

from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

IN_CORPUS = "in_corpus"
FILTERED_LOW_REVIEWS = "filtered_low_reviews"
NOT_A_GAME = "not_a_game"
PENDING_INGEST = "pending_ingest"

MATCH_THRESHOLD = 90

# rapidfuzz does no normalisation unless asked. Without this, "stardew valley" scores 85.7 against
# "Stardew Valley" and "half life 2" scores 72.7 against "Half-Life 2" -- both rejected by the 90
# threshold purely over case and punctuation. With it, both are 100. The threshold is meant to
# measure how different the words are, not how the user capitalised them.
#
# Known limitation: a long subtitle still drags the score down ("DARK SOULS 2" against "DARK SOULS
# II: Scholar of the First Sin" is 86.1), so those need the appid. Lowering the threshold is not the
# fix -- it would start matching genuinely different games.
_PROCESSOR = default_process

# Sequel markers, in digits or roman numerals. A fuzzy scorer treats these as near-noise -- "Half-Life
# 3" scores 90+ against "Half-Life", so asking for a game that does not exist would quietly return a
# different one. In game titles the number is the most load-bearing token there is, so it is compared
# exactly and the fuzzy score only decides the rest.
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9",
          "x": "10", "xi": "11", "xii": "12", "xiii": "13"}


def _sequel_markers(title: str) -> frozenset[str]:
    markers = set()
    for token in re.findall(r"[A-Za-z0-9]+", title.lower()):
        if token.isdigit():
            markers.add(token.lstrip("0") or "0")
        elif token in _ROMAN:
            markers.add(_ROMAN[token])
    return frozenset(markers)

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

    @staticmethod
    def merge(path: Path, entries: list[NameEntry]) -> int:
        """Fold a run's entries into the file on disk, newest winning. Returns the resulting size.

        A resumable fill (D14) sees a slice of the catalogue per run, so writing only what this run
        saw would shrink the index every time -- and the index is what lets a miss explain itself for
        games that are *not* in the corpus. Newest wins because a status can legitimately change: a
        game that was pending_ingest becomes in_corpus, and one that leaves early access can start
        failing the review floor.
        """
        path = Path(path)
        merged: dict[int, NameEntry] = {}
        try:
            for raw in json.loads(path.read_text(encoding="utf-8")):
                merged[int(raw["appid"])] = NameEntry(**raw)
        except FileNotFoundError:
            pass
        for entry in entries:
            merged[entry.appid] = entry
        NameIndex.dump(list(merged.values()), path)
        return len(merged)

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
            wanted = _sequel_markers(seed)
            candidates = process.extract(
                seed,
                self._choices,
                scorer=fuzz.WRatio,
                processor=_PROCESSOR,
                score_cutoff=MATCH_THRESHOLD,
                limit=10,
            )
            match = next((c for c in candidates if _sequel_markers(c[0]) == wanted), None)
            if match is None:
                if candidates:
                    near = self._by_appid[candidates[0][2]].name
                    return Resolution(
                        None, f"no game named {seed!r} -- the closest is {near}, which is not the same game"
                    )
                return Resolution(None, f"no game named {seed!r} (needs a {MATCH_THRESHOLD}% match)")
            entry = self._by_appid[match[2]]

        if entry.status == IN_CORPUS:
            return Resolution(entry)
        return Resolution(entry, f"{entry.name} is known, but {_EXPLANATIONS[entry.status]}")
