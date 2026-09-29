"""Splitting a query into what the user wants and what they want kept away (D63).

A bi-encoder has no representation for "not". "an open world boss fight game which is nothing like
dark souls" and "... which is exactly like dark souls" embed to almost the same point, because the
tokens that carry the most weight -- dark, souls -- are identical and the negation is a function
word. Measured against the corpus, the first query returned four Dark Souls titles in its top five.

So the negation is handled before the model sees the text: the clause is cut off the query, and the
part after the cue becomes a direction to push results away from. Cutting the clause is the change
that does the work; see D63 for the numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Only comparative negations, and only ones that cannot plausibly be part of a game's title.
#
# A bare "no" is deliberately absent: "no man's sky like games" is a real query and cutting it at
# "no" would leave nothing to search for. "nothing" alone is absent for the same reason. Each cue
# here needs a following span to mean anything, which is what makes it safe to split on.
CUES = (
    "that is nothing like",
    "which is nothing like",
    "that isn't like",
    "which isn't like",
    "that is not like",
    "which is not like",
    "nothing like",
    "not similar to",
    "dissimilar to",
    "anything but",
    "as long as it isn't",
    "as long as it is not",
    "instead of",
    "other than",
    "isn't like",
    "is not like",
    "but not",
    "just not",
    "not like",
    "unlike",
    "without",
    "except for",
    "except",
    "minus",
)

# Longest first, so "which is nothing like" wins over "nothing like" and the leftover "which is"
# never ends up in the positive span.
_CUE_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(CUES, key=len, reverse=True))
                     + r")\b", re.IGNORECASE)

# What a cue tends to leave dangling on the end of the positive span once the clause is cut.
_TRAILING = re.compile(r"(?:\b(?:which|that|that's|and|but|is|are|it|one)\b|[,;:]|\s)+$",
                       re.IGNORECASE)
_LEADING = re.compile(r"^(?:\b(?:and|but|a|an)\b|[,;:]|\s)+", re.IGNORECASE)


@dataclass(frozen=True)
class Split:
    """`wanted` is what gets embedded as the query; `negated` is what results are pushed away from.

    `negated` is None for the overwhelming majority of queries, and that case must behave exactly as
    it did before any of this existed -- one embed, one search, no extra round trip.
    """

    wanted: str
    negated: str | None = None


def split(query: str) -> Split:
    """Cut the first negated clause out of `query`.

    The clause runs from the cue to the first comma or to the end of the string, whichever comes
    first, so both orders read correctly:

        "an open world game, nothing like dark souls" -> ("an open world game", "dark souls")
        "unlike dark souls, something cozy"           -> ("something cozy", "dark souls")

    A split that would leave either side empty is not a split: the query is returned whole. "games
    without a single bug" has nothing to search for once "without a single bug" is removed, and
    searching for nothing is worse than ignoring the negation.
    """
    match = _CUE_RE.search(query)
    if not match:
        return Split(query)
    before, after = query[:match.start()], query[match.end():]
    tail = ""
    if "," in after:
        after, tail = after.split(",", 1)
    negated = _LEADING.sub("", _TRAILING.sub("", after)).strip()
    wanted = _TRAILING.sub("", before).strip()
    wanted = (wanted + " " + _LEADING.sub("", tail).strip()).strip()
    if not negated or not wanted:
        return Split(query)
    return Split(wanted, negated)
