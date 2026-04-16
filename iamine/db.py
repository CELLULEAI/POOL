"""Couche d'abstraction base de donnees — RAM pour le MVP, PostgreSQL pour la prod.

Pour migrer vers PostgreSQL :
1. pip install asyncpg
2. Creer la base avec le schema ci-dessous
3. Remplacer MemoryStore par PostgresStore dans pool.py
4. Configurer DATABASE_URL dans le config ou les variables d'environnement

Schema PostgreSQL :
--------------------
CREATE TABLE workers (
    worker_id       VARCHAR(64) PRIMARY KEY,
    machine_hash    VARCHAR(64) NOT NULL,
    hostname        VARCHAR(128),
    cpu             VARCHAR(128),
    cpu_threads     INTEGER,
    ram_total_gb    REAL,
    model_path      VARCHAR(256),
    first_seen      TIMESTAMP DEFAULT NOW(),
    last_seen       TIMESTAMP DEFAULT NOW(),
    is_online       BOOLEAN DEFAULT FALSE,
    total_jobs      INTEGER DEFAULT 0
);

CREATE TABLE api_tokens (
    token           VARCHAR(64) PRIMARY KEY,
    worker_id       VARCHAR(64) REFERENCES workers(worker_id),
    credits         REAL DEFAULT 0.0,
    total_earned    REAL DEFAULT 0.0,
    total_spent     REAL DEFAULT 0.0,
    requests_used   INTEGER DEFAULT 0,
    created         TIMESTAMP DEFAULT NOW()
);

CREATE TABLE jobs (
    job_id          VARCHAR(32) PRIMARY KEY,
    worker_id       VARCHAR(64) REFERENCES workers(worker_id),
    tokens_generated INTEGER,
    tokens_per_sec  REAL,
    duration_sec    REAL,
    model           VARCHAR(128),
    credits_earned  REAL,
    created         TIMESTAMP DEFAULT NOW()
);

CREATE TABLE contacts (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128),
    email           VARCHAR(256) NOT NULL,
    message         TEXT,
    created         TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_tokens_worker ON api_tokens(worker_id);
CREATE INDEX idx_jobs_worker ON jobs(worker_id);
CREATE INDEX idx_jobs_created ON jobs(created);
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import os
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("iamine.db")


# --- Chiffrement L3 : messages archives chiffres avec le token de compte ---

_PBKDF2_ITERATIONS = 100_000
_SALT_SIZE = 16  # 16 bytes de sel aléatoire


def _derive_key(api_token: str, salt: bytes) -> bytes:
    """Derive une cle Fernet via PBKDF2-SHA256 avec sel unique."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(api_token.encode()))


def _encrypt_messages(messages: list[dict], api_token: str) -> str:
    """Chiffre une liste de messages avec PBKDF2 + sel unique. Format: sel_base64:ciphertext."""
    import os
    from cryptography.fernet import Fernet
    salt = os.urandom(_SALT_SIZE)
    key = _derive_key(api_token, salt)
    f = Fernet(key)
    plaintext = json.dumps(messages, ensure_ascii=False).encode("utf-8")
    ciphertext = f.encrypt(plaintext).decode("ascii")
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    return f"{salt_b64}:{ciphertext}"


def _decrypt_messages(encrypted: str, api_token: str) -> list[dict]:
    """Dechiffre une liste de messages. Retourne [] si le token est invalide."""
    from cryptography.fernet import Fernet, InvalidToken
    try:
        # Format: sel_base64:ciphertext
        if ":" not in encrypted:
            # Ancien format sans sel — fallback SHA256 direct
            raw = hashlib.sha256(api_token.encode()).digest()
            key = base64.urlsafe_b64encode(raw)
            f = Fernet(key)
            plaintext = f.decrypt(encrypted.encode("ascii"))
            return json.loads(plaintext)
        salt_b64, ciphertext = encrypted.split(":", 1)
        salt = base64.urlsafe_b64decode(salt_b64)
        key = _derive_key(api_token, salt)
        f = Fernet(key)
        plaintext = f.decrypt(ciphertext.encode("ascii"))
        return json.loads(plaintext)
    except (InvalidToken, Exception):
        log.warning("L3 decrypt failed — wrong token or corrupted data")
        return []


def _decrypt_fact(fact_text_enc: str, salt_b64: str, api_token: str) -> str:
    """Dechiffre un fait individuel depuis user_memories."""
    from cryptography.fernet import Fernet, InvalidToken
    try:
        salt = base64.urlsafe_b64decode(salt_b64)
        key = _derive_key(api_token, salt)
        f = Fernet(key)
        return f.decrypt(fact_text_enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        return ""


@dataclass
class WorkerRecord:
    worker_id: str
    machine_hash: str = ""
    hostname: str = ""
    cpu: str = ""
    cpu_threads: int = 0
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    model_path: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    is_online: bool = False
    total_jobs: int = 0
    bench_tps: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class TokenRecord:
    token: str
    worker_id: str
    credits: float = 0.0
    total_earned: float = 0.0
    total_spent: float = 0.0
    requests_used: int = 0
    created: float = 0.0


@dataclass
class JobRecord:
    job_id: str
    worker_id: str
    tokens_generated: int = 0
    tokens_per_sec: float = 0.0
    duration_sec: float = 0.0
    model: str = ""
    credits_earned: float = 0.0
    created: float = 0.0
    routed_tier: str = ""                 # small | medium | large | code
    route_confidence: float | None = None  # [0.0, 1.0] — None si pas classifié
    route_method: str = ""                 # passive | heuristic | knn | llm_idle | fallback
    prompt_embedding: list[float] | None = None  # vecteur 384d (MiniLM), Phase 3 KNN


@dataclass
class ContactRecord:
    name: str = ""
    email: str = ""
    message: str = ""
    created: float = 0.0


class Store(ABC):
    """Interface abstraite pour le stockage persistant."""

    # --- Workers ---
    @abstractmethod
    async def get_worker(self, worker_id: str) -> WorkerRecord | None: ...

    @abstractmethod
    async def upsert_worker(self, record: WorkerRecord) -> None: ...

    @abstractmethod
    async def set_worker_offline(self, worker_id: str) -> None: ...

    @abstractmethod
    async def is_known_machine(self, machine_hash: str) -> bool: ...

    # --- Tokens ---
    @abstractmethod
    async def get_token(self, token: str) -> TokenRecord | None: ...

    @abstractmethod
    async def upsert_token(self, record: TokenRecord) -> None: ...

    @abstractmethod
    async def credit_token(self, token: str, amount: float) -> float: ...

    @abstractmethod
    async def debit_token(self, token: str, amount: float) -> bool: ...

    @abstractmethod
    async def get_token_by_worker(self, worker_id: str) -> TokenRecord | None: ...

    # --- Jobs ---
    @abstractmethod
    async def log_job(self, record: JobRecord) -> None: ...

    @abstractmethod
    async def get_job_count(self) -> int: ...

    # --- Conversations (L3 — tampon PostgreSQL, chiffré) ---
    @abstractmethod
    async def archive_messages(self, conv_id: str, messages: list[dict], summary: str = "", api_token: str = "") -> None:
        """Archive les messages compactés, chiffrés avec le token de compte."""
        ...

    @abstractmethod
    async def get_archived_messages(self, conv_id: str, api_token: str = "") -> list[dict]:
        """Récupère et déchiffre les messages archivés d'une conversation active."""
        ...

    @abstractmethod
    async def delete_conversation(self, conv_id: str) -> None:
        """Supprime l'archive quand la conversation expire."""
        ...

    @abstractmethod
    async def cleanup_expired_conversations(self) -> int:
        """Nettoie toutes les conversations expirées. Retourne le nombre supprimé."""
        ...
    async def get_conversation_summary(self, conv_id: str, api_token: str = "") -> str | None:
        """Recupere le resume L3 d'une conversation depuis la DB (leger, pas de messages)."""
        return None  # default: pas de resume

    # --- Contacts ---
    @abstractmethod
    async def save_contact(self, record: ContactRecord) -> None: ...

    # --- Worker Tasks (compactage distribué) ---
    @abstractmethod
    async def log_task(self, task_id: str, task_type: str, conv_id: str,
                       assigned_worker: str, source_worker: str, status: str = "done",
                       duration_sec: float = 0.0) -> None: ...

    @abstractmethod
    async def log_reward(self, worker_id: str, amount: float, label: str) -> None: ...

    # --- Pending Jobs (tampon DB anti-saturation) ---
    @abstractmethod
    async def enqueue_pending_job(self, job_id: str, conv_id: str, api_token: str,
                                   messages: list[dict], max_tokens: int,
                                   requested_model: str = "",
                                   webhook_url: str = "") -> None: ...

    @abstractmethod
    async def get_pending_job(self, job_id: str, api_token: str = "") -> dict | None: ...

    @abstractmethod
    async def get_next_pending_job(self) -> dict | None:
        """Prend le plus ancien job pending (FIFO) et le marque 'processing'."""
        ...

    @abstractmethod
    async def complete_pending_job(self, job_id: str, response: dict,
                                    worker_id: str = "") -> None: ...

    @abstractmethod
    async def fail_pending_job(self, job_id: str, error: str) -> None: ...

    @abstractmethod
    async def get_queue_stats(self) -> dict:
        """Retourne {pending: int, processing: int, completed: int, avg_wait_sec: float}."""
        ...

    async def count_pending_by_token(self, api_token: str) -> int:
        """Count pending jobs for a specific token (anti-flood)."""
        return 0

    async def get_cached_response(self, user_message: str) -> str | None:
        """Match user message against cached_responses patterns. Returns response or None."""
        return None

    @abstractmethod
    async def cleanup_expired_jobs(self, ttl_sec: int = 300) -> int:
        """Supprime les jobs expires (pending depuis plus de ttl_sec). Retourne le nombre supprime."""
        ...


class MemoryStore(Store):
    """Stockage en RAM — utilisé pour le MVP.

    Remplacer par PostgresStore pour la production.
    Les donnees sont perdues au redemarrage du pool.
    """

    def __init__(self):
        self._workers: dict[str, WorkerRecord] = {}
        self._tokens: dict[str, TokenRecord] = {}
        self._tokens_by_worker: dict[str, str] = {}  # worker_id -> token
        self._known_machines: set[str] = set()
        self._jobs: list[JobRecord] = []
        self._contacts: list[ContactRecord] = []
        self._archived_convs: dict[str, dict] = {}  # L3 tampon en RAM

    # --- Workers ---
    async def get_worker(self, worker_id: str) -> WorkerRecord | None:
        return self._workers.get(worker_id)

    async def upsert_worker(self, record: WorkerRecord) -> None:
        self._workers[record.worker_id] = record
        self._known_machines.add(record.machine_hash or record.worker_id)

    async def set_worker_offline(self, worker_id: str) -> None:
        w = self._workers.get(worker_id)
        if w:
            w.is_online = False

    async def is_known_machine(self, machine_hash: str) -> bool:
        return machine_hash in self._known_machines

    # --- Tokens ---
    async def get_token(self, token: str) -> TokenRecord | None:
        return self._tokens.get(token)

    async def upsert_token(self, record: TokenRecord) -> None:
        self._tokens[record.token] = record
        self._tokens_by_worker[record.worker_id] = record.token

    async def credit_token(self, token: str, amount: float) -> float:
        t = self._tokens.get(token)
        if t:
            t.credits += amount
            t.total_earned += amount
            return t.credits
        return 0.0

    async def debit_token(self, token: str, amount: float) -> bool:
        t = self._tokens.get(token)
        if t and t.credits >= amount:
            t.credits -= amount
            t.total_spent += amount
            t.requests_used += 1
            return True
        return False

    async def get_token_by_worker(self, worker_id: str) -> TokenRecord | None:
        tk = self._tokens_by_worker.get(worker_id)
        if tk:
            return self._tokens.get(tk)
        return None

    # --- Jobs ---
    async def log_job(self, record: JobRecord) -> None:
        self._jobs.append(record)
        # Garder les 10000 derniers jobs en RAM
        if len(self._jobs) > 10000:
            self._jobs = self._jobs[-10000:]

    async def get_job_count(self) -> int:
        return len(self._jobs)

    # --- Conversations (L3 — tampon RAM, pas de chiffrement) ---
    async def archive_messages(self, conv_id: str, messages: list[dict], summary: str = "", api_token: str = "") -> None:
        if conv_id not in self._archived_convs:
            self._archived_convs[conv_id] = {"messages": [], "summary": ""}
        self._archived_convs[conv_id]["messages"].extend(messages)
        if summary:
            self._archived_convs[conv_id]["summary"] = summary

    async def get_archived_messages(self, conv_id: str, api_token: str = "") -> list[dict]:
        arch = self._archived_convs.get(conv_id)
        return arch["messages"] if arch else []

    async def delete_conversation(self, conv_id: str) -> None:
        self._archived_convs.pop(conv_id, None)

    async def cleanup_expired_conversations(self) -> int:
        # En RAM, le nettoyage est géré par le router — rien à faire ici
        return 0

    # --- Contacts ---
    async def save_contact(self, record: ContactRecord) -> None:
        self._contacts.append(record)
        import json
        with open("contacts.jsonl", "a") as f:
            f.write(json.dumps({
                "name": record.name,
                "email": record.email,
                "message": record.message,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }) + "\n")

    # --- Worker Tasks ---
    async def log_task(self, task_id: str, task_type: str, conv_id: str,
                       assigned_worker: str, source_worker: str, status: str = "done",
                       duration_sec: float = 0.0) -> None:
        pass  # RAM store — pas de persistence des tâches

    async def log_reward(self, worker_id: str, amount: float, label: str) -> None:
        pass  # RAM store

    async def update_worker_real_tps(self, worker_id: str, job_tps: float, tokens: int) -> None:
        pass  # RAM store

    async def update_worker_assignment(self, worker_id: str, model_id: str,
                                        model_path: str, ctx_size: int, gpu_layers: int) -> None:
        pass  # RAM store

    async def get_worker_assignment(self, worker_id: str) -> dict | None:
        return None  # RAM store

    async def increment_worker_failures(self, worker_id: str) -> None:
        pass  # RAM store

    # --- Pending Jobs (tampon RAM) ---

    def __init_pending(self):
        if not hasattr(self, '_pending_jobs'):
            self._pending_jobs: dict[str, dict] = {}

    async def enqueue_pending_job(self, job_id: str, conv_id: str, api_token: str,
                                   messages: list[dict], max_tokens: int,
                                   requested_model: str = "",
                                   webhook_url: str = "") -> None:
        self.__init_pending()
        self._pending_jobs[job_id] = {
            "job_id": job_id, "conv_id": conv_id, "api_token": api_token,
            "messages": messages, "max_tokens": max_tokens,
            "requested_model": requested_model,
            "webhook_url": webhook_url,
            "status": "pending", "created_at": time.time(),
            "response": None, "error": None, "worker_id": None,
        }

    async def get_pending_job(self, job_id: str, api_token: str = "") -> dict | None:
        self.__init_pending()
        job = self._pending_jobs.get(job_id)
        if job and api_token and job["api_token"] != api_token:
            return None  # isolation
        return job

    async def get_next_pending_job(self) -> dict | None:
        self.__init_pending()
        for job in sorted(self._pending_jobs.values(), key=lambda j: j["created_at"]):
            if job["status"] == "pending":
                job["status"] = "processing"
                job["started_at"] = time.time()
                return job
        return None

    async def complete_pending_job(self, job_id: str, response: dict,
                                    worker_id: str = "") -> None:
        self.__init_pending()
        job = self._pending_jobs.get(job_id)
        if job:
            job["status"] = "completed"
            job["response"] = response
            job["worker_id"] = worker_id
            job["completed_at"] = time.time()

    async def fail_pending_job(self, job_id: str, error: str) -> None:
        self.__init_pending()
        job = self._pending_jobs.get(job_id)
        if job:
            job["status"] = "failed"
            job["error"] = error

    async def get_queue_stats(self) -> dict:
        self.__init_pending()
        jobs = list(self._pending_jobs.values())
        pending = sum(1 for j in jobs if j["status"] == "pending")
        processing = sum(1 for j in jobs if j["status"] == "processing")
        completed = sum(1 for j in jobs if j["status"] == "completed")
        waits = [j.get("completed_at", time.time()) - j["created_at"]
                 for j in jobs if j["status"] == "completed" and j.get("completed_at")]
        return {
            "pending": pending, "processing": processing, "completed": completed,
            "avg_wait_sec": round(sum(waits) / len(waits), 1) if waits else 0,
        }


    async def count_pending_by_token(self, api_token: str) -> int:
        """Count pending jobs for a specific token."""
        if not hasattr(self, '_pending_jobs'):
            return 0
        return sum(1 for j in self._pending_jobs.values() if j.get("api_token") == api_token and j.get("status") == "pending")

    async def get_cached_response(self, user_message: str) -> str | None:
        """Simple pattern match against hardcoded responses."""
        import re
        msg = user_message.lower()
        patterns = [
            (r"bonjour|salut|hello|hi|hey", "Bonjour ! Je suis IAMINE. Le pool est actuellement charge. Reessayez dans quelques instants."),
            (r"aide|help|comment", "IAMINE est un reseau d'inference IA distribue. pip install iamine-ai && python -m iamine worker --auto"),
        ]
        for pat, resp in patterns:
            if re.search(pat, msg):
                return resp
        return None
    async def cleanup_expired_jobs(self, ttl_sec: int = 300) -> int:
        self.__init_pending()
        now = time.time()
        expired = [jid for jid, j in self._pending_jobs.items()
                   if j["status"] in ("pending", "failed") and now - j["created_at"] > ttl_sec]
        # Aussi nettoyer les completed de plus de 60s (deja poll)
        expired += [jid for jid, j in self._pending_jobs.items()
                    if j["status"] == "completed" and now - j.get("completed_at", now) > 60]
        for jid in expired:
            del self._pending_jobs[jid]
        return len(expired)


# --- Placeholder pour PostgreSQL (a implementer lors de la migration) ---


    async def update_checker_score(self, worker_id: str, passed: bool,
                                     score: float) -> None:
        """Stub - MemoryStore ne persiste pas les scores checker."""
        pass

    async def get_checker_score(self, worker_id: str) -> dict:
        """Stub - MemoryStore retourne le score par defaut."""
        return {"score": 1.0, "fails": 0, "total": 0, "passed": 0}

_MIGRATION_SEARCH_PATHS_CHECKED = []

def _discover_migrations_dir():
    """Try multiple locations in order for the migrations/ directory:

    1. Package source checkout : <pkg>/../migrations (git clone layout, VPS prod)
    2. Data files directory : <prefix>/share/iamine-migrations (wheel install,
       where setuptools data-files places them)
    3. Sibling of site-packages : <site-packages>/../../../share/iamine-migrations
    4. Package data inside the wheel : <pkg>/migrations
    5. /opt/iamine/migrations (system-wide fallback for docker images)
    6. $IAMINE_MIGRATIONS_DIR env var override (last-resort escape hatch)
    """
    global _MIGRATION_SEARCH_PATHS_CHECKED
    _MIGRATION_SEARCH_PATHS_CHECKED = []
    import sys as _sys

    candidates = []

    pkg_dir = Path(__file__).parent
    # 1. checkout layout
    candidates.append(pkg_dir.parent / "migrations")
    # 2. setuptools data-files prefix (sys.prefix/share/iamine-migrations)
    candidates.append(Path(_sys.prefix) / "share" / "iamine-migrations")
    # 2b. setuptools data-files without share/ prefix (venv layout)
    candidates.append(Path(_sys.prefix) / "iamine-migrations")
    # 3. sibling of site-packages for venv installs (common)
    candidates.append(pkg_dir.parent.parent.parent.parent / "share" / "iamine-migrations")
    # 4. package-data inside wheel
    candidates.append(pkg_dir / "migrations")
    # 5. opt system layout
    candidates.append(Path("/opt/iamine/migrations"))
    # 6. env override
    env_override = os.environ.get("IAMINE_MIGRATIONS_DIR")
    if env_override:
        candidates.insert(0, Path(env_override))

    for c in candidates:
        _MIGRATION_SEARCH_PATHS_CHECKED.append(str(c))
        try:
            if c.exists() and c.is_dir() and any(c.glob("*.sql")):
                return c
        except Exception:
            continue
    return None


class PostgresStore(Store):
    """Stockage PostgreSQL — pour la production sur iamine.org.

    Necessite : pip install asyncpg
    Usage :
        store = PostgresStore("postgresql://user:pass@host/iamine")
        await store.connect()
    """

    def __init__(self, dsn: str = "", host: str = "localhost", port: int = 5432,
                 user: str = "", password: str = "", database: str = "iamine"):
        self.dsn = dsn
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self.pool = None

    async def connect(self):
        import asyncpg
        if self.dsn:
            self.pool = await asyncpg.create_pool(self.dsn)
        else:
            self.pool = await asyncpg.create_pool(
                host=self._host, port=self._port,
                user=self._user, password=self._password,
                database=self._database,
            )
        log.info(f"PostgreSQL connected: {self._host}:{self._port}/{self._database}")
        await self.run_migrations()

    async def run_migrations(self):
        """Execute les migrations SQL non encore appliquees depuis migrations/."""
        migrations_dir = _discover_migrations_dir()

        if migrations_dir is None or not migrations_dir.exists():
            log.warning(f"Migrations dir not found (checked {_MIGRATION_SEARCH_PATHS_CHECKED}), running legacy _create_tables_fallback")
            await self._create_tables_fallback()
            return

        async with self.pool.acquire() as conn:
            # Creer schema_version si necessaire
            schema_file = migrations_dir / "000_schema_version.sql"
            if schema_file.exists():
                await conn.execute(schema_file.read_text())

            # Lire la version actuelle
            current_version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            )

            # Lister les fichiers de migration (001_xxx.sql, 002_xxx.sql, ...)
            migration_files = sorted(
                f for f in migrations_dir.iterdir()
                if f.suffix == ".sql" and f.name[0].isdigit() and f.name != "000_schema_version.sql"
            )

            applied = 0
            for mf in migration_files:
                # Extraire le numero de version du nom de fichier (ex: 001_initial_schema.sql -> 1)
                try:
                    version = int(mf.name.split("_", 1)[0])
                except ValueError:
                    continue

                if version <= current_version:
                    continue

                sql = mf.read_text()
                try:
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_version (version, filename) "
                        "VALUES ($1, $2) ON CONFLICT (version) DO NOTHING",
                        version, mf.name
                    )
                    applied += 1
                    log.info(f"Migration {mf.name} applied successfully")
                except Exception as e:
                    log.error(f"Migration {mf.name} failed: {e}")
                    raise

            if applied == 0:
                log.info(f"Schema up to date (version {current_version})")
            else:
                log.info(f"{applied} migration(s) applied, now at version {current_version + applied}")

        log.info("PostgreSQL tables ready")

    async def _create_tables_fallback(self):
        """Fallback legacy si le dossier migrations/ n'existe pas."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id VARCHAR(64) PRIMARY KEY,
                    machine_hash VARCHAR(64) NOT NULL,
                    hostname VARCHAR(128),
                    cpu VARCHAR(128),
                    cpu_threads INTEGER,
                    ram_total_gb REAL,
                    model_path VARCHAR(256),
                    first_seen TIMESTAMP DEFAULT NOW(),
                    last_seen TIMESTAMP DEFAULT NOW(),
                    is_online BOOLEAN DEFAULT FALSE,
                    total_jobs INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS api_tokens (
                    token VARCHAR(64) PRIMARY KEY,
                    worker_id VARCHAR(64) REFERENCES workers(worker_id),
                    credits REAL DEFAULT 0.0,
                    total_earned REAL DEFAULT 0.0,
                    total_spent REAL DEFAULT 0.0,
                    requests_used INTEGER DEFAULT 0,
                    created TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id VARCHAR(32) PRIMARY KEY,
                    worker_id VARCHAR(64),
                    tokens_generated INTEGER,
                    tokens_per_sec REAL,
                    duration_sec REAL,
                    model VARCHAR(128),
                    credits_earned REAL,
                    created TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS contacts (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(128),
                    email VARCHAR(256) NOT NULL,
                    message TEXT,
                    created TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conv_id VARCHAR(64) PRIMARY KEY,
                    api_token VARCHAR(64),
                    messages JSONB DEFAULT '[]'::jsonb,
                    last_activity TIMESTAMP DEFAULT NOW(),
                    expires TIMESTAMP DEFAULT NOW() + INTERVAL '1 hour'
                );
            """)
        log.info("PostgreSQL tables ready (fallback)")


    # --- Workers ---
    async def get_worker(self, worker_id: str) -> WorkerRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM workers WHERE worker_id=$1", worker_id)
            if row:
                return WorkerRecord(
                    worker_id=row["worker_id"], machine_hash=row["machine_hash"],
                    hostname=row["hostname"] or "", cpu=row["cpu"] or "",
                    cpu_threads=row["cpu_threads"] or 0, ram_total_gb=row["ram_total_gb"] or 0,
                    is_online=row["is_online"], total_jobs=row["total_jobs"] or 0,
                    first_seen=row["first_seen"].timestamp(), last_seen=row["last_seen"].timestamp(),
                )
        return None

    async def upsert_worker(self, record: WorkerRecord) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO workers (worker_id, machine_hash, hostname, cpu, cpu_threads,
                    ram_total_gb, model_path, is_online, last_seen,
                    gpu, gpu_vram_gb, has_gpu, version, bench_tps, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),$9,$10,$11,$12,$13,'online')
                ON CONFLICT (worker_id) DO UPDATE SET
                    is_online=TRUE, last_seen=NOW(), status='online',
                    hostname=EXCLUDED.hostname, cpu=EXCLUDED.cpu,
                    cpu_threads=EXCLUDED.cpu_threads, ram_total_gb=EXCLUDED.ram_total_gb,
                    model_path=EXCLUDED.model_path,
                    gpu=EXCLUDED.gpu, gpu_vram_gb=EXCLUDED.gpu_vram_gb,
                    has_gpu=EXCLUDED.has_gpu, version=EXCLUDED.version,
                    bench_tps=EXCLUDED.bench_tps
            """, record.worker_id, record.machine_hash or record.worker_id,
                record.hostname, record.cpu, record.cpu_threads,
                record.ram_total_gb, record.model_path, record.is_online,
                record.extra.get("gpu", ""),
                record.extra.get("gpu_vram_gb", 0),
                record.extra.get("has_gpu", False),
                record.extra.get("version", ""),
                record.bench_tps)

    async def set_worker_offline(self, worker_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET is_online=FALSE, status='offline', last_seen=NOW() WHERE worker_id=$1",
                worker_id)

    async def update_worker_real_tps(self, worker_id: str, job_tps: float, tokens: int) -> None:
        """Met a jour la perf reelle (moyenne glissante) apres chaque job."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE workers SET
                    real_tps = CASE WHEN real_tps > 0
                        THEN real_tps * 0.8 + $2 * 0.2
                        ELSE $2 END,
                    total_tokens = COALESCE(total_tokens, 0) + $3,
                    total_jobs = COALESCE(total_jobs, 0) + 1,
                    last_seen = NOW()
                WHERE worker_id = $1
            """, worker_id, job_tps, tokens)

    async def update_worker_assignment(self, worker_id: str, model_id: str,
                                        model_path: str, ctx_size: int,
                                        gpu_layers: int) -> None:
        """Enregistre l'attribution de modele decidee par le pool."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE workers SET
                    assigned_model_id=$2, assigned_model_path=$3,
                    assigned_ctx_size=$4, assigned_gpu_layers=$5,
                    status='migrating'
                WHERE worker_id=$1
            """, worker_id, model_id, model_path, ctx_size, gpu_layers)
    async def update_checker_score(self, worker_id: str, passed: bool,
                                     score: float) -> None:
        """Met a jour le score checker d'un worker apres une verification."""
        async with self.pool.acquire() as conn:
            if passed:
                await conn.execute("""
                    UPDATE workers SET
                        checker_score = $2,
                        checker_fails = 0,
                        checker_total = COALESCE(checker_total, 0) + 1,
                        checker_passed = COALESCE(checker_passed, 0) + 1
                    WHERE worker_id = $1
                """, worker_id, score)
            else:
                await conn.execute("""
                    UPDATE workers SET
                        checker_score = $2,
                        checker_fails = COALESCE(checker_fails, 0) + 1,
                        checker_total = COALESCE(checker_total, 0) + 1
                    WHERE worker_id = $1
                """, worker_id, score)

    async def get_checker_score(self, worker_id: str) -> dict:
        """Recupere le score checker d'un worker."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT checker_score, checker_fails, checker_total, checker_passed
                FROM workers WHERE worker_id = $1
            """, worker_id)
            if row:
                return {
                    "score": row["checker_score"] or 1.0,
                    "fails": row["checker_fails"] or 0,
                    "total": row["checker_total"] or 0,
                    "passed": row["checker_passed"] or 0,
                }
        return {"score": 1.0, "fails": 0, "total": 0, "passed": 0}



    async def get_worker_assignment(self, worker_id: str) -> dict | None:
        """Recupere l'attribution de modele pour un worker."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT assigned_model_id, assigned_model_path, assigned_ctx_size, assigned_gpu_layers FROM workers WHERE worker_id=$1",
                worker_id)
            if row and row["assigned_model_path"]:
                return {
                    "model_id": row["assigned_model_id"],
                    "model_path": row["assigned_model_path"],
                    "ctx_size": row["assigned_ctx_size"] or 4096,
                    "gpu_layers": row["assigned_gpu_layers"] or 0,
                }
        return None


    async def delete_worker(self, worker_id: str):
        """Supprime un worker de la DB."""
        await self.pool.execute("DELETE FROM workers WHERE worker_id = $1", worker_id)

    async def get_workers_by_account(self, account_id: str) -> list[dict]:
        """Retourne les workers lies a un compte depuis la DB."""
        rows = await self.pool.fetch(
            "SELECT worker_id, model_path, bench_tps, status FROM workers WHERE account_id = $1",
            account_id)
        return [{"worker_id": r["worker_id"], "model_path": r["model_path"],
                 "bench_tps": r["bench_tps"], "status": r["status"]} for r in rows]

    async def is_pool_managed(self, worker_id: str) -> bool:
        """Retourne True si le pool peut envoyer update_model a ce worker."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT pool_managed FROM workers WHERE worker_id=$1", worker_id)
            if row and row["pool_managed"] is not None:
                return row["pool_managed"]
        return True  # par defaut, le pool gere

    async def set_pool_managed(self, worker_id: str, managed: bool) -> None:
        """Active/desactive la gestion automatique du pool pour ce worker."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET pool_managed=$2 WHERE worker_id=$1",
                worker_id, managed)

    async def increment_worker_failures(self, worker_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET jobs_failed = COALESCE(jobs_failed, 0) + 1 WHERE worker_id=$1",
                worker_id)

    async def is_known_machine(self, machine_hash: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM workers WHERE machine_hash=$1", machine_hash)
            return row is not None

    # --- Hardware Benchmarks (base hashrate style XMRig) ---
    async def lookup_hardware_benchmarks(self, cpu_model: str, gpu_model: str = "") -> list[dict]:
        """Cherche les benchmarks connus pour ce CPU/GPU. Retourne [{model_id, measured_tps, sample_count}]."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT model_id, measured_tps, sample_count FROM hardware_benchmarks
                WHERE cpu_model=$1 AND gpu_model=$2
                ORDER BY measured_tps DESC
            """, cpu_model, gpu_model or "")
            return [{"model_id": r["model_id"], "measured_tps": r["measured_tps"],
                     "sample_count": r["sample_count"]} for r in rows]

    async def upsert_hardware_benchmark(self, cpu_model: str, gpu_model: str,
                                         ram_gb: float, model_id: str, tps: float) -> None:
        """Ajoute ou met a jour un benchmark hardware (moyenne glissante)."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO hardware_benchmarks (cpu_model, gpu_model, ram_gb, model_id, measured_tps, sample_count, last_updated)
                VALUES ($1, $2, $3, $4, $5, 1, NOW())
                ON CONFLICT (cpu_model, gpu_model, model_id) DO UPDATE SET
                    measured_tps = (hardware_benchmarks.measured_tps * hardware_benchmarks.sample_count + $5)
                                   / (hardware_benchmarks.sample_count + 1),
                    sample_count = hardware_benchmarks.sample_count + 1,
                    ram_gb = GREATEST(hardware_benchmarks.ram_gb, $3),
                    last_updated = NOW()
            """, cpu_model, gpu_model or "", ram_gb, model_id, tps)

    # --- Pool Config ---
    async def get_config(self, key: str, default: str = "") -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM pool_config WHERE key=$1", key)
            return row["value"] if row else default

    async def set_config(self, key: str, value: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pool_config (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
            """, key, value)

    # --- Tokens ---
    async def get_token(self, token: str) -> TokenRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM api_tokens WHERE token=$1", token)
            if row:
                return TokenRecord(
                    token=row["token"], worker_id=row["worker_id"],
                    credits=row["credits"], total_earned=row["total_earned"],
                    total_spent=row["total_spent"], requests_used=row["requests_used"],
                    created=row["created"].timestamp(),
                )
        return None

    async def upsert_token(self, record: TokenRecord) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO api_tokens (token, worker_id, credits, total_earned, total_spent, requests_used)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (token) DO UPDATE SET
                    credits=EXCLUDED.credits, total_earned=EXCLUDED.total_earned,
                    total_spent=EXCLUDED.total_spent, requests_used=EXCLUDED.requests_used
            """, record.token, record.worker_id, record.credits,
                record.total_earned, record.total_spent, record.requests_used)

    async def credit_token(self, token: str, amount: float) -> float:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE api_tokens SET credits=credits+$2, total_earned=total_earned+$2
                WHERE token=$1 RETURNING credits
            """, token, amount)
            return row["credits"] if row else 0.0

    async def debit_token(self, token: str, amount: float) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE api_tokens SET credits=credits-$2, total_spent=total_spent+$2, requests_used=requests_used+1
                WHERE token=$1 AND credits>=$2 RETURNING credits
            """, token, amount)
            return row is not None

    async def get_token_by_worker(self, worker_id: str) -> TokenRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM api_tokens WHERE worker_id=$1", worker_id)
            if row:
                return TokenRecord(
                    token=row["token"], worker_id=row["worker_id"],
                    credits=row["credits"], total_earned=row["total_earned"],
                    total_spent=row["total_spent"], requests_used=row["requests_used"],
                )
        return None

    # --- Jobs ---
    async def log_job(self, record: JobRecord) -> None:
        emb_literal = None
        emb = getattr(record, "prompt_embedding", None)
        if emb:
            emb_literal = "[" + ",".join(f"{x:.6f}" for x in emb) + "]"
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jobs (job_id, worker_id, tokens_generated, tokens_per_sec, duration_sec, model, credits_earned,
                                  routed_tier, route_confidence, route_method, prompt_embedding)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::vector)
            """, record.job_id, record.worker_id, record.tokens_generated,
                record.tokens_per_sec, record.duration_sec, record.model, record.credits_earned,
                record.routed_tier or None, record.route_confidence, record.route_method or None,
                emb_literal)
            await conn.execute("UPDATE workers SET total_jobs=total_jobs+1 WHERE worker_id=$1", record.worker_id)

    async def knn_tier_vote(self, embedding: list[float], k: int = 10) -> tuple[str, float, int] | None:
        """KNN vote pour smart routing Phase 3.

        Query les k voisins les plus proches via ivfflat cosine, vote majorite.
        Exclut les jobs flags reprompt_fast (Phase 5 feedback loop) car un
        re-prompt rapide indique que le routing precedent etait mauvais — on
        ne veut pas propager la mauvaise decision dans le KNN.

        Retourne (tier, confidence, n_found) ou None si cold start.
        """
        if not embedding or len(embedding) != 384:
            return None
        emb_literal = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.routed_tier
                FROM jobs j
                LEFT JOIN (
                    SELECT DISTINCT job_id FROM routing_feedback
                    WHERE feedback_signal = 'reprompt_fast'
                ) f ON f.job_id = j.job_id
                WHERE j.prompt_embedding IS NOT NULL
                  AND j.routed_tier IS NOT NULL
                  AND j.route_method IN ('heuristic', 'llm_idle', 'knn')
                  AND f.job_id IS NULL
                ORDER BY j.prompt_embedding <=> $1::vector
                LIMIT $2
                """,
                emb_literal, k,
            )
        if not rows:
            return None
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["routed_tier"]] = counts.get(r["routed_tier"], 0) + 1
        top_tier, top_n = max(counts.items(), key=lambda kv: kv[1])
        conf = top_n / len(rows)
        return top_tier, conf, len(rows)

    async def log_routing_feedback(self, job_id: str, signal: str, metadata: dict | None = None) -> None:
        """Enregistre un signal de feedback sur le routing d'un job (Phase 5).

        Signaux : reprompt_fast | regenerate | user_flag | success
        """
        import json as _json
        meta_json = _json.dumps(metadata) if metadata else None
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO routing_feedback (job_id, feedback_signal, metadata) VALUES ($1, $2, $3::jsonb)",
                    job_id, signal, meta_json,
                )
            except Exception as e:
                log.debug(f"log_routing_feedback failed (non-blocking): {e}")

    async def routing_stats(self, since_hours: int = 24) -> dict:
        """Agregats pour /v1/admin/routing_stats (Phase 5)."""
        async with self.pool.acquire() as conn:
            distribution = await conn.fetch(
                f"""
                SELECT routed_tier, route_method, COUNT(*) as n,
                       ROUND(AVG(duration_sec)::numeric, 2) as avg_duration,
                       ROUND(AVG(tokens_per_sec)::numeric, 1) as avg_tps
                FROM jobs
                WHERE created > NOW() - INTERVAL '{int(since_hours)} hours'
                  AND routed_tier IS NOT NULL
                GROUP BY routed_tier, route_method
                ORDER BY n DESC
                """,
            )
            mis_routed = await conn.fetchrow(
                f"""
                SELECT COUNT(DISTINCT f.job_id) as flagged,
                       (SELECT COUNT(*) FROM jobs WHERE created > NOW() - INTERVAL '{int(since_hours)} hours') as total
                FROM routing_feedback f
                JOIN jobs j ON j.job_id = f.job_id
                WHERE f.feedback_signal = 'reprompt_fast'
                  AND j.created > NOW() - INTERVAL '{int(since_hours)} hours'
                """,
            )
        total = mis_routed["total"] or 0
        flagged = mis_routed["flagged"] or 0
        return {
            "window_hours": since_hours,
            "total_jobs": total,
            "mis_routed_count": flagged,
            "mis_routed_rate": round(flagged / total, 3) if total else 0.0,
            "distribution": [
                {"routed_tier": r["routed_tier"], "route_method": r["route_method"],
                 "n": r["n"], "avg_duration": float(r["avg_duration"] or 0),
                 "avg_tps": float(r["avg_tps"] or 0)}
                for r in distribution
            ],
        }

    async def get_job_count(self) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) as c FROM jobs")
            return row["c"]

    # --- Conversations (L3 — tampon PostgreSQL, chiffré) ---
    async def archive_messages(self, conv_id: str, messages: list[dict], summary: str = "", api_token: str = "") -> None:
        # Chiffrer les messages avec le token de compte
        encrypted_blob = _encrypt_messages(messages, api_token) if api_token else json.dumps(messages)
        is_encrypted = bool(api_token)

        async with self.pool.acquire() as conn:
            # Chaque compaction ajoute un blob chiffré dans un JSONB array
            # Format : ["blob1", "blob2", ...] — chaque blob est un chunk chiffré
            blob_json = json.dumps([encrypted_blob])
            await conn.execute("""
                INSERT INTO conversations (conv_id, api_token, messages, expires)
                VALUES ($1, $2, $3::jsonb,
                        CASE WHEN $2::text LIKE 'acc_%' THEN NOW() + INTERVAL '10 years' ELSE NOW() + INTERVAL '1 hour' END)
                ON CONFLICT (conv_id) DO UPDATE SET
                    messages = conversations.messages || $3::jsonb,
                    last_activity = NOW(),
                    expires = CASE WHEN $2::text LIKE 'acc_%' THEN NOW() + INTERVAL '10 years' ELSE NOW() + INTERVAL '1 hour' END
            """, conv_id, api_token or "", blob_json)

    async def get_archived_messages(self, conv_id: str, api_token: str = "") -> list[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT messages FROM conversations WHERE conv_id=$1 AND expires > NOW()", conv_id
            )
            if not row or not row["messages"]:
                return []

            blobs = row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"])
            all_messages = []
            for blob in blobs:
                if isinstance(blob, str) and api_token:
                    # Blob chiffré → déchiffrer
                    all_messages.extend(_decrypt_messages(blob, api_token))
                elif isinstance(blob, list):
                    # Blob non chiffré (ancien format)
                    all_messages.extend(blob)
                elif isinstance(blob, dict):
                    all_messages.append(blob)
            return all_messages

    async def delete_conversation(self, conv_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM conversations WHERE conv_id=$1", conv_id)

    async def cleanup_expired_conversations(self) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM conversations WHERE expires < NOW() AND api_token NOT LIKE 'acc_%'")
            count = int(result.split()[-1])  # "DELETE N"
            if count > 0:
                log.info(f"L3 cleanup: {count} conversations expirées supprimées de PostgreSQL")
            return count

    # --- Conversations persistantes ---

    async def save_conversation_state(self, conv_id: str, api_token: str,
                                       messages: list[dict], summary: str = "",
                                       title: str = "", total_tokens: int = 0) -> None:
        """Sauvegarde complete d'une conversation (persistante pour acc_*)."""
        encrypted_blob = _encrypt_messages(messages, api_token) if api_token else json.dumps(messages)
        summary_enc = _encrypt_messages([{"content": summary}], api_token) if (api_token and summary) else ""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO conversations (conv_id, api_token, messages, title, message_count, total_tokens, last_activity, expires)
                VALUES ($1, $2::text, $3::jsonb, $4, $5, $6, NOW(),
                        CASE WHEN $2::text LIKE 'acc_%' THEN NOW() + INTERVAL '10 years' ELSE NOW() + INTERVAL '1 hour' END)
                ON CONFLICT (conv_id) DO UPDATE SET
                    messages = $3::jsonb,
                    title = COALESCE(NULLIF($4, ''), conversations.title),
                    message_count = $5,
                    total_tokens = $6,
                    last_activity = NOW(),
                    expires = CASE WHEN $2::text LIKE 'acc_%' THEN NOW() + INTERVAL '10 years' ELSE NOW() + INTERVAL '1 hour' END
            """, conv_id, api_token or "",
                json.dumps([encrypted_blob, summary_enc] if summary_enc else [encrypted_blob]),
                title, len(messages), total_tokens)

    async def list_conversations(self, api_token: str, limit: int = 50) -> list[dict]:
        """Liste les conversations d'un utilisateur authentifie."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT conv_id, title, message_count, total_tokens, last_activity, created_at
                FROM (
                    SELECT conv_id, title, message_count, total_tokens, last_activity,
                           COALESCE(last_activity, NOW()) as created_at
                    FROM conversations
                    WHERE api_token = $1
                ) sub
                ORDER BY last_activity DESC
                LIMIT $2
            """, api_token, limit)
            return [{"conv_id": r["conv_id"], "title": r["title"] or "",
                     "message_count": r["message_count"] or 0,
                     "total_tokens": r["total_tokens"] or 0,
                     "last_activity": r["last_activity"].isoformat() if r["last_activity"] else ""
                     } for r in rows]

    async def load_conversation(self, conv_id: str, api_token: str) -> dict | None:
        """Charge une conversation complete (messages + summary)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT messages, title, message_count, total_tokens FROM conversations WHERE conv_id=$1 AND api_token=$2",
                conv_id, api_token)
            if not row:
                return None
            blobs = row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"])
            all_messages = []
            summary = ""
            for blob in blobs:
                if isinstance(blob, str) and api_token:
                    decrypted = _decrypt_messages(blob, api_token)
                    # Check if this blob is a summary
                    if len(decrypted) == 1 and "content" in decrypted[0] and "role" not in decrypted[0]:
                        summary = decrypted[0]["content"]
                    else:
                        all_messages.extend(decrypted)
                elif isinstance(blob, list):
                    all_messages.extend(blob)
                elif isinstance(blob, dict):
                    all_messages.append(blob)
            return {
                "conv_id": conv_id,
                "title": row["title"] or "",
                "messages": all_messages,
                "summary": summary,
                "message_count": row["message_count"] or 0,
                "total_tokens": row["total_tokens"] or 0,
            }

    async def get_conversation_summary(self, conv_id: str, api_token: str = "") -> str | None:
        """Recupere le resume L3 sans charger tous les messages."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT messages FROM conversations WHERE conv_id=$1 AND api_token=$2",
                    conv_id, api_token)
                if not row:
                    return None
                blobs = row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"])
                for blob in blobs:
                    if isinstance(blob, str) and api_token:
                        decrypted = _decrypt_messages(blob, api_token)
                        if len(decrypted) == 1 and "content" in decrypted[0] and "role" not in decrypted[0]:
                            return decrypted[0]["content"]
                return None
        except Exception:
            return None
    async def delete_conversation_by_user(self, conv_id: str, api_token: str) -> bool:
        """Supprime une conversation (verification par api_token)."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM conversations WHERE conv_id=$1 AND api_token=$2",
                conv_id, api_token)
            return int(result.split()[-1]) > 0

    async def delete_user_conversations(self, api_token: str) -> int:
        """Supprime toutes les conversations d'un utilisateur (RGPD)."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM conversations WHERE api_token = $1", api_token)
            return int(result.split()[-1])

    # --- Memoire vectorisee RAG ---

    async def store_memory(self, token_hash: str, embedding: list[float],
                           fact_text: str, api_token: str, conv_id: str = "") -> None:
        """Stocke un fait vectorise chiffre pour un utilisateur."""
        import os as _os
        salt = _os.urandom(_SALT_SIZE)
        key = _derive_key(api_token, salt)
        from cryptography.fernet import Fernet
        f = Fernet(key)
        encrypted = f.encrypt(fact_text.encode("utf-8")).decode("ascii")
        salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_memories (token_hash, embedding, fact_text_enc, salt, conv_id)
                VALUES ($1, $2::vector, $3, $4, $5)
            """, token_hash, str(embedding), encrypted, salt_b64, conv_id)

    async def search_memories(self, token_hash: str, query_embedding: list[float],
                              limit: int = 5, min_similarity: float = 0.35,
                              conv_id: str = "") -> list[dict]:
        """Recherche les faits les plus proches par cosine similarity."""
        async with self.pool.acquire() as conn:
            # Verifier que la table a des donnees pour cet utilisateur (IVFFlat crash si vide)
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM user_memories WHERE token_hash = $1", token_hash)
            if not count:
                return []
            try:
                rows = await conn.fetch("""
                    SELECT id, fact_text_enc, salt,
                           1 - (embedding <=> $2::vector) AS similarity
                    FROM user_memories
                    WHERE token_hash = $1
                      AND 1 - (embedding <=> $2::vector) > $3
                      AND ($5 = '' OR conv_id = $5)
                    ORDER BY embedding <=> $2::vector
                    LIMIT $4
                """, token_hash, str(query_embedding), min_similarity, limit, conv_id or "")
            except Exception as e:
                log.warning(f"pgvector search failed (index not ready?): {e}")
                return []
            if rows:
                ids = [r["id"] for r in rows]
                await conn.execute("""
                    UPDATE user_memories SET last_accessed = NOW(), access_count = access_count + 1
                    WHERE id = ANY($1::bigint[])
                """, ids)
            return [{"id": r["id"], "fact_text_enc": r["fact_text_enc"],
                     "salt": r["salt"], "similarity": float(r["similarity"])} for r in rows]

    async def cleanup_stale_memories(self, days: int = 90) -> int:
        """Evicte les memoires non accedees depuis N jours."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_memories WHERE last_accessed < NOW() - $1 * INTERVAL '1 day'",
                days)
            return int(result.split()[-1])

    async def delete_user_memories(self, token_hash: str) -> int:
        """Supprime toutes les memoires d'un utilisateur (droit a l'oubli)."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_memories WHERE token_hash = $1", token_hash)
            return int(result.split()[-1])

    async def list_user_memories(self, api_token: str) -> list[dict]:
        """Liste les faits memorises d'un utilisateur (dechiffres)."""
        import hashlib
        token_hash = hashlib.sha256(api_token.encode()).hexdigest()
        rows = await self.pool.fetch(
            "SELECT id, fact_text_enc, salt, conv_id, created FROM user_memories "
            "WHERE token_hash = $1 ORDER BY created DESC LIMIT 200",
            token_hash)
        results = []
        for r in rows:
            try:
                fact = _decrypt_fact(r["fact_text_enc"], r["salt"], api_token)
            except Exception:
                fact = "(chiffre)"
            if not fact:
                fact = "(chiffre)"
            results.append({
                "id": r["id"],
                "fact": fact,
                "conv_id": r["conv_id"],
                "created": str(r["created"]),
            })
        return results

    async def delete_user_memory(self, memory_id: int, api_token: str) -> bool:
        """Supprime un fait specifique (verifie le proprietaire)."""
        import hashlib
        token_hash = hashlib.sha256(api_token.encode()).hexdigest()
        result = await self.pool.execute(
            "DELETE FROM user_memories WHERE id = $1 AND token_hash = $2",
            memory_id, token_hash)
        return "DELETE 1" in result

    # --- Contacts ---
    async def save_contact(self, record: ContactRecord) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO contacts (name, email, message) VALUES ($1,$2,$3)
            """, record.name, record.email, record.message)

    # --- Worker Tasks (compactage distribué) ---
    async def log_task(self, task_id: str, task_type: str, conv_id: str,
                       assigned_worker: str, source_worker: str, status: str = "done",
                       duration_sec: float = 0.0) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO worker_tasks (task_id, task_type, conv_id, assigned_worker, source_worker, status, duration_sec, completed)
                VALUES ($1,$2,$3,$4,$5,$6,$7, NOW())
            """, task_id, task_type, conv_id, assigned_worker, source_worker, status, duration_sec)

    async def log_reward(self, worker_id: str, amount: float, label: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO loyalty_rewards (worker_id, amount, label) VALUES ($1,$2,$3)
            """, worker_id, amount, label)

    # --- Pending Jobs (tampon DB anti-saturation) ---

    async def enqueue_pending_job(self, job_id: str, conv_id: str, api_token: str,
                                   messages: list[dict], max_tokens: int,
                                   requested_model: str = "",
                                   webhook_url: str = "") -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pending_jobs (job_id, conv_id, api_token, messages, max_tokens, requested_model, webhook_url)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
            """, job_id, conv_id, api_token, json.dumps(messages), max_tokens, requested_model, webhook_url)

    async def get_pending_job(self, job_id: str, api_token: str = "") -> dict | None:
        async with self.pool.acquire() as conn:
            if api_token:
                row = await conn.fetchrow(
                    "SELECT * FROM pending_jobs WHERE job_id=$1 AND api_token=$2",
                    job_id, api_token)
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM pending_jobs WHERE job_id=$1", job_id)
            if not row:
                return None
            return {
                "job_id": row["job_id"], "conv_id": row["conv_id"],
                "api_token": row["api_token"], "status": row["status"],
                "messages": row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"]),
                "max_tokens": row["max_tokens"],
                "requested_model": row["requested_model"] or "",
                "created_at": row["created_at"].timestamp() if row["created_at"] else 0,
                "worker_id": row["worker_id"],
                "response": row["response"] if isinstance(row["response"], dict) else (json.loads(row["response"]) if row["response"] else None),
                "error": row["error"],
            }

    async def get_next_pending_job(self) -> dict | None:
        async with self.pool.acquire() as conn:
            # SELECT FOR UPDATE SKIP LOCKED pour concurrence
            row = await conn.fetchrow("""
                UPDATE pending_jobs SET status='processing', started_at=NOW()
                WHERE job_id = (
                    SELECT job_id FROM pending_jobs
                    WHERE status='pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """)
            if not row:
                return None
            return {
                "job_id": row["job_id"], "conv_id": row["conv_id"],
                "api_token": row["api_token"],
                "messages": row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"]),
                "max_tokens": row["max_tokens"],
                "requested_model": row["requested_model"] or "",
                "created_at": row["created_at"].timestamp() if row["created_at"] else 0,
            }


    async def get_pending_job_webhook(self, job_id: str) -> str | None:
        """Get webhook_url for a pending job."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT webhook_url FROM pending_jobs WHERE job_id=$1", job_id)
                return row["webhook_url"] if row and row["webhook_url"] else None
        except Exception:
            return None

    async def complete_pending_job(self, job_id: str, response: dict,
                                    worker_id: str = "") -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pending_jobs SET status='completed', response=$2::jsonb,
                    worker_id=$3, completed_at=NOW()
                WHERE job_id=$1
            """, job_id, json.dumps(response), worker_id)

    async def fail_pending_job(self, job_id: str, error: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_jobs SET status='failed', error=$2 WHERE job_id=$1",
                job_id, error)

    async def get_queue_stats(self) -> dict:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT status, COUNT(*) as cnt,
                    AVG(EXTRACT(EPOCH FROM (COALESCE(completed_at, NOW()) - created_at)))
                        FILTER (WHERE status='completed') as avg_wait
                FROM pending_jobs
                WHERE created_at > NOW() - INTERVAL '1 hour'
                GROUP BY status
            """)
            stats = {"pending": 0, "processing": 0, "completed": 0, "avg_wait_sec": 0}
            for r in rows:
                stats[r["status"]] = r["cnt"]
                if r["avg_wait"] is not None:
                    stats["avg_wait_sec"] = round(float(r["avg_wait"]), 1)
            return stats


    async def count_pending_by_token(self, api_token: str) -> int:
        """Count pending jobs for a specific token (anti-flood)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM pending_jobs WHERE api_token=$1 AND status='pending'",
                api_token)
            return row['cnt'] if row else 0

    async def get_cached_response(self, user_message: str) -> str | None:
        """Match user message against cached_responses patterns (PostgreSQL)."""
        import re
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT pattern, response FROM cached_responses ORDER BY priority DESC")
                msg = user_message.lower()
                for row in rows:
                    if re.search(row['pattern'], msg):
                        return row['response']
        except Exception:
            pass
        return None
    async def cleanup_expired_jobs(self, ttl_sec: int = 300) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM pending_jobs
                WHERE (status IN ('pending', 'failed') AND created_at < NOW() - $1 * INTERVAL '1 second')
                   OR (status = 'completed' AND completed_at < NOW() - INTERVAL '60 seconds')
            """, ttl_sec)
            return int(result.split()[-1])
