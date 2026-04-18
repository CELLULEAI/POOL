# Cellule.ai Pool — multi-stage Docker image
# Version: 1.0.0 (first community-governed release)
# Usage:
#   docker run -d --name iamine-pool -p 8080:8080 \
#     -e POOL_NAME=my-pool -e POOL_URL=http://my-public-url:8080 \
#     -e DB_HOST=iamine-pg -e DB_PASS=strong_pg_pass \
#     celluleai/pool:1.0.0
#
# Release signing: this image embeds the signed wheel + sig + MAINTAINERS
# in /opt/cellule/. Pool verifies at boot and logs the result (warning-only
# unless IAMINE_STRICT_SIGNING=1). See docs/GOVERNANCE.md for the rationale.

# ============================================================
# Stage 1: builder — install from local signed wheel
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

# Install from locally-built signed wheel (built by scripts/build_release.sh)
COPY dist/iamine_ai-1.0.0-py3-none-any.whl /tmp/
RUN pip install --no-cache-dir \
    --extra-index-url https://pypi.org/simple \
    "/tmp/iamine_ai-1.0.0-py3-none-any.whl[pool]"

# Pre-download MiniLM-L6-v2 (~90 MB) — instant container boot, no internet at runtime
RUN mkdir -p /root/.cache/iamine-embedder && \
    python -c "from sentence_transformers import SentenceTransformer; \
m = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
m.save('/root/.cache/iamine-embedder')"

# ============================================================
# Stage 2: runtime — slim image with venv + embedder cache + signed release
# ============================================================
FROM python:3.12-slim-bookworm

# OCI image metadata labels (opencontainers.org spec)
LABEL org.opencontainers.image.title="CELLULE Pool"       org.opencontainers.image.description="Distributed AI inference network — pool orchestrator"       org.opencontainers.image.url="https://cellule.ai"       org.opencontainers.image.source="https://github.com/CELLULEAI/POOL"       org.opencontainers.image.version="1.0.0"       org.opencontainers.image.licenses="AGPL-3.0-or-later"       org.opencontainers.image.vendor="Cellule.ai / David Mourgues"       org.opencontainers.image.authors="David Mourgues <david.mourgues@gmail.com>"


RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    postgresql-client \
    libgomp1 \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /root/.cache/iamine-embedder /root/.cache/iamine-embedder
ENV PATH="/opt/venv/bin:$PATH"

# Release signing artifacts (verified at boot by iamine.core.release_signing)
RUN mkdir -p /opt/cellule
COPY dist/iamine_ai-1.0.0-py3-none-any.whl           /opt/cellule/
COPY dist/iamine_ai-1.0.0-py3-none-any.whl.sig       /opt/cellule/
COPY MAINTAINERS                                      /opt/cellule/
COPY LICENSE                                          /opt/cellule/LICENSE
COPY NOTICE                                           /opt/cellule/NOTICE

# Env vars consumed by iamine.core.release_signing.verify_release_at_boot
ENV IAMINE_MAINTAINERS=/opt/cellule/MAINTAINERS \
    IAMINE_RELEASE_SIG=/opt/cellule/iamine_ai-1.0.0-py3-none-any.whl.sig \
    IAMINE_RELEASE_ARTIFACT=/opt/cellule/iamine_ai-1.0.0-py3-none-any.whl

WORKDIR /opt/iamine
COPY docker-entrypoint.sh /opt/iamine/docker-entrypoint.sh
RUN chmod +x /opt/iamine/docker-entrypoint.sh

VOLUME /root/.iamine/federation

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/v1/status || exit 1

ENTRYPOINT ["/opt/iamine/docker-entrypoint.sh"]
