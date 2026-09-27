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

# How many documents go into one ONNX run, regardless of how many the caller hands over.
#
# fastembed's default is 256, and it pads every text in a run to the longest one in that run, so the
# cost is batch x longest sequence rather than the sum of the actual lengths. Measured on the VM
# (2 vCPU, 4 GiB) against 200 real rendered documents, with the model already resident at ~290Mi:
#
#     onnx batch     peak       throughput
#     16             364Mi      14.1 docs/s
#     32             376Mi      13.2 docs/s
#     64             487Mi      12.3 docs/s
#     256 (default) 1202Mi      11.3 docs/s
#
# The default is how the first nightly ingest died: 1179Mi against a 1Gi limit, OOM-killed while
# embedding its first batch, having already written the documents and the name index (D51). Capping
# it is not a trade -- the padding it avoids makes it faster too, and the vectors are bit-identical
# across every batch size above, so nothing about the corpus depends on this number.
ONNX_BATCH = 32


class Embedder:
    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self._model = TextEmbedding(model_name)
        self.model_stamp = f"{model_name}/{DIMS}"

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        """Chunked at ONNX_BATCH, so peak memory is set here rather than by the size of `texts`.

        Callers batch for their own reasons -- the ingest's batch is the unit of durable progress,
        ~200 documents between a corpus append and a checkpoint -- and that unit should not double as
        a memory budget for the model.
        """
        return [v.astype(np.float32) for v in self._model.embed(texts, batch_size=ONNX_BATCH)]

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([QUERY_PREFIX + text])[0]

    def embed_query_without_prefix(self, text: str) -> np.ndarray:
        """Only for the test that proves the prefix changes the result."""
        return self.embed_documents([text])[0]
