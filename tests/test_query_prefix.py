"""Guard rail: the BGE query prefix must actually change retrieval.

BGE is asymmetric -- queries carry an instruction prefix, documents do not. Dropping the prefix is a
silent accuracy regression: everything still works, results just get quietly worse. This test makes
that failure loud.

It is not hypothetical. fastembed ships a `query_embed()` that looks like it applies the prefix and,
for this model, does not: cos(query_embed(t), embed(t)) == 1.0. gamerec.embeddings therefore applies
the prefix itself, and this test is what stops anyone "simplifying" that away.
"""

from __future__ import annotations

import numpy as np
import pytest

from gamerec.embeddings import DIMS, QUERY_PREFIX, Embedder

QUERIES = [
    "something relaxing to unwind with after work",
    "a punishing game that respects my time",
    "co-op horror with friends",
]

CORPUS = [
    "Stardew Valley. A farming game. Build the farm of your dreams at your own pace.",
    "Dark Souls III. An action role-playing game. Punishing combat and intricate level design.",
    "Phasmophobia. A horror game. Four-player online co-op ghost hunting.",
    "Factorio. A simulation game. Build and automate sprawling factories.",
    "Civilization VI. A strategy game. Build an empire to stand the test of time.",
]


@pytest.fixture(scope="module")
def embedder():
    return Embedder()


@pytest.fixture(scope="module")
def corpus(embedder):
    return np.vstack(embedder.embed_documents(CORPUS))


def test_embeddings_are_normalised(embedder):
    vector = embedder.embed_query(QUERIES[0])
    assert vector.shape == (DIMS,)
    # MinDB scores by cosine; unit vectors keep score and dot product interchangeable.
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-4)


def test_prefix_changes_the_query_vector(embedder):
    with_prefix = embedder.embed_query(QUERIES[0])
    without = embedder.embed_query_without_prefix(QUERIES[0])
    similarity = float(with_prefix @ without)
    assert similarity < 0.99, (
        f"prefixed and unprefixed queries are {similarity:.4f} similar -- the prefix is not being "
        f"applied. Check that embed_query() still prepends QUERY_PREFIX."
    )


def test_prefix_is_exactly_the_documented_string(embedder):
    """Pins the prefix itself: a typo in it would pass the test above while still being wrong."""
    manual = embedder.embed_documents([QUERY_PREFIX + QUERIES[0]])[0]
    assert float(manual @ embedder.embed_query(QUERIES[0])) == pytest.approx(1.0, abs=1e-5)


@pytest.mark.parametrize("query", QUERIES)
def test_prefix_materially_changes_the_scores(embedder, corpus, query):
    """The prefix has to move the numbers, not just the vector."""
    prefixed = corpus @ embedder.embed_query(query)
    plain = corpus @ embedder.embed_query_without_prefix(query)
    assert np.max(np.abs(prefixed - plain)) > 0.01, (
        f"prefix changed no score by more than 0.01 for {query!r}"
    )


def test_prefixed_queries_retrieve_the_intended_game(embedder, corpus):
    """The prefix should not merely differ -- it should be the version that works."""
    for query, expected in zip(QUERIES, [0, 1, 2]):
        ranked = int(np.argmax(corpus @ embedder.embed_query(query)))
        assert ranked == expected, f"{query!r} retrieved {CORPUS[ranked].split('.')[0]}"
