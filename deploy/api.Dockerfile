# One image for the whole service: the API serves from it, and the initial fill and the nightly
# ingest run `python -m ingest.run` out of the same layers. Two images would be two chances for
# them to disagree about the embedding model or the render template, which is the disagreement
# nothing downstream can detect (D31).
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/DeviousDrops/GameRec"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gamerec/ gamerec/
COPY api/ api/
COPY ingest/ ingest/
# The backup and restore entrypoints. Same image again: they have to agree with the ingest about
# where the checkpoint lives and what a complete one means (D37).
COPY ops/ ops/
COPY clients/generated/ clients/generated/

# Bake the model into the image. Downloading it at startup would make a cold pod wait on
# huggingface.co, which turns an unrelated outage into a GameRec outage.
#
# The path is explicit because fastembed's default is /tmp/fastembed_cache, and that is exactly the
# directory a hardened pod mounts an emptyDir over -- which would hide the baked model and send the
# process to huggingface.co after all. /opt/models is world-readable so the unprivileged runtime
# user can load it, and nothing writes there at runtime.
ENV FASTEMBED_CACHE_PATH=/opt/models
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" \
    && chmod -R a+rX /opt/models

RUN useradd -u 10001 -m gamerec
USER 10001
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
