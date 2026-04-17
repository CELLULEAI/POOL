"""Release signature verification at pool boot.

Worker-first invariant : verification is WARNING-only. A missing or invalid
signature NEVER prevents the pool from starting — a lost signing key must
not take workers offline. Operators who want strict verification can set
IAMINE_STRICT_SIGNING=1, which turns the warning into a fatal exit.

See docs/GOVERNANCE.md for the full rationale.
"""
from __future__ import annotations

import logging
import os
import pathlib
from typing import Optional

log = logging.getLogger("iamine.signing")


def _load_maintainers(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2 or len(parts[1]) != 64:
                continue
            out[parts[0]] = parts[1].lower()
    except Exception as e:
        log.debug(f"MAINTAINERS unreadable: {e}")
    return out


def _find_maintainers_file() -> Optional[pathlib.Path]:
    """Locate MAINTAINERS shipped alongside the package or at a conventional path."""
    candidates = [
        pathlib.Path(__file__).resolve().parent.parent.parent / "MAINTAINERS",
        pathlib.Path("/opt/cellule/MAINTAINERS"),
        pathlib.Path("/etc/iamine/MAINTAINERS"),
    ]
    env_path = os.environ.get("IAMINE_MAINTAINERS")
    if env_path:
        candidates.insert(0, pathlib.Path(env_path))
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_release_sig() -> Optional[pathlib.Path]:
    env_path = os.environ.get("IAMINE_RELEASE_SIG")
    if env_path:
        p = pathlib.Path(env_path)
        return p if p.exists() else None
    for p in (
        pathlib.Path("/opt/cellule/release.sig"),
        pathlib.Path(__file__).resolve().parent.parent.parent / "release.sig",
    ):
        if p.exists():
            return p
    return None


def _parse_sig_file(path: pathlib.Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    return fields


def _canonical_manifest(fields: dict[str, str]) -> bytes:
    return (
        f"artifact: {fields['artifact']}\n"
        f"sha256: {fields['sha256']}\n"
        f"signed_at: {fields['signed_at']}\n"
        f"signer: {fields['signer']}\n"
        f"pubkey: {fields['pubkey']}\n"
    ).encode()


def verify_release_at_boot() -> dict:
    """Verify the running release signature. Returns a status dict.

    Behavior contract :
      - Returns {'status': 'verified'|'unsigned'|'invalid', ...}
      - Logs INFO on verified, WARNING on unsigned/invalid.
      - If IAMINE_STRICT_SIGNING=1 and status != 'verified', exits the process.
      - Never raises.
    """
    strict = os.environ.get("IAMINE_STRICT_SIGNING", "0") == "1"

    sig_path = _find_release_sig()
    if sig_path is None:
        msg = ("release signature not found — running unverified code. "
               "Set IAMINE_STRICT_SIGNING=1 to require signatures.")
        log.warning(msg)
        if strict:
            raise SystemExit("IAMINE_STRICT_SIGNING=1 but no release.sig found")
        return {"status": "unsigned", "reason": "no_sig_file"}

    try:
        fields = _parse_sig_file(sig_path)
        required = ("artifact", "sha256", "signed_at", "signer", "pubkey", "signature")
        for k in required:
            if k not in fields:
                raise ValueError(f"missing field: {k}")

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(fields["pubkey"]))
        pub.verify(bytes.fromhex(fields["signature"]), _canonical_manifest(fields))

        artifact_path_env = os.environ.get("IAMINE_RELEASE_ARTIFACT")
        if artifact_path_env:
            artifact_path = pathlib.Path(artifact_path_env)
            if artifact_path.exists():
                import hashlib
                h = hashlib.sha256()
                with artifact_path.open("rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                actual = h.hexdigest()
                if actual != fields["sha256"].lower():
                    log.warning(
                        f"artifact sha256 mismatch : file={actual[:16]}... "
                        f"sig={fields['sha256'][:16]}... — running tampered code"
                    )
                    if strict:
                        raise SystemExit(
                            "IAMINE_STRICT_SIGNING=1 and artifact sha256 mismatch"
                        )
                    return {"status": "invalid", "reason": "sha256_mismatch",
                            "expected": fields["sha256"], "actual": actual}

        maintainers_path = _find_maintainers_file()
        cross_checked = False
        if maintainers_path is not None:
            maint = _load_maintainers(maintainers_path)
            expected = maint.get(fields["signer"])
            if expected is None:
                log.warning(f"release signed by unknown signer '{fields['signer']}' "
                            f"(not in {maintainers_path})")
                if strict:
                    raise SystemExit(f"IAMINE_STRICT_SIGNING=1 and signer unknown")
                return {"status": "invalid", "reason": "unknown_signer",
                        "signer": fields["signer"]}
            if expected != fields["pubkey"].lower():
                log.warning(f"release signer '{fields['signer']}' key mismatch vs MAINTAINERS")
                if strict:
                    raise SystemExit("IAMINE_STRICT_SIGNING=1 and key mismatch")
                return {"status": "invalid", "reason": "key_mismatch",
                        "signer": fields["signer"]}
            cross_checked = True

        log.info(
            f"release verified : {fields['artifact']} signed by {fields['signer']} "
            f"at {fields['signed_at']}"
            + (" (MAINTAINERS cross-checked)" if cross_checked else " (MAINTAINERS absent)")
        )
        return {
            "status": "verified",
            "artifact": fields["artifact"],
            "signer": fields["signer"],
            "signed_at": fields["signed_at"],
            "cross_checked": cross_checked,
        }

    except (InvalidSignature, ValueError, Exception) as e:
        log.warning(f"release signature invalid or unparseable : {e}")
        if strict:
            raise SystemExit(f"IAMINE_STRICT_SIGNING=1 and signature invalid : {e}")
        return {"status": "invalid", "reason": str(e)[:200]}
