"""The ONNX batch cap: the ingest's batch size must not set the model's memory footprint.

The first nightly ingest on the VM was OOM-killed embedding 200 documents in one ONNX run (D51), so
these assert the two properties that fix depends on: the cap is applied whatever the caller passes,
and applying it changes nothing about the vectors.
"""

from __future__ import annotations

import numpy as np

from gamerec.embeddings import DIMS, ONNX_BATCH, Embedder


class _Recording:
    """Stands in for fastembed's TextEmbedding, recording the batch sizes it was asked for."""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed(self, texts, batch_size=256, **kwargs):
        self.batch_sizes.append(batch_size)
        return [np.zeros(DIMS, dtype=np.float32) for _ in texts]


def _embedder_with(model) -> Embedder:
    """An Embedder around a stub. __init__ would download and load the real model."""
    embedder = Embedder.__new__(Embedder)
    embedder._model = model
    embedder.model_stamp = "stub/384"
    return embedder


def test_the_onnx_batch_is_capped_not_left_at_the_default():
    model = _Recording()
    _embedder_with(model).embed_documents(["a game about bees"] * 200)
    assert model.batch_sizes == [ONNX_BATCH]


def test_a_single_query_uses_the_same_path():
    model = _Recording()
    _embedder_with(model).embed_query("something relaxing")
    assert model.batch_sizes == [ONNX_BATCH]


def test_the_cap_is_small_enough_to_bound_an_ingest_batch():
    """200 is the ingest's batch size; a cap at or above it would not bound anything."""
    from ingest.run import BATCH_SIZE

    assert ONNX_BATCH < BATCH_SIZE


def test_chunking_does_not_change_the_vectors():
    """Real model, because the whole point is that ONNX gives the same answer either way.

    Measured on the VM across batch sizes 16, 32, 64 and 256: max elementwise difference 0.0. This
    asserts the weaker, machine-independent form of that -- a list longer than the cap embeds the
    same as its elements do one at a time.
    """
    embedder = Embedder()
    texts = [f"A cooperative puzzle game about {n} robots in a factory" for n in range(ONNX_BATCH + 5)]

    together = embedder.embed_documents(texts)
    separately = [embedder.embed_documents([text])[0] for text in texts]

    assert len(together) == len(texts)
    for chunked, alone in zip(together, separately):
        np.testing.assert_allclose(chunked, alone, atol=1e-6)
