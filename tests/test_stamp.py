"""The guard that stops two embedding models sharing one index (D3, D9, D22).

The failure this prevents is silent. Vectors from a different model still have 384 dimensions and
still produce a cosine score between -1 and 1, so nothing errors -- the recommendations just quietly
stop meaning anything. A refusal at ingest time is the only place it can be caught cheaply.
"""

from __future__ import annotations

import logging

import pytest

from gamerec.stamp import Stamp, StampMismatch, assert_ingestable

BGE = Stamp("BAAI/bge-small-en-v1.5/384", 1)


def test_a_first_run_has_nothing_to_disagree_with():
    assert_ingestable(BGE, None)


def test_the_same_stamp_passes():
    assert_ingestable(BGE, Stamp("BAAI/bge-small-en-v1.5/384", 1))


def test_a_different_model_refuses_and_says_what_to_run():
    other = Stamp("intfloat/e5-small-v2/384", 1)

    with pytest.raises(StampMismatch) as raised:
        assert_ingestable(other, BGE)

    message = str(raised.value)
    assert "BAAI/bge-small-en-v1.5/384" in message and "intfloat/e5-small-v2/384" in message
    # A refusal that does not name the way out just moves the problem to whoever is on call.
    assert "ingest.reindex" in message


def test_the_same_model_at_a_different_dimension_still_refuses():
    """The stamp carries dims for a reason: 384 and 768 vectors of the same family are no more
    comparable than two different models."""
    with pytest.raises(StampMismatch):
        assert_ingestable(Stamp("BAAI/bge-small-en-v1.5/768", 1), BGE)


def test_a_changed_template_warns_but_does_not_refuse(caplog):
    """D22 expects a mixed-version corpus while a fill catches up -- same model, same space, just
    more or less text. Refusing here would block the v2 rollout it plans for."""
    with caplog.at_level(logging.WARNING):
        assert_ingestable(Stamp("BAAI/bge-small-en-v1.5/384", 2), BGE)

    assert "template v1" in caplog.text and "v2" in caplog.text


def test_a_model_change_wins_over_a_template_change(caplog):
    """Both changed at once is still a refusal, not a warning."""
    with pytest.raises(StampMismatch):
        assert_ingestable(Stamp("other/384", 2), BGE)
