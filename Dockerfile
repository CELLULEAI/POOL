# ============================================================================
# Dockerfile SIMPLIFIE (fly.io / render / quickstart). NON SUPPORTE pour la prod.
# Il manque : pgvector (smart-routing KNN), la verification de signature de
# release et l'extra [pool]. Il requiert un Postgres pgvector EXTERNE.
# Pour un pool de production federe, utiliser docker/Dockerfile (image
# celluleai/pool : wheel signe + pgvector + migrations). Audit 2026-06-22.
# ============================================================================
FROM python:3.12-slim

WORKDIR /app

# Dépendances système pour llama-cpp-python
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY iamine/ iamine/

# Port exposé
EXPOSE 8080

# Lancer le pool
CMD ["python", "-m", "iamine", "pool", "--host", "0.0.0.0", "--port", "8080"]
