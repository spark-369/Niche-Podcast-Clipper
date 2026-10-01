FROM python:3.11-slim

# OpenCV headless needs these shared libraries at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Local model cache and media storage live on mounted volumes.
# AI_NUM_THREADS=0 keeps the per-process auto-budget (physical cores); override
# per service in docker-compose to split a big host across the API + workers.
ENV HF_HOME=/app/models \
    STORAGE_DIR=/app/media_storage \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    DEBUG=false \
    ENABLE_DOCS=false \
    AI_NUM_THREADS=0

RUN mkdir -p /app/models /app/media_storage \
    && useradd --system --uid 10001 --no-create-home appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/ping || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
