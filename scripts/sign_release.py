#!/usr/bin/env python3
"""Sign a release artifact (wheel or Docker image digest) with an Ed25519
maintainer key. Produces a sidecar `.sig` file next to the artifact.

Usage:
    sign_release.py sign <artifact_path> --seed <path_to_seed> --signer <nickname>
    sign_release.py verify <artifact_path> [--maintainers <path>]
    sign_release.py sign-digest <sha256_hex> --artifact-name <name> --seed <path> --signer <nick>

The signed manifest is a canonical string :
    artifact: <name>\n
    sha256: <hex>\n
    signed_at: <iso8601>\n
    signer: <nickname>\n
    pubkey: <hex>\n

The .sig file contains (newline-separated key: value) :
    # Cellule.ai release signature v1
    artifact: ...
    sha256: ...
    signed_at: ...
    signer: ...
    pubkey: ...
    signature: <hex_64_bytes>
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import pathlib
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature


SIG_VERSION = "1"


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_manifest(artifact: str, sha256: str, signed_at: str,
                        signer: str, pubkey_hex: str) -> bytes:
    return (
        f"artifact: {artifact}\n"
        f"sha256: {sha256}\n"
        f"signed_at: {signed_at}\n"
        f"signer: {signer}\n"
        f"pubkey: {pubkey_hex}\n"
    ).encode()


def _load_seed(path: pathlib.Path) -> Ed25519PrivateKey:
    raw = path.read_bytes()
    if len(raw) != 32:
        sys.exit(f"seed file must be exactly 32 bytes, got {len(raw)}")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _load_maintainers(path: pathlib.Path) -> dict[str, str]:
    """Return {nickname: pubkey_hex} from MAINTAINERS."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        nickname, pubkey_hex = parts[0], parts[1]
        if len(pubkey_hex) != 64:
            continue
        out[nickname] = pubkey_hex
    return out


def cmd_sign(args) -> int:
    artifact = pathlib.Path(args.artifact)
    if not artifact.exists():
        sys.exit(f"artifact not found: {artifact}")
    priv = _load_seed(pathlib.Path(args.seed))
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    pubkey_hex = pub_raw.hex()

    sha = _sha256_file(artifact)
    signed_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = _canonical_manifest(
        artifact.name, sha, signed_at, args.signer, pubkey_hex,
    )
    sig = priv.sign(manifest).hex()

    sig_path = artifact.with_suffix(artifact.suffix + ".sig")
    sig_path.write_text(
        f"# Cellule.ai release signature v{SIG_VERSION}\n"
        + manifest.decode()
        + f"signature: {sig}\n"
    )
    print(f"signed {artifact.name} -> {sig_path.name}")
    print(f"  sha256:    {sha}")
    print(f"  signed_at: {signed_at}")
    print(f"  signer:    {args.signer}")
    print(f"  pubkey:    {pubkey_hex}")
    return 0


def cmd_sign_digest(args) -> int:
    """Sign a digest directly (useful for Docker image manifests)."""
    sha = args.digest.lower().removeprefix("sha256:")
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        sys.exit("digest must be 64 hex chars (sha256)")

    priv = _load_seed(pathlib.Path(args.seed))
    pubkey_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    ).hex()

    signed_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = _canonical_manifest(
        args.artifact_name, sha, signed_at, args.signer, pubkey_hex,
    )
    sig = priv.sign(manifest).hex()

    out = pathlib.Path(args.output or f"{args.artifact_name}.sig")
    out.write_text(
        f"# Cellule.ai release signature v{SIG_VERSION}\n"
        + manifest.decode()
        + f"signature: {sig}\n"
    )
    print(f"signed digest {sha[:16]}... -> {out}")
    return 0


def cmd_verify(args) -> int:
    artifact = pathlib.Path(args.artifact)
    sig_path = artifact.with_suffix(artifact.suffix + ".sig")
    if not sig_path.exists():
        print(f"NO SIGNATURE: {sig_path.name} missing")
        return 2

    fields: dict[str, str] = {}
    for line in sig_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()

    required = ("artifact", "sha256", "signed_at", "signer", "pubkey", "signature")
    for k in required:
        if k not in fields:
            print(f"INVALID: missing field {k}")
            return 3

    actual_sha = _sha256_file(artifact)
    if actual_sha != fields["sha256"]:
        print(f"SHA MISMATCH: file={actual_sha[:16]}... sig={fields['sha256'][:16]}...")
        return 4

    manifest = _canonical_manifest(
        fields["artifact"], fields["sha256"], fields["signed_at"],
        fields["signer"], fields["pubkey"],
    )

    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(fields["pubkey"]))
        pub.verify(bytes.fromhex(fields["signature"]), manifest)
    except (InvalidSignature, ValueError) as e:
        print(f"INVALID SIGNATURE: {e}")
        return 5

    maintainers_path = pathlib.Path(args.maintainers or "MAINTAINERS")
    if not maintainers_path.exists():
        print(f"VERIFIED (pubkey not cross-checked: {maintainers_path} absent)")
        return 0

    maint = _load_maintainers(maintainers_path)
    expected = maint.get(fields["signer"])
    if expected is None:
        print(f"UNKNOWN SIGNER: '{fields['signer']}' not in {maintainers_path.name}")
        return 6
    if expected.lower() != fields["pubkey"].lower():
        print(f"KEY MISMATCH: {fields['signer']} in MAINTAINERS has different pubkey")
        return 7

    print(f"VERIFIED {artifact.name} signed by {fields['signer']} at {fields['signed_at']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Cellule.ai release signing")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("sign", help="Sign a file artifact")
    ps.add_argument("artifact")
    ps.add_argument("--seed", required=True)
    ps.add_argument("--signer", required=True)
    ps.set_defaults(func=cmd_sign)

    psd = sub.add_parser("sign-digest", help="Sign a sha256 digest (e.g. Docker image)")
    psd.add_argument("digest")
    psd.add_argument("--artifact-name", required=True)
    psd.add_argument("--seed", required=True)
    psd.add_argument("--signer", required=True)
    psd.add_argument("--output", default=None)
    psd.set_defaults(func=cmd_sign_digest)

    pv = sub.add_parser("verify", help="Verify an artifact signature")
    pv.add_argument("artifact")
    pv.add_argument("--maintainers", default=None)
    pv.set_defaults(func=cmd_verify)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
