"""The Game Document Store: append-only JSONL, latest-wins per appid (D9).

This is the source of truth (D13), so the one thing it must never do is lose a document. Phase 1
rewrote the whole file on every run, which meant an interrupted run -- or a run with a smaller
--limit -- truncated the corpus it was supposed to be extending. Appending fixes that: a document is
only ever added, and a later fetch of the same appid shadows the earlier one rather than replacing it
in place.

Reading therefore has to resolve duplicates, and the rule is latest-wins by file order:

    appid:413150  template v1  <- superseded
    appid:413150  template v2  <- wins, last line for that appid

Compaction drops the shadowed lines. It is not required for correctness -- `load` resolves them
either way -- so it runs when the file has grown enough to be worth rewriting, and never in the
middle of a run.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from gamerec.documents import GameDocument

log = logging.getLogger(__name__)


class CorpusStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, documents: list[GameDocument]) -> None:
        """One open per batch, flushed before returning: a crash loses at most the current batch,
        and the checkpoint for that batch has not been written yet either."""
        if not documents:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for document in documents:
                handle.write(document.to_jsonl() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load(self) -> list[GameDocument]:
        """Latest-wins per appid, in first-seen order so the corpus keeps its popularity ordering."""
        by_appid: dict[int, GameDocument] = {}
        for lineno, line in enumerate(self._lines(), start=1):
            try:
                document = GameDocument.from_jsonl(line)
            except (json.JSONDecodeError, TypeError) as exc:
                # A bad line is a bug somewhere upstream, but refusing to open the corpus over one
                # of them would turn a lost document into a lost service. Loud and skipped.
                log.warning("%s:%d is not a Game Document, skipping: %s", self.path, lineno, exc)
                continue
            by_appid[document.appid] = document
        return list(by_appid.values())

    def appids(self) -> set[int]:
        """Cheaper than `load` when all that is wanted is what is already there."""
        found = set()
        for line in self._lines():
            try:
                found.add(int(json.loads(line)["appid"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return found

    def line_count(self) -> int:
        return sum(1 for _ in self._lines())

    def compact(self) -> int:
        """Rewrite with one line per appid. Returns how many lines went away.

        tmp + rename, because the alternative -- truncating and rewriting in place -- has a window
        where the source of truth is a partial file. That window is exactly what D13 forbids.
        """
        documents = self.load()
        before = self.line_count()
        if before == len(documents):
            return 0
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for document in documents:
                handle.write(document.to_jsonl() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        log.info("compacted %s: %d lines -> %d", self.path, before, len(documents))
        return before - len(documents)

    def _lines(self):
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield line
        except FileNotFoundError:
            return
