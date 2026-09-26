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
COPY clients/generated/ clients/generated/

# Bake the model into the image. Downloading it at startup would make a cold pod wait on
# huggingface.co, which turns an unrelated outage into a GameRec outage.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

RUN useradd -u 10001 -m gamerec
USER gamerec
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
