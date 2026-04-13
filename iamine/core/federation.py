"""Molecule v2 — federation identity + envelope primitives (M3).

Feature flag IAMINE_FED (env): off | observe | active
- off      : module chargé, initialize_federation() no-op
- observe  : keygen + federation_self persist, endpoints log-only (M4+)
- active   : full federation enforcement (M6+)

M3 scope : keygen Ed25519, persist atom_id/pubkey/privkey, load federation_self.
Endpoints, signature middleware, forwarding = M4+.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("iamine.federation")


FED_MODE_OFF = "off"
FED_MODE_OBSERVE = "observe"
FED_MODE_ACTIVE = "active"

_KEY_DIR_DEFAULT = Path.home() / ".iamine" / "federation"
_PRIVKEY_NAME = "self_ed25519.key"

# M6 — kill switch filesystem sentinel. Si ce fichier existe, federation est
# forcibly disabled pour les endpoints signés, indépendamment de IAMINE_FED.
# Cache 5s pour éviter une stat() par request. Validé molecule-guardian 2026-04-10.
KILL_SWITCH_PATH = "/etc/iamine/fed_disable"
_KILL_SWITCH_CACHE_TTL = 5.0
_kill_switch_cache = {"ts": 0.0, "present": False}


def is_fed_disabled_by_fs() -> bool:
    """Check kill switch file existence. Cached 5s. Never raises."""
    import time as _time
    now = _time.monotonic()
    if now - _kill_switch_cache["ts"] < _KILL_SWITCH_CACHE_TTL:
        return _kill_switch_cache["present"]
    try:
        present = os.path.exists(KILL_SWITCH_PATH)
    except Exception:
        # Permission/IO error → default NOT disabled (fail-open, aligné
        # "toujours répondre" doctrine)
        present = False
    _kill_switch_cache["ts"] = now
    _kill_switch_cache["present"] = present
    return present


def get_mode() -> str:
    """Lire IAMINE_FED env var. Default = off."""
    mode = os.environ.get("IAMINE_FED", FED_MODE_OFF).strip().lower()
    if mode not in (FED_MODE_OFF, FED_MODE_OBSERVE, FED_MODE_ACTIVE):
        log.warning(f"Invalid IAMINE_FED={mode!r}, falling back to off")
        return FED_MODE_OFF
    return mode


@dataclass
class SelfIdentity:
    atom_id: str
    name: str
    pubkey: bytes
    privkey_path: str
    url: str
    molecule_id: Optional[str]
    capabilities: list


def _key_dir() -> Path:
    override = os.environ.get("IAMINE_FED_KEY_DIR")
    return Path(override) if override else _KEY_DIR_DEFAULT


def _atom_id_from_pubkey(pubkey: bytes) -> str:
    """Fingerprint = sha256(pubkey) hex (64 chars)."""
    return hashlib.sha256(pubkey).hexdigest()


def _generate_keypair() -> tuple[bytes, bytes]:
    """Retourne (privkey_raw_32, pubkey_raw_32)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv_raw, pub_raw


def _load_privkey_from_disk(path: Path) -> Optional[bytes]:
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
    except Exception as e:
        log.error(f"Cannot read privkey at {path}: {e}")
        return None
    if len(data) != 32:
        log.error(f"Privkey at {path} has invalid length {len(data)}")
        return None
    return data


def _pubkey_from_privkey(priv_raw: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.from_private_bytes(priv_raw)
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def sign(priv_raw: bytes, message: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(priv_raw)
    return priv.sign(message)


def verify(pubkey: bytes, signature: bytes, message: bytes) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        pub = Ed25519PublicKey.from_public_bytes(pubkey)
        pub.verify(signature, message)
        return True
    except (InvalidSignature, Exception):
        return False


# ---- R1 : hop counter + forward_chain helpers ----

HOP_MAX = 2


def envelope_check_hop(hop: int, chain: list[str], self_atom_id: str) -> Optional[str]:
    """Return None if envelope OK, else an error string."""
    if hop < 0 or hop > HOP_MAX:
        return f"hop out of range: {hop} (max {HOP_MAX})"
    if self_atom_id in chain:
        return f"loop detected: self {self_atom_id[:8]}... in forward_chain"
    if len(chain) != hop:
        return f"chain length {len(chain)} inconsistent with hop {hop}"
    return None


def envelope_bump(hop: int, chain: list[str], self_atom_id: str) -> tuple[int, list[str]]:
    return hop + 1, chain + [self_atom_id]


# ---- Envelope canonical form + verify (M4) ----

NONCE_WINDOW_SEC = 60  # anti-replay window


def canonical_envelope_body(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    hop: str,
    chain_csv: str,
    body: bytes,
) -> bytes:
    """Exact bytes signed/verified. Newline-separated. Hop+chain participent (R1)."""
    header = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{hop}\n{chain_csv}\n".encode()
    return header + body


async def _nonce_seen(pool, atom_id: str, nonce: str) -> bool:
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return False
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM federation_nonces WHERE atom_id=$1 AND nonce=$2",
            atom_id, nonce,
        )
    return row is not None


async def _record_nonce(pool, atom_id: str, nonce: str) -> None:
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO federation_nonces (atom_id, nonce) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            atom_id, nonce,
        )


async def verify_envelope(
    pool,
    peer_pubkey: bytes,
    method: str,
    path: str,
    headers: dict,
    body: bytes,
) -> Optional[str]:
    """Verify a signed envelope. Returns None if OK, else an error string.

    Does NOT enforce — caller decides (observe vs active).
    """
    import time as _time

    def _h(*names):
        for n in names:
            v = headers.get(n) or headers.get(n.lower())
            if v is not None:
                return v
        return None

    atom_id = _h("X-IAMINE-Atom-Id")
    ts = _h("X-IAMINE-Timestamp")
    nonce = _h("X-IAMINE-Nonce")
    hop_str = _h("X-IAMINE-Hop") or "0"
    chain_csv = _h("X-IAMINE-Forward-Chain") or ""
    sig_b64 = _h("X-IAMINE-Signature")

    if not (atom_id and ts and nonce and sig_b64):
        return "missing envelope headers"

    try:
        ts_int = int(ts)
    except ValueError:
        return "invalid timestamp"
    now = int(_time.time())
    if abs(now - ts_int) > NONCE_WINDOW_SEC:
        return f"timestamp out of window ({now - ts_int}s)"

    try:
        hop = int(hop_str)
    except ValueError:
        return "invalid hop"
    chain = [c for c in chain_csv.split(",") if c]
    self_id = pool.federation_self.atom_id if getattr(pool, "federation_self", None) else ""
    err = envelope_check_hop(hop, chain, self_id)
    if err:
        return err

    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        return "invalid base64 signature"
    canonical = canonical_envelope_body(method, path, ts, nonce, str(hop), chain_csv, body)
    if not verify(peer_pubkey, sig, canonical):
        return "signature mismatch"

    if await _nonce_seen(pool, atom_id, nonce):
        return "replay (nonce seen)"
    await _record_nonce(pool, atom_id, nonce)
    return None


def build_envelope_headers(
    priv_raw: bytes,
    atom_id: str,
    method: str,
    path: str,
    body: bytes,
    hop: int = 0,
    chain: Optional[list] = None,
) -> dict:
    """Build signed headers for an outgoing call (client-side helper)."""
    import secrets
    import time as _time
    ts = str(int(_time.time()))
    nonce = secrets.token_hex(8)
    chain_csv = ",".join(chain or [])
    canonical = canonical_envelope_body(method, path, ts, nonce, str(hop), chain_csv, body)
    sig = sign(priv_raw, canonical)
    return {
        "X-IAMINE-Atom-Id": atom_id,
        "X-IAMINE-Timestamp": ts,
        "X-IAMINE-Nonce": nonce,
        "X-IAMINE-Hop": str(hop),
        "X-IAMINE-Forward-Chain": chain_csv,
        "X-IAMINE-Signature": base64.b64encode(sig).decode(),
    }


# ---- Peers CRUD (M4) ----

async def upsert_peer(
    pool,
    atom_id: str,
    name: str,
    pubkey: bytes,
    url: str,
    molecule_id: Optional[str],
    capabilities: list,
    trust_level: int = 1,
    added_by: Optional[str] = None,
) -> None:
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        log.warning("upsert_peer: no PG store")
        return
    import json as _json
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO federation_peers (atom_id, name, pubkey, url, molecule_id, capabilities, trust_level, added_by, last_seen)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8, NOW())
            ON CONFLICT (atom_id) DO UPDATE SET
                name = EXCLUDED.name,
                url = EXCLUDED.url,
                molecule_id = EXCLUDED.molecule_id,
                capabilities = EXCLUDED.capabilities,
                last_seen = NOW()
            """,
            atom_id, name, pubkey, url, molecule_id, _json.dumps(capabilities), trust_level, added_by,
        )


async def load_peer(pool, atom_id: str):
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return None
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT atom_id, name, pubkey, url, molecule_id, capabilities, trust_level, last_seen, added_at, revoked_at FROM federation_peers WHERE atom_id=$1",
            atom_id,
        )
    return dict(row) if row else None


async def list_peers(pool, include_revoked: bool = False):
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return []
    q = "SELECT atom_id, name, url, molecule_id, capabilities, trust_level, last_seen, added_at, revoked_at FROM federation_peers"
    if not include_revoked:
        q += " WHERE revoked_at IS NULL"
    q += " ORDER BY added_at DESC"
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(q)
    return [dict(r) for r in rows]


# ---- M7-server-partial : workers certs + ledger attribution ----
# Scope : endpoint enroll admin + helpers verify + mark_ledger_attributed,
# SANS toucher iamine/worker.py. Scaffold pour M7-worker (wheel future).
# Replication RF=1 jusqu a M11.2 ledger/certs gossip. Append-only ledger
# hard invariant : revoke cert NE TOUCHE PAS aux rows ledger existantes.

import hashlib as _hashlib_worker_certs


async def enroll_worker_cert(pool, worker_id: str, pubkey: bytes) -> dict:
    """Countersign a worker pubkey with the local pool privkey.

    Returns dict with cert_id, pool_signer, signature_b64, enrolled_at.
    Raises RuntimeError if self privkey unavailable or no DB.
    """
    if len(pubkey) != 32:
        raise ValueError("pubkey must be 32 bytes Ed25519")
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        raise RuntimeError("no DB store")
    if pool.federation_self is None:
        raise RuntimeError("federation_self not initialized")

    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        raise RuntimeError("privkey unavailable for countersign")

    self_atom_id = pool.federation_self.atom_id
    import datetime as _dt
    enrolled_at = _dt.datetime.utcnow()

    # Canonical signing message : sha256(worker_id || pubkey || enrolled_at_iso || self_atom_id)
    msg_material = worker_id.encode() + pubkey + enrolled_at.isoformat().encode() + self_atom_id.encode()
    msg_hash = _hashlib_worker_certs.sha256(msg_material).digest()
    signature = sign(priv_raw, msg_hash)

    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO workers_certs (worker_id, pubkey, pool_signer, signature, enrolled_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (worker_id, pubkey) DO UPDATE SET
                pool_signer = EXCLUDED.pool_signer,
                signature = EXCLUDED.signature,
                enrolled_at = EXCLUDED.enrolled_at,
                revoked_at = NULL
            RETURNING id, enrolled_at
            """,
            worker_id, pubkey, self_atom_id, signature, enrolled_at,
        )

    log.info(f"worker enroll: worker_id={worker_id} pubkey={pubkey.hex()[:16]}... signed by {self_atom_id[:16]}...")
    return {
        "cert_id": row["id"],
        "worker_id": worker_id,
        "pool_signer": self_atom_id,
        "signature_b64": base64.b64encode(signature).decode(),
        "enrolled_at": row["enrolled_at"].isoformat(),
    }


async def verify_worker_signature(pool, worker_id: str, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature from a known worker.

    Looks up the worker_cert in workers_certs WHERE worker_id=$1 AND revoked_at IS NULL.
    Only verifies local pool certs (M11.2 will add peer-gossipped certs).
    Returns False on any failure (no raise).
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return False
    try:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT pubkey FROM workers_certs WHERE worker_id=$1 AND revoked_at IS NULL ORDER BY enrolled_at DESC LIMIT 1",
                worker_id,
            )
        if not row:
            log.debug(f"verify_worker_signature: no cert for worker_id={worker_id}")
            return False
        return verify(bytes(row["pubkey"]), signature, message)
    except Exception as e:
        log.warning(f"verify_worker_signature error: {e}")
        return False


async def mark_ledger_attributed(pool, job_id: str, worker_sig: bytes) -> bool:
    """Mark a ledger row as attributed to a signed worker response.

    Sets worker_sig and flips pending_worker_attribution=false. Only applies if
    the row is currently NULL (prevents double-attribution on retry).
    Returns True if exactly one row was updated.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return False
    async with pool.store.pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE revenue_ledger
            SET worker_sig = $1, pending_worker_attribution = false
            WHERE job_id = $2 AND worker_sig IS NULL
            """,
            worker_sig, job_id,
        )
    # asyncpg returns "UPDATE N" — parse count
    updated = 0
    if result and result.startswith("UPDATE "):
        try:
            updated = int(result.split()[1])
        except (IndexError, ValueError):
            updated = 0
    if updated > 0:
        log.info(f"ledger attributed: job_id={job_id} worker_sig={worker_sig[:8].hex() if worker_sig else None}...")
    return updated == 1


async def revoke_worker_cert(pool, worker_id: str, pubkey: Optional[bytes] = None) -> tuple:
    """Revoke a worker cert (soft). Does NOT cascade on revenue_ledger (append-only)."""
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return False, "no DB store"
    async with pool.store.pool.acquire() as conn:
        if pubkey is not None:
            result = await conn.execute(
                "UPDATE workers_certs SET revoked_at=NOW() WHERE worker_id=$1 AND pubkey=$2 AND revoked_at IS NULL",
                worker_id, pubkey,
            )
        else:
            result = await conn.execute(
                "UPDATE workers_certs SET revoked_at=NOW() WHERE worker_id=$1 AND revoked_at IS NULL",
                worker_id,
            )
    log.warning(f"worker cert revoked: worker_id={worker_id}")
    return True, str(result)


async def list_worker_certs(pool, include_revoked: bool = False) -> list:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return []
    q = "SELECT id, worker_id, pubkey, pool_signer, signature, enrolled_at, revoked_at FROM workers_certs"
    if not include_revoked:
        q += " WHERE revoked_at IS NULL"
    q += " ORDER BY enrolled_at DESC"
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(q)
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "worker_id": r["worker_id"],
            "pubkey_hex": bytes(r["pubkey"]).hex(),
            "pool_signer": r["pool_signer"],
            "signature_b64": base64.b64encode(bytes(r["signature"])).decode(),
            "enrolled_at": r["enrolled_at"].isoformat() if r["enrolled_at"] else None,
            "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
        })
    return out


# ---- M7b : molecule discovery + heartbeat support ----
# federation_peers.last_seen is classified `ephemeral-acceptable`: vue locale
# par pool, pas de RF>=2, reconstructible via heartbeat 30s. Validated by
# molecule-guardian 2026-04-10.

HEARTBEAT_INTERVAL_SEC = 30
PEER_UNREACHABLE_AFTER_SEC = 120


async def list_molecule_peers(pool, min_trust: int = 2):
    """Return peers suitable for worker failover discovery.

    Filters: trust_level >= min_trust, not revoked, last_seen within
    PEER_UNREACHABLE_AFTER_SEC. Ordered by last_seen DESC (freshest first) —
    intentionally NOT alphabetical nor seed-order to avoid implying hierarchy.
    """
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return []
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT atom_id, name, url, molecule_id, capabilities, trust_level, last_seen
            FROM federation_peers
            WHERE revoked_at IS NULL
              AND trust_level >= $1
              AND last_seen IS NOT NULL
              AND last_seen > NOW() - make_interval(secs => $2)
            ORDER BY last_seen DESC
            """,
            min_trust,
            PEER_UNREACHABLE_AFTER_SEC,
        )
    return [dict(r) for r in rows]


# ---- M10-active Q7 : account federation JWT (identity-only, phase 1) ----
#
# Decision D-ACCOUNT-FED (2026-04-11, david arbitration) :
#   (a) JWT signe cross-pool, IDENTITE UNIQUEMENT en phase 1.
#
# Shape: base64url(header).base64url(payload).base64url(Ed25519 signature)
# Header  : {"alg": "EdDSA", "typ": "IAMINE-JWT", "kid": <pool atom_id[:16]>}
# Payload : {"iss": <origin_pool atom_id>, "sub": <account_id>,
#            "email": <email or "">, "iat": <int unix>, "exp": <int unix>,
#            "ver": 1}
#
# Phase 1 scope (explicitly NOT included) :
#   - No credits balance transport (deferred until inter-pool settlement M10-active)
#   - No role/admin claims (admin is local to each pool)
#   - No audience field (accept-from-any-bonded-peer semantics)
#
# Verification : caller loads the emitter atom_id from `iss`, resolves the
# corresponding `federation_peers.pubkey`, and checks Ed25519 signature over
# the canonical `header.payload` bytes. Expiry is honored.
#
# Guardian note : this preserves "no master pool" (any bonded pool can emit),
# and the signature verification reuses the same Ed25519 primitive as the
# handshake envelope. No new crypto, no new key material.

ACCOUNT_JWT_TYP = "IAMINE-JWT"
ACCOUNT_JWT_ALG = "EdDSA"
ACCOUNT_JWT_VERSION = 1
ACCOUNT_JWT_DEFAULT_TTL_SEC = 3600  # 1 hour


def _b64url_encode(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_account_jwt(
    pool,
    account_id: str,
    email: str = "",
    ttl_sec: int = ACCOUNT_JWT_DEFAULT_TTL_SEC,
) -> dict:
    """Mint a cross-pool identity-only JWT signed with the pool Ed25519 key.

    Returns dict with token (str) + decoded header + payload (for debug).
    Raises ValueError if federation_self is missing or privkey unavailable.
    """
    import json as _json
    import time as _time

    ident = getattr(pool, "federation_self", None)
    if not ident:
        raise ValueError("federation_self missing, cannot sign JWT")
    priv_raw = _load_privkey_from_disk(Path(ident.privkey_path))
    if not priv_raw:
        raise ValueError(f"privkey not found at {ident.privkey_path}")

    if not account_id:
        raise ValueError("account_id is required")
    if ttl_sec <= 0 or ttl_sec > 86400:
        raise ValueError("ttl_sec must be in (0, 86400]")

    now = int(_time.time())
    header = {
        "alg": ACCOUNT_JWT_ALG,
        "typ": ACCOUNT_JWT_TYP,
        "kid": ident.atom_id[:16],
    }
    payload = {
        "iss": ident.atom_id,
        "sub": account_id,
        "email": email or "",
        "iat": now,
        "exp": now + int(ttl_sec),
        "ver": ACCOUNT_JWT_VERSION,
    }
    header_b = _json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    payload_b = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signing_input = _b64url_encode(header_b) + "." + _b64url_encode(payload_b)
    signature = sign(priv_raw, signing_input.encode("ascii"))
    token = signing_input + "." + _b64url_encode(signature)
    return {"token": token, "header": header, "payload": payload}


async def verify_account_jwt(pool, token: str) -> dict:
    """Verify an IAMINE JWT and return the decoded payload.

    Resolves the issuer (`iss`) via federation_peers lookup (must be a known
    bonded peer). Rejects expired tokens. Rejects unknown alg/typ.
    Returns dict with:
        - valid : bool
        - payload : dict (if valid)
        - reason : str (if invalid)
    """
    import json as _json
    import time as _time

    if not token or not isinstance(token, str):
        return {"valid": False, "reason": "empty token"}

    parts = token.split(".")
    if len(parts) != 3:
        return {"valid": False, "reason": "malformed token (expected 3 parts)"}

    try:
        header = _json.loads(_b64url_decode(parts[0]).decode())
        payload = _json.loads(_b64url_decode(parts[1]).decode())
        signature = _b64url_decode(parts[2])
    except Exception as e:
        return {"valid": False, "reason": f"base64/json decode failed: {e}"}

    if header.get("alg") != ACCOUNT_JWT_ALG:
        return {"valid": False, "reason": f"unsupported alg: {header.get('alg')}"}
    if header.get("typ") != ACCOUNT_JWT_TYP:
        return {"valid": False, "reason": f"unsupported typ: {header.get('typ')}"}

    iss = payload.get("iss")
    exp = payload.get("exp")
    sub = payload.get("sub")
    ver = payload.get("ver")

    if not iss:
        return {"valid": False, "reason": "missing iss"}
    if not sub:
        return {"valid": False, "reason": "missing sub"}
    if ver != ACCOUNT_JWT_VERSION:
        return {"valid": False, "reason": f"unsupported jwt version: {ver}"}
    if not isinstance(exp, int):
        return {"valid": False, "reason": "missing or bad exp"}
    if exp < int(_time.time()):
        return {"valid": False, "reason": "token expired"}

    # Self-issued tokens : verify with our own pubkey shortcut.
    ident = getattr(pool, "federation_self", None)
    self_atom_id = ident.atom_id if ident else None
    if iss == self_atom_id:
        issuer_pubkey = ident.pubkey
    else:
        peer = await load_peer(pool, iss)
        if not peer:
            return {"valid": False, "reason": f"issuer not a known peer: {iss[:16]}..."}
        if peer.get("revoked_at"):
            return {"valid": False, "reason": "issuer revoked"}
        issuer_pubkey = bytes(peer["pubkey"])

    signing_input = (parts[0] + "." + parts[1]).encode("ascii")
    if not verify(issuer_pubkey, signature, signing_input):
        return {"valid": False, "reason": "signature verification failed"}

    return {
        "valid": True,
        "payload": payload,
        "header": header,
        "issuer_atom_id": iss,
        "account_id": sub,
    }


async def _nonce_cleanup_loop(pool) -> None:
    """Background task: periodically purge expired nonces.

    Nonces older than 2x NONCE_WINDOW_SEC are safe to delete — no signature
    emitted in that timeframe could still be accepted anyway. Prevents
    unbounded growth of federation_nonces under load.
    """
    import asyncio as _asyncio
    interval = NONCE_WINDOW_SEC * 2  # 120s default
    log.info(f"nonce cleanup loop starting (interval={interval}s)")
    while True:
        try:
            if get_mode() == FED_MODE_OFF:
                await _asyncio.sleep(interval)
                continue
            if hasattr(pool.store, 'pool') and pool.store.pool:
                async with pool.store.pool.acquire() as conn:
                    res = await conn.execute(
                        "DELETE FROM federation_nonces "
                        "WHERE seen_at < NOW() - make_interval(secs => $1)",
                        interval,
                    )
                if res and res != "DELETE 0":
                    log.info(f"nonce cleanup: {res}")
        except Exception as e:
            log.warning(f"nonce cleanup error (non-fatal): {e}")
        await _asyncio.sleep(interval)


async def mark_peer_seen(pool, atom_id: str) -> None:
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE federation_peers SET last_seen = NOW() WHERE atom_id = $1",
            atom_id,
        )


# ---- Q5 D-ANTI-DUMPING : rate minimum helpers (auto-trigger at N>20) ----
#
# Decision 2026-04-11 (david arbitration) :
#   Phase 1 (N <= 20 peers) : (a) marche libre, pas de rate minimum.
#   Phase 2 (N > 20 peers)  : (b) rate minimum configurable via pool_config.
#                             Auto-triggered, pas de decision humaine reactive.
#
# Guardian : le principe "no gift" est protege par la transition automatique.
# Voir project_decisions_a_tranchees.md Q5.

ANTI_DUMPING_THRESHOLD_PEERS = 20  # scaffold invariant : above this, enforce rate


async def get_bonded_peer_count(pool) -> int:
    """Count non-revoked peers in federation_peers (bonded set)."""
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return 0
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*)::BIGINT AS n FROM federation_peers WHERE revoked_at IS NULL"
        )
    return int(row["n"] or 0)


async def get_effective_anti_dumping_min_rate(pool) -> dict:
    """Return the effective anti-dumping min rate and enforcement state.

    - phase 1 (peers <= threshold) : enforced=False, rate=None
    - phase 2 (peers > threshold)  : enforced=True, rate=pool_config.anti_dumping_min_rate
                                      (may still be None if not set, with enforced=True
                                       to signal the admin should set it)

    Returns dict with : enforced, rate, bonded_peers, threshold, phase.
    """
    n = await get_bonded_peer_count(pool)
    enforced = n > ANTI_DUMPING_THRESHOLD_PEERS
    rate = None
    if enforced and hasattr(pool.store, "pool") and pool.store.pool:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM pool_config WHERE key = $1",
                "anti_dumping_min_rate",
            )
        if row and row["value"]:
            try:
                rate = float(row["value"])
            except (TypeError, ValueError):
                rate = None
    return {
        "enforced": enforced,
        "rate": rate,
        "bonded_peers": n,
        "threshold": ANTI_DUMPING_THRESHOLD_PEERS,
        "phase": 2 if enforced else 1,
    }


# ---- Live capabilities from current workers ----
# Computed on-the-fly for /info and /molecule. The static DB column
# federation_self.capabilities is treated as fallback/override.

def _model_stem(model_path: str) -> str:
    """Extract a human-friendly model identifier from a GGUF path."""
    if not model_path:
        return "unknown"
    import os as _os
    name = _os.path.basename(model_path)
    for suffix in (".gguf", ".bin"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    for part in ("-Q4_K_M", "-Q4_K_S", "-Q5_K_M", "-Q8_0", "-F16"):
        if part in name:
            name = name.replace(part, "")
    if "_" in name:
        head, _, rest = name.partition("_")
        if head.lower() in ("qwen", "meta", "mistral", "google"):
            name = rest
    return name.lower()


def compute_live_capabilities(pool) -> list:
    """Build a capability list from the currently connected workers.

    Returns a list of dicts grouped by model stem. Never raises.
    """
    try:
        workers = getattr(pool, "workers", {}) or {}
    except Exception:
        return []

    agg = {}
    for w in workers.values():
        try:
            info = getattr(w, "info", {}) or {}
            model_path = info.get("model_path", "") or ""
            stem = _model_stem(model_path)
            if stem == "unknown":
                continue
            tps = info.get("bench_tps") or 0
            entry = agg.setdefault(stem, {
                "kind": "llm.chat",
                "model": stem,
                "version": "v1",
                "worker_count": 0,
                "max_tps": 0.0,
            })
            entry["worker_count"] += 1
            try:
                if float(tps) > entry["max_tps"]:
                    entry["max_tps"] = float(tps)
            except (TypeError, ValueError):
                pass
        except Exception:
            continue
    return list(agg.values())


# ---- M5 : admin operations on peers ----
# trust_level 3 (bonded) HARD-LOCKED jusqu'à M10 settlement protocol.
# Enforcement code, pas juste convention. Voir project_m5_handshake_decisions.md.

TRUST_LEVEL_MAX_M5 = 2


async def promote_peer(pool, atom_id: str, target_level: int):
    """Promote a peer. Returns (ok: bool, message: str)."""
    if target_level < 0 or target_level > 3:
        return False, f"invalid trust level {target_level} (must be 0-3)"
    if target_level > TRUST_LEVEL_MAX_M5:
        return False, "trust level 3 (bonded) requires M10 settlement protocol - not yet available"
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return False, "no DB store"
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT trust_level, revoked_at FROM federation_peers WHERE atom_id=$1",
            atom_id,
        )
        if not row:
            return False, "peer not found"
        if row["revoked_at"] is not None:
            return False, "peer is revoked"
        if row["trust_level"] >= target_level:
            return True, f"already at trust_level={row['trust_level']}"
        await conn.execute(
            "UPDATE federation_peers SET trust_level=$1, last_seen=NOW() WHERE atom_id=$2",
            target_level, atom_id,
        )
    log.info(f"peer {atom_id[:16]}... promoted {row['trust_level']}→{target_level}")
    return True, f"promoted {row['trust_level']}→{target_level}"


async def demote_peer(pool, atom_id: str, target_level: int):
    if target_level < 0 or target_level > TRUST_LEVEL_MAX_M5:
        return False, f"invalid target level {target_level}"
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return False, "no DB store"
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT trust_level FROM federation_peers WHERE atom_id=$1",
            atom_id,
        )
        if not row:
            return False, "peer not found"
        if row["trust_level"] <= target_level:
            return True, f"already at trust_level={row['trust_level']}"
        await conn.execute(
            "UPDATE federation_peers SET trust_level=$1 WHERE atom_id=$2",
            target_level, atom_id,
        )
    log.info(f"peer {atom_id[:16]}... demoted {row['trust_level']}→{target_level}")
    return True, f"demoted {row['trust_level']}→{target_level}"


async def revoke_peer(pool, atom_id: str):
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return False, "no DB store"
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT revoked_at FROM federation_peers WHERE atom_id=$1",
            atom_id,
        )
        if not row:
            return False, "peer not found"
        if row["revoked_at"] is not None:
            return True, "already revoked"
        await conn.execute(
            "UPDATE federation_peers SET revoked_at=NOW(), trust_level=0 WHERE atom_id=$1",
            atom_id,
        )
    log.warning(f"peer {atom_id[:16]}... REVOKED")
    return True, "revoked"


# ---- M5 : outbound handshake (pool signs, initiates) ----
# Cap global sur back-handshakes in-flight (garde DoS).

_RECIPROCATION_SEM_SIZE = 64
_reciprocation_sem = None


def _get_reciprocation_sem():
    import asyncio as _asyncio
    global _reciprocation_sem
    if _reciprocation_sem is None:
        _reciprocation_sem = _asyncio.Semaphore(_RECIPROCATION_SEM_SIZE)
    return _reciprocation_sem


def build_self_handshake_payload(pool, request_reciprocation: bool = False) -> dict:
    ident = pool.federation_self
    if ident is None:
        raise RuntimeError("federation_self not initialized")
    return {
        "atom_id": ident.atom_id,
        "name": ident.name,
        "pubkey_hex": ident.pubkey.hex(),
        "url": ident.url,
        "molecule_id": ident.molecule_id,
        "capabilities": ident.capabilities,
        "request_reciprocation": bool(request_reciprocation),
    }


# ---- M6 : unified enforcement helper ----
# Validé molecule-guardian 2026-04-10 : per-route helper (PAS de middleware
# FastAPI global) pour limiter le blast radius d'un bug. Un helper KO = 1 endpoint
# cassé. Un middleware global KO = tout le pool. Aligné doctrine "toujours répondre".

def get_effective_mode(pool) -> str:
    """Compute effective mode at runtime. Kill switch wins over env var."""
    if is_fed_disabled_by_fs():
        return FED_MODE_OFF
    return getattr(pool, "federation_mode", FED_MODE_OFF)


async def enforce_fed_policy(
    pool,
    request,
    method: str,
    path: str,
    body: bytes,
    peer_pubkey: Optional[bytes] = None,
    require_signature: bool = True,
):
    """Unified federation enforcement for a signed endpoint.

    Returns a tuple (reject_dict_or_none, sig_ok).
    - reject_dict : None if request passes, else {error, status_code} to return
    - sig_ok : True if envelope signature verified OK, False otherwise.
      Only meaningful when require_signature=True and peer_pubkey is provided.

    The caller can use sig_ok to decide downstream trust_level (e.g. in observe
    mode, a peer with invalid sig is still recorded but at trust_level=0).

    Policy priority:
    1. Kill switch FS present → reject 503
    2. Effective mode == off → reject 503
    3. If require_signature and peer_pubkey supplied → verify envelope ONCE
       - active mode: invalid → reject 401
       - observe mode: invalid → log warning, pass (sig_ok=False)

    IMPORTANT : this helper consumes the nonce (anti-replay side effect).
    Callers MUST NOT re-run verify_envelope on the same request.
    """
    if is_fed_disabled_by_fs():
        log.warning(f"kill switch active, rejecting {method} {path}")
        try:
            from . import federation_metrics as _fm
            _fm.killswitch_reject(path)
        except Exception:
            pass
        return (
            {"error": "federation disabled by kill switch (/etc/iamine/fed_disable)", "status_code": 503},
            False,
        )

    effective_mode = get_effective_mode(pool)
    if effective_mode == FED_MODE_OFF:
        return ({"error": "federation off", "status_code": 503}, False)

    if not require_signature:
        return (None, True)

    if peer_pubkey is None:
        # Caller couldn't determine the peer yet (shouldn't happen for handshake
        # since we extract pubkey from body before calling). Pass through.
        return (None, False)

    headers = {k: v for k, v in request.headers.items()}
    err = await verify_envelope(pool, peer_pubkey, method, path, headers, body)
    if err is None:
        return (None, True)

    client_host = request.client.host if request.client else "unknown"
    log.warning(
        f"enforce_fed_policy: signature invalid on {method} {path} "
        f"from {client_host}: {err} (mode={effective_mode})"
    )
    try:
        from . import federation_metrics as _fm
        _fm.signature_reject(path)
    except Exception:
        pass
    if effective_mode == FED_MODE_ACTIVE:
        return ({"error": f"signature: {err}", "status_code": 401}, False)
    # observe: log-only, pass through, sig_ok=False for downstream logic
    return (None, False)


async def outbound_handshake_to(pool, target_url: str, reciprocate: bool = False, name_hint: Optional[str] = None) -> dict:
    """Drive an outbound handshake to a remote pool.

    Steps:
    1. GET <target_url>/v1/federation/info
    2. Verify atom_id == sha256(pubkey_hex)
    3. Sign handshake payload with self privkey
    4. POST to <target_url>/v1/federation/handshake
    5. Upsert target as peer locally at trust_level=1
    """
    import json as _json
    import aiohttp

    if pool.federation_self is None:
        return {"ok": False, "message": "self identity not initialized", "http_status": 0}

    base = target_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "message": "target_url must be http(s)://", "http_status": 0}

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/v1/federation/info") as resp:
                if resp.status != 200:
                    return {"ok": False, "message": f"/info returned {resp.status}", "http_status": resp.status}
                info = await resp.json()
    except Exception as e:
        return {"ok": False, "message": f"/info fetch failed: {e}", "http_status": 0}

    for k in ("atom_id", "pubkey_hex", "name", "url"):
        if k not in info:
            return {"ok": False, "message": f"/info missing field: {k}", "http_status": 0}

    try:
        target_pubkey = bytes.fromhex(info["pubkey_hex"])
    except ValueError:
        return {"ok": False, "message": "invalid pubkey_hex in /info", "http_status": 0}
    if len(target_pubkey) != 32:
        return {"ok": False, "message": "pubkey must be 32 bytes", "http_status": 0}

    expected_id = _atom_id_from_pubkey(target_pubkey)
    if expected_id != info["atom_id"]:
        log.warning(
            f"outbound handshake: atom_id mismatch at {base} — "
            f"expected {expected_id[:16]}... got {info['atom_id'][:16]}..."
        )
        return {"ok": False, "message": "atom_id does not match sha256(pubkey) — possible MITM", "http_status": 0}

    if info["atom_id"] == pool.federation_self.atom_id:
        return {"ok": False, "message": "refuse to handshake with self", "http_status": 0}

    payload = build_self_handshake_payload(pool, request_reciprocation=reciprocate)
    body = _json.dumps(payload).encode()

    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return {"ok": False, "message": "privkey unavailable", "http_status": 0}

    headers = build_envelope_headers(
        priv_raw, pool.federation_self.atom_id, "POST", "/v1/federation/handshake",
        body, hop=0, chain=[],
    )
    headers["Content-Type"] = "application/json"

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base}/v1/federation/handshake", data=body, headers=headers) as resp:
                status = resp.status
                resp_text = await resp.text()
                if status != 200:
                    return {"ok": False, "message": f"handshake POST returned {status}: {resp_text[:200]}", "http_status": status}
                try:
                    resp_json = _json.loads(resp_text)
                except Exception:
                    resp_json = {}
    except Exception as e:
        return {"ok": False, "message": f"handshake POST failed: {e}", "http_status": 0}

    await upsert_peer(
        pool,
        atom_id=info["atom_id"],
        name=name_hint or info.get("name") or "unnamed",
        pubkey=target_pubkey,
        url=info.get("url") or base,
        molecule_id=info.get("molecule_id"),
        capabilities=info.get("capabilities") or [],
        trust_level=1,
        added_by="handshake_initial",
    )

    try:
        from . import federation_metrics as _fm
        _fm.handshake_ok()
    except Exception:
        pass
    log.info(
        f"outbound handshake ok: {info['atom_id'][:16]}... ({info.get('name')!r}) "
        f"reciprocate={reciprocate} signature_verified={resp_json.get('signature_verified')}"
    )
    return {
        "ok": True,
        "target_atom_id": info["atom_id"],
        "target_name": info.get("name"),
        "http_status": 200,
        "target_trust_level_on_us": resp_json.get("peer_trust_level"),
        "signature_verified_by_target": resp_json.get("signature_verified"),
        "message": "handshake complete, peer stored at trust_level=1",
    }


# ---- Persist / load self identity from DB ----

async def _persist_self(pool, ident: SelfIdentity) -> None:
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        log.warning("No PG store — federation_self not persisted")
        return
    import json as _json
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO federation_self (atom_id, name, pubkey, privkey_path, url, molecule_id, capabilities)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (atom_id) DO UPDATE SET
                name = EXCLUDED.name,
                url = EXCLUDED.url,
                molecule_id = EXCLUDED.molecule_id,
                capabilities = EXCLUDED.capabilities
            """,
            ident.atom_id,
            ident.name,
            ident.pubkey,
            ident.privkey_path,
            ident.url,
            ident.molecule_id,
            _json.dumps(ident.capabilities),
        )


async def _load_self(pool) -> Optional[SelfIdentity]:
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return None
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT atom_id, name, pubkey, privkey_path, url, molecule_id, capabilities FROM federation_self LIMIT 1")
    if not row:
        return None
    caps = row["capabilities"]
    if isinstance(caps, str):
        import json as _json
        caps = _json.loads(caps)
    return SelfIdentity(
        atom_id=row["atom_id"],
        name=row["name"],
        pubkey=bytes(row["pubkey"]),
        privkey_path=row["privkey_path"],
        url=row["url"],
        molecule_id=row["molecule_id"],
        capabilities=caps or [],
    )


async def initialize_federation(pool) -> None:
    """Bootstrap federation identity at pool startup.

    No-op if IAMINE_FED=off. Otherwise: load or generate Ed25519 keypair,
    persist federation_self row, attach SelfIdentity to pool.federation_self.
    """
    mode = get_mode()
    if mode == FED_MODE_OFF:
        pool.federation_self = None
        pool.federation_mode = FED_MODE_OFF
        log.info("federation: IAMINE_FED=off, skipping keygen")
        return

    name = os.environ.get("IAMINE_FED_NAME") or os.environ.get("HOSTNAME") or "unnamed-pool"
    url = os.environ.get("IAMINE_FED_URL", "")
    molecule_id = os.environ.get("IAMINE_FED_MOLECULE")

    key_dir = _key_dir()
    key_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_dir, 0o700)
    except Exception:
        pass
    priv_path = key_dir / _PRIVKEY_NAME

    priv_raw = _load_privkey_from_disk(priv_path)
    if priv_raw is None:
        log.info(f"federation: generating new Ed25519 keypair at {priv_path}")
        priv_raw, pub_raw = _generate_keypair()
        priv_path.write_bytes(priv_raw)
        try:
            os.chmod(priv_path, 0o600)
        except Exception:
            pass
    else:
        pub_raw = _pubkey_from_privkey(priv_raw)

    atom_id = _atom_id_from_pubkey(pub_raw)

    # Try to load existing row first — keeps capabilities/molecule_id if already set in DB
    existing = await _load_self(pool)
    if existing and existing.atom_id == atom_id:
        # Only update volatile fields (name, url) if changed via env
        if name and existing.name != name:
            existing.name = name
        if url and existing.url != url:
            existing.url = url
        if molecule_id and existing.molecule_id != molecule_id:
            existing.molecule_id = molecule_id
        ident = existing
    else:
        ident = SelfIdentity(
            atom_id=atom_id,
            name=name,
            pubkey=pub_raw,
            privkey_path=str(priv_path),
            url=url,
            molecule_id=molecule_id,
            capabilities=[],
        )

    await _persist_self(pool, ident)

    pool.federation_self = ident
    pool.federation_mode = mode

    # M6 — log enforcement state au boot (diagnostic à chaud)
    kill_switch = is_fed_disabled_by_fs()
    log.info(
        f"federation: mode={mode} atom_id={atom_id[:16]}... name={ident.name!r} "
        f"molecule={ident.molecule_id or 'standalone'} "
        f"kill_switch={'present' if kill_switch else 'absent'}"
    )

    # M7b — start peer heartbeat loop (background task)
    if mode != FED_MODE_OFF:
        import asyncio as _asyncio
        from . import federation_heartbeat
        _asyncio.create_task(federation_heartbeat.heartbeat_loop(pool))
        log.info("federation: peer heartbeat loop scheduled")

        # M10-scaffold : schedule settlement loop (internally gated by
        # SETTLEMENT_ENABLED env var, default false). Safe to schedule
        # unconditionally — the loop exits immediately when disabled.
        from . import federation_settlement
        _asyncio.create_task(federation_settlement.settlement_loop(pool))
        _asyncio.create_task(_nonce_cleanup_loop(pool))
        log.info("federation: nonce cleanup loop scheduled")
        log.info(
            f"federation: settlement loop scheduled "
            f"(enabled={federation_settlement.is_settlement_enabled()} "
            f"mode={federation_settlement.get_settlement_mode()})"
        )
