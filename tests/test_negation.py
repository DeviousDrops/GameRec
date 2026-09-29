"""Negated queries: what gets cut out of the text, and what gets dropped from the results (D63).

The bug this suite exists for, reported against the live service: "a open world boss fight game which
is nothing like dark souls" returned four Dark Souls titles in its top five. A bi-encoder embeds
"nothing like dark souls" and "exactly like dark souls" to nearly the same point, so the fix cannot
be a better query -- the negation has to leave the text before the model sees it.

Two layers, tested separately: `gamerec.negation.split` is pure text, and the exclusion in
api.main._negated_out is pure geometry. The last three run the real model over real store text,
because both layers can be correct while the retrieval they add up to is still wrong.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from api import main
from gamerec import negation
from gamerec.documents import GameDocument
from gamerec.embeddings import Embedder
from gamerec.mindb import Hit
from gamerec.names import NameIndex

SPLITS = [
    # The reported query, and the orders a negated clause shows up in.
    ("a open world boss fight game which is nothing like dark souls",
     "a open world boss fight game", "dark souls"),
    ("an open world game, nothing like dark souls", "an open world game", "dark souls"),
    ("unlike dark souls, something cozy", "something cozy", "dark souls"),
    ("a soulslike game but not dark souls", "a soulslike game", "dark souls"),
    ("a relaxing game without combat", "a relaxing game", "combat"),
    ("a puzzle game instead of a shooter", "a puzzle game", "shooter"),
    ("something like hades except for the difficulty", "something like hades", "the difficulty"),
    # Nothing to split on.
    ("open world rpg", "open world rpg", None),
    ("", "", None),
]


@pytest.mark.parametrize("query,wanted,negated", SPLITS)
def test_the_negated_clause_is_cut_out_of_the_query(query, wanted, negated):
    assert negation.split(query) == negation.Split(wanted, negated)


@pytest.mark.parametrize("query", [
    # A bare "no" is not a cue, because these are the queries it would destroy.
    "no man's sky like games",
    "nothing",
    "cozy farming, no combat",
    # Cutting these leaves nothing to search for, and searching for nothing is worse than ignoring
    # the negation.
    "without combat",
    "anything but a shooter",
])
def test_a_split_that_would_leave_nothing_to_search_for_is_not_a_split(query):
    assert negation.split(query) == negation.Split(query, None)


def test_the_longest_cue_wins_and_leaves_no_filler_behind():
    """"which is nothing like" must beat "nothing like", or "which is" ends up in the query."""
    assert negation.split("a game which is nothing like x").wanted == "a game"


class _Embedder:
    """Two orthogonal directions, so "closer to the negated span" is exact rather than approximate."""

    WANTED = np.eye(384, dtype=np.float32)[0]
    NEGATED = np.eye(384, dtype=np.float32)[1]

    def embed_query(self, text: str) -> np.ndarray:
        return self.NEGATED if text == "dark souls" else self.WANTED


class _MinDB:
    """Records what it was asked for: the no-negation path must not grow a second round trip."""

    def __init__(self, vectors: dict[str, np.ndarray]) -> None:
        self.vectors = vectors
        self.searched_for: list[int] = []
        self.got: list[list[str]] = []

    def search(self, query, top_k):
        self.searched_for.append(top_k)
        hits = [Hit(i, 0.7, b'{"appid": 1}') for i in self.vectors]
        return hits[:top_k]

    def get(self, ids):
        self.got.append(list(ids))
        return {i: self.vectors[i] for i in ids if i in self.vectors}


def _mix(wanted: float, negated: float) -> np.ndarray:
    vector = wanted * _Embedder.WANTED + negated * _Embedder.NEGATED
    return (vector / np.linalg.norm(vector)).astype(np.float32)


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "config", dataclasses.replace(main.config, corpus_dir=tmp_path))
    main.state.clear()
    main.state.update({"embedder": _Embedder(), "names": NameIndex([])})
    return main.state


def test_a_result_closer_to_the_negated_span_is_dropped(api):
    api["mindb"] = _MinDB({
        "appid:1": _mix(0.2, 0.9),   # mostly the thing the user ruled out
        "appid:2": _mix(0.9, 0.2),   # mostly what they asked for
    })
    response = main.recommend(q="an open world game, nothing like dark souls", seed=None, k=2,
                              narrate_results=False)
    assert [r["name"] for r in response["results"]] == ["appid:2"]


def test_nothing_is_dropped_and_nothing_is_refetched_without_a_negation(api):
    mindb = api["mindb"] = _MinDB({"appid:1": _mix(0.2, 0.9), "appid:2": _mix(0.9, 0.2)})
    response = main.recommend(q="an open world game", seed=None, k=2, narrate_results=False)
    assert [r["name"] for r in response["results"]] == ["appid:1", "appid:2"]
    assert mindb.got == [], "a query without a negation must not cost an extra round trip"
    assert mindb.searched_for == [2], "a query without a negation must not over-fetch"


def test_a_negated_query_over_fetches_so_a_filtered_page_still_fills(api):
    mindb = api["mindb"] = _MinDB({f"appid:{i}": _mix(0.9, 0.2) for i in range(20)})
    main.recommend(q="an open world game, nothing like dark souls", seed=None, k=5,
                   narrate_results=False)
    assert mindb.searched_for == [5 * main.NEGATION_HEADROOM]


def test_the_response_says_what_the_negation_was_read_as(api):
    api["mindb"] = _MinDB({"appid:1": _mix(0.9, 0.2)})
    negated = main.recommend(q="an open world game, nothing like dark souls", seed=None, k=1,
                             narrate_results=False)
    plain = main.recommend(q="an open world game", seed=None, k=1, narrate_results=False)
    assert negated["negated"] == "dark souls"
    assert plain["negated"] is None


def test_a_hit_deleted_between_the_search_and_the_fetch_is_kept(api):
    """Being wrong by showing one game too many beats being wrong by showing none."""
    mindb = api["mindb"] = _MinDB({"appid:1": _mix(0.2, 0.9)})
    mindb.get = lambda ids: {}
    response = main.recommend(q="an open world game, nothing like dark souls", seed=None, k=1,
                              narrate_results=False)
    assert [r["name"] for r in response["results"]] == ["appid:1"]


# The real thing: real model, real store text, exact cosine -- which is what MinDB computes.
CORPUS = [
    GameDocument(1, "DARK SOULS III", genres=["Action", "RPG"], short_description=(
        "Dark Souls III. Punishing boss battles and intricate level design in a decaying world.")),
    GameDocument(2, "DARK SOULS II: Scholar of the First Sin", genres=["Action", "RPG"],
                 short_description="Dark Souls II. Relentless bosses, cursed kingdoms, hard deaths."),
    GameDocument(3, "Monster Hunter: World", genres=["Action", "RPG"], short_description=(
        "Hunt colossal monsters in a living open world with friends, and forge gear from what "
        "you bring down.")),
    GameDocument(4, "Far Cry 5", genres=["Action", "Adventure"], short_description=(
        "Fight for hire across an open world county, taking on cult bosses however you like.")),
    GameDocument(5, "Stardew Valley", genres=["Simulation", "RPG"], short_description=(
        "Build the farm of your dreams at your own pace. Raise animals, grow crops, make friends.")),
]


@pytest.fixture(scope="module")
def corpus():
    embedder = Embedder()
    return embedder, np.vstack(embedder.embed_documents([d.render() for d in CORPUS]))


def _ranked(corpus, query: str) -> list[str]:
    """The query path, with numpy standing in for MinDB's exact kNN."""
    embedder, matrix = corpus
    parsed = negation.split(query)
    wanted = embedder.embed_query(parsed.wanted)
    negated = embedder.embed_query(parsed.negated) if parsed.negated else None
    keep = []
    for i in np.argsort(-(matrix @ wanted)):
        if negated is not None and matrix[i] @ negated > matrix[i] @ wanted:
            continue
        keep.append(CORPUS[i].name)
    return keep


REPORTED = "a open world boss fight game which is nothing like dark souls"


def test_dark_souls_is_a_good_answer_to_the_query_with_its_negation_removed(corpus):
    """Why the bug looked like working search, and why the suite would pass for the wrong reason
    if this ever stopped being true."""
    assert "DARK SOULS III" in _ranked(corpus, negation.split(REPORTED).wanted)[:3]


def test_the_reported_query_no_longer_returns_the_game_it_ruled_out(corpus):
    assert not any("DARK SOULS" in name for name in _ranked(corpus, REPORTED)[:3])


def test_a_query_whose_wanted_half_implies_the_negated_half_still_excludes_it(corpus):
    """The case cutting the clause cannot fix on its own: what is left means the same thing."""
    ranked = _ranked(corpus, "a soulslike game but not dark souls")
    assert not any("DARK SOULS" in name for name in ranked[:2])


def test_a_broad_negated_concept_separates_the_corpus_without_emptying_the_page(corpus):
    """The negated span does not have to be a title. "combat" is a concept no document is named
    after, and the rule still sorts the corpus by it: the farming game survives, the boss-fighting
    ones are dropped, and something is left to show."""
    ranked = _ranked(corpus, "a relaxing farming game without combat")
    assert ranked[0] == "Stardew Valley"
    assert not any("DARK SOULS" in name for name in ranked)
    assert len(ranked) >= 2, "a broad negation must not drop every game that contains a verb"
