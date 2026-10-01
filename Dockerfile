FROM python:3.12-slim

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV FASTEMBED_CACHE_PATH=/app/.fastembed_cache \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    ENGRAM_DATA_ROOT=/tmp/engram-data \
    PORT=7860 \
    PYTHONUNBUFFERED=1

COPY --chown=user . .
# Bake the embedding model into the image so the first request is not a 130 MB download.
RUN python -c "from engram.embeddings import embed_dense; embed_dense('warmup')" && chown -R user /app

USER user
EXPOSE 7860
CMD ["sh", "-c", "python -m uvicorn engram.gateway:app --host 0.0.0.0 --port ${PORT:-7860}"]
