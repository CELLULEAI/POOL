# Cellule.ai Pool — multi-stage Docker image
# Version: 0.2.89 (Smart Routing Phase 3 KNN embedder pre-baked)
# Usage:
#   docker run -d --name iamine-pool -p 8080:8080 \
#     -e POOL_NAME=my-pool -e POOL_URL=http://my-public-url:8080 \
#     -e DB_HOST=iamine-pg -e DB_PASS=strong_pg_pass \
#     celluleai/pool:0.2.89
#
# Recommended: use docker-compose.yml which spins up postgres pgvector.

# ============================================================
# Stage 1: builder — install iamine-ai[pool] + pre-download embedder
# ============================================================
FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# torch CPU-only first (avoids heavy CUDA variant pulled by sentence-transformers)
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch

# iamine-ai + [pool] extras = sentence-transformers (Smart Routing Phase 3 KNN)
RUN pip install --no-cache-dir \
    --index-url https://cellule.ai/pypi \
    --extra-index-url https://pypi.org/simple \
    "iamine-ai[pool]==0.2.89"

# Pre-download MiniLM-L6-v2 (~90 MB) — instant container boot, no internet at runtime
RUN mkdir -p /root/.cache/iamine-embedder && \
    python -c "from sentence_transformers import SentenceTransformer; \
m = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
m.save('/root/.cache/iamine-embedder')"

# ============================================================
# Stage 2: runtime — slim image with venv + embedder cache
# ============================================================
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    postgresql-client \
    libgomp1 \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /root/.cache/iamine-embedder /root/.cache/iamine-embedder
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /opt/iamine
COPY docker-entrypoint.sh /opt/iamine/docker-entrypoint.sh
RUN chmod +x /opt/iamine/docker-entrypoint.sh

VOLUME /root/.iamine/federation

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/v1/status || exit 1

ENTRYPOINT ["/opt/iamine/docker-entrypoint.sh"]
