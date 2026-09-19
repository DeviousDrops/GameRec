"""Embedding model and the asymmetric query/document split (D3, ADR-0001)."""

from __future__ import annotations

import numpy as np
from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384

# BGE is trained asymmetrically: queries get an instruction prefix, documents do not.
#
# Do not replace this with fastembed's TextEmbedding.query_embed(). For this model it is a plain
# alias for embed() and applies no prefix at all -- verified, cos(query_embed, embed) == 1.0, while
# cos(embed, embed(PREFIX + text)) == 0.946. Dropping the prefix is a silent accuracy regression,
# which is why tests/test_query_prefix.py asserts the neighbours actually differ.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Written into every payload. An ingest run refuses to touch a corpus stamped differently (D3).
MODEL_STAMP = f"{MODEL_NAME}/{DIMS}"


class Embedder:
    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self._model = TextEmbedding(model_name)
        self.model_stamp = f"{model_name}/{DIMS}"

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        return [v.astype(np.float32) for v in self._model.embed(texts)]

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([QUERY_PREFIX + text])[0]

    def embed_query_without_prefix(self, text: str) -> np.ndarray:
        """Only for the test that proves the prefix changes the result."""
        return self.embed_documents([text])[0]
