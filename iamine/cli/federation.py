"""M5 — CLI wrapper for federation admin commands.

Modèle B : le CLI n'a PAS de privkey. Il appelle les endpoints admin du pool,
qui signe lui-même avec sa propre privkey. Le CLI est trivialement portable
(laptop → VPS) et ne détient aucun secret crypto.

Authentification : admin_token via IAMINE_ADMIN_TOKEN env var ou --token flag.

Commandes :
    iamine pool register <url> [--reciprocate] [--name NAME]
    iamine pool peers [--all]
    iamine pool show <atom_id>
    iamine pool promote <atom_id> [--level 2]
    iamine pool demote <atom_id> [--level 1]
    iamine pool revoke <atom_id>
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Optional


def _pool_base() -> str:
    return os.environ.get("IAMINE_POOL_URL", "http://127.0.0.1:8080").rstrip("/")


def _admin_token() -> Optional[str]:
    return os.environ.get("IAMINE_ADMIN_TOKEN") or os.environ.get("ADMIN_PASSWORD")


def _call(method: str, path: str, body: Optional[dict] = None, token_override: Optional[str] = None) -> dict:
    """Call a pool endpoint. Admin endpoints require token via query param."""
    base = _pool_base()
    token = token_override or _admin_token()
    url = base + path
    if token and ("admin" in path or "peers" in path):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"

    data = None
    headers = {"User-Agent": "iamine-cli/0.5"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return {"_http_status": e.code, **json.loads(raw)}
        except Exception:
            return {"_http_status": e.code, "error": raw or str(e)}
    except Exception as e:
        return {"_http_status": 0, "error": str(e)}

    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def _print_json(obj: dict) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---- Sub-commands ----

def cmd_pool_register(args) -> None:
    """iamine pool register <url> [--reciprocate] [--name NAME]"""
    token = getattr(args, "token", None) or _admin_token()
    if not token:
        _die("admin token required (IAMINE_ADMIN_TOKEN env or --token)")
    body = {
        "url": args.url,
        "reciprocate": bool(getattr(args, "reciprocate", False)),
    }
    if getattr(args, "name", None):
        body["name"] = args.name
    result = _call("POST", "/v1/federation/admin/register", body=body, token_override=token)
    if not result.get("ok"):
        _print_json(result)
        sys.exit(2)
    print(f"✓ handshake ok : {result.get('target_name')!r} ({result.get('target_atom_id', '')[:16]}...)")
    print(f"  our_trust_level_on_target: {result.get('target_trust_level_on_us')}")
    print(f"  signature_verified_by_target: {result.get('signature_verified_by_target')}")
    if body.get("reciprocate"):
        print("  reciprocation: requested — check `iamine pool peers` in ~5s")


def cmd_pool_peers(args) -> None:
    token = getattr(args, "token", None) or _admin_token()
    if not token:
        _die("admin token required (IAMINE_ADMIN_TOKEN env or --token)")
    path = "/v1/federation/peers"
    if getattr(args, "all", False):
        path += "?include_revoked=1"
    result = _call("GET", path, token_override=token)
    peers = result.get("peers", [])
    if not peers:
        print(f"(aucun peer — mode={result.get('mode')})")
        return
    print(f"mode: {result.get('mode')}")
    print(f"self: {result.get('self', {}).get('name')} ({result.get('self', {}).get('atom_id', '')[:16]}...)")
    print(f"peers ({len(peers)}):")
    for p in peers:
        revoked = " [REVOKED]" if p.get("revoked_at") else ""
        last = (p.get("last_seen") or "never")[:19]
        print(
            f"  - {p['name']} trust={p['trust_level']} "
            f"atom={p['atom_id'][:16]}... url={p['url']} last_seen={last}{revoked}"
        )


def cmd_pool_show(args) -> None:
    token = getattr(args, "token", None) or _admin_token()
    if not token:
        _die("admin token required")
    result = _call("GET", f"/v1/federation/peers/{args.atom_id}", token_override=token)
    _print_json(result)


def cmd_pool_promote(args) -> None:
    token = getattr(args, "token", None) or _admin_token()
    if not token:
        _die("admin token required")
    body = {"target_level": int(getattr(args, "level", 2))}
    result = _call("POST", f"/v1/federation/peers/{args.atom_id}/promote", body=body, token_override=token)
    _print_json(result)
    if not result.get("ok"):
        sys.exit(2)


def cmd_pool_demote(args) -> None:
    token = getattr(args, "token", None) or _admin_token()
    if not token:
        _die("admin token required")
    body = {"target_level": int(getattr(args, "level", 1))}
    result = _call("POST", f"/v1/federation/peers/{args.atom_id}/demote", body=body, token_override=token)
    _print_json(result)
    if not result.get("ok"):
        sys.exit(2)


def cmd_pool_revoke(args) -> None:
    token = getattr(args, "token", None) or _admin_token()
    if not token:
        _die("admin token required")
    result = _call("POST", f"/v1/federation/peers/{args.atom_id}/revoke", token_override=token)
    _print_json(result)
    if not result.get("ok"):
        sys.exit(2)


def add_subparsers(pp_sub):
    """Wire the pool sub-subcommands to an existing `pp_sub` (subparsers of `pool`)."""
    common_url = {"default": None, "help": "Pool base URL (default: $IAMINE_POOL_URL or http://127.0.0.1:8080)"}
    common_token = {"default": None, "help": "Admin token (default: $IAMINE_ADMIN_TOKEN)"}

    p_reg = pp_sub.add_parser("register", help="Register a peer pool (handshake)")
    p_reg.add_argument("url", help="Target pool URL (https://...)")
    p_reg.add_argument("--name", help="Human-readable name hint")
    p_reg.add_argument("--reciprocate", action="store_true", help="Ask target to handshake back")
    p_reg.add_argument("--token", **common_token)
    p_reg.set_defaults(pool_action="register")

    p_peers = pp_sub.add_parser("peers", help="List known peers")
    p_peers.add_argument("--all", action="store_true", help="Include revoked peers")
    p_peers.add_argument("--token", **common_token)
    p_peers.set_defaults(pool_action="peers")

    p_show = pp_sub.add_parser("show", help="Show a peer's full details")
    p_show.add_argument("atom_id")
    p_show.add_argument("--token", **common_token)
    p_show.set_defaults(pool_action="show")

    p_prom = pp_sub.add_parser("promote", help="Promote a peer trust level")
    p_prom.add_argument("atom_id")
    p_prom.add_argument("--level", type=int, default=2, help="Target trust level (max 2 until M10)")
    p_prom.add_argument("--token", **common_token)
    p_prom.set_defaults(pool_action="promote")

    p_dem = pp_sub.add_parser("demote", help="Demote a peer trust level")
    p_dem.add_argument("atom_id")
    p_dem.add_argument("--level", type=int, default=1)
    p_dem.add_argument("--token", **common_token)
    p_dem.set_defaults(pool_action="demote")

    p_rev = pp_sub.add_parser("revoke", help="Revoke a peer (trust 0 + mark revoked)")
    p_rev.add_argument("atom_id")
    p_rev.add_argument("--token", **common_token)
    p_rev.set_defaults(pool_action="revoke")


def dispatch(args) -> bool:
    """Return True if args were handled by federation CLI, False otherwise."""
    action = getattr(args, "pool_action", None)
    if action == "register":
        cmd_pool_register(args); return True
    if action == "peers":
        cmd_pool_peers(args); return True
    if action == "show":
        cmd_pool_show(args); return True
    if action == "promote":
        cmd_pool_promote(args); return True
    if action == "demote":
        cmd_pool_demote(args); return True
    if action == "revoke":
        cmd_pool_revoke(args); return True
    return False
