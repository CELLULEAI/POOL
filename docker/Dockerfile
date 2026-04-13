# Cellule.ai Pool — multi-stage Docker image
# Usage:
#   docker run -d --name iamine-pool -p 8080:8080 #     -e POOL_NAME=my-pool -e POOL_URL=http://my-public-url:8080 #     -e DB_HOST=iamine-pg -e DB_PASS=strong_pg_pass #     celluleai/pool:0.2.58
#
# Recommended : use docker-compose.yml which spins up postgres pgvector
# alongside automatically.

# ============================================================
# Stage 1 : builder — compile llama-cpp-python + install deps
# ============================================================
FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends     build-essential     cmake     git     curl     ca-certificates  && rm -rf /var/lib/apt/lists/*

# Install iamine-ai + deps into a virtualenv we can copy as a whole
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# CPU-only llama-cpp-python — no GPU flags
RUN pip install --no-cache-dir     --index-url https://cellule.ai/pypi     --extra-index-url https://pypi.org/simple     iamine-ai==0.2.67

# ============================================================
# Stage 2 : runtime — slim image with just the venv + sys deps
# ============================================================
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends     curl     postgresql-client     libgomp1     ca-certificates  && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /opt/iamine
COPY docker-entrypoint.sh /opt/iamine/docker-entrypoint.sh
RUN chmod +x /opt/iamine/docker-entrypoint.sh

VOLUME /root/.iamine/federation

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3     CMD curl -fsS http://127.0.0.1:8080/v1/status || exit 1

ENTRYPOINT ["/opt/iamine/docker-entrypoint.sh"]
