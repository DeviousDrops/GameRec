# 384-dimensional asymmetric embeddings

The vector store allocates `capacity × dims × 4` bytes eagerly at boot, so embedding dimension is a
hard memory commitment rather than a tuning knob. We use `bge-small-en-v1.5` at 384 dimensions,
running in the API process, instead of a 768-dimensional model: it halves the store's footprint and
roughly halves scan time on a single VM, at a retrieval-quality cost we judged small for mood-style
queries.

## Consequences

BGE is **asymmetric**. Queries must carry the prefix `Represent this sentence for searching relevant
passages:` and Game Documents must not. Getting this wrong does not raise an error — it silently
degrades ranking, which is far worse than a crash. Any change to the model, its version, or the
dimension invalidates every stored vector and requires a full Reindex, which is why the Model Stamp
is written alongside each vector.
