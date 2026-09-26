"""Model Stamp and Template Version guards (D3, D9, D22).

Two things can make a stored vector wrong, and they are not equally bad.

**A changed model is fatal.** Cosine distance between vectors from two different models is not a
smaller similarity, it is a meaningless number -- so a corpus holding both does not degrade, it lies.
An ingest that finds a different stamp refuses to add to it and says what to run instead. This is
cheap to recover from precisely because D9 stored the documents: a Reindex re-embeds from JSONL and
never touches Steam.

**A changed template is tolerated.** D22 plans for exactly this: v1 is metadata-only, v2 adds review
phrases, and the corpus is expected to hold both while the fill catches up. The vectors are still
comparable -- same model, same space -- just built from more or less text, so the mix is reported per
result by /recommend rather than being refused. A Reindex is how it gets levelled when that is worth
the compute, not a precondition for ingesting.

The asymmetry is the whole point of keeping both fields. One is an error, the other is a note.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


class StampMismatch(RuntimeError):
    """Raised when continuing would mix incomparable vectors. Never caught to carry on regardless."""


@dataclass(frozen=True)
class Stamp:
    model_stamp: str
    template_version: int

    def __str__(self) -> str:
        return f"{self.model_stamp} template v{self.template_version}"


def assert_ingestable(current: Stamp, recorded: Stamp | None) -> None:
    """Called before an ingest writes anything. `recorded is None` is a first run, not a mismatch."""
    if recorded is None:
        log.info("no previous stamp; starting a corpus at %s", current)
        return

    if recorded.model_stamp != current.model_stamp:
        raise StampMismatch(
            f"corpus was embedded with {recorded.model_stamp}, this process has "
            f"{current.model_stamp}. Vectors from two models are not comparable, so ingest would "
            f"corrupt the index rather than extend it. Re-embed the stored documents instead:\n"
            f"    python -m ingest.reindex\n"
            f"That needs no Steam requests -- the documents are already in the corpus (D9)."
        )

    if recorded.template_version != current.template_version:
        # Expected by D22, so this is deliberately not an error.
        log.warning(
            "corpus holds template v%d documents, this process renders v%d. Both are in the same "
            "vector space, so the mix is reported per result rather than refused; run "
            "`python -m ingest.reindex` to level it.",
            recorded.template_version,
            current.template_version,
        )
