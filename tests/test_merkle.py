"""Tests de caracterisation — ledger Merkle (audit 2026-06-22).

Verrouille la CANONICAL FORM v1 ("DO NOT CHANGE WITHOUT VERSION BUMP") et les
invariants RFC 6962 du module iamine.core.federation_merkle : preuve d'integrite
du ledger de revenus cross-pool. Inclut la non-regression CVE-2012-2459 (pas de
duplication de la derniere feuille sur compte impair). DB-free : fonctions pures.
"""
import hashlib

from iamine.core import federation_merkle as M

# Row de reference (created_at en int micros -> golden stable, sans datetime/tz).
ROW = dict(
    id=42, job_id="job_abc", origin_pool_id="poolA", exec_pool_id="poolB",
    worker_id="w1", model="qwen3-9b", tokens_in=10, tokens_out=20,
    credits_total=100, credits_worker=60, credits_exec=20, credits_origin=10,
    credits_treasury=10, forward_chain=["poolA", "poolB"], created_at=1700000000000000,
)

# Golden values calculees a partir du code (caracterisation).
CANON_GOLDEN_HEX = (
    "000000000000002a1f6a6f625f6162631f706f6f6c411f706f6f6c421f77311f7177"
    "656e332d39621f000000000000000a1f00000000000000141f00000000000000641f"
    "000000000000003c1f00000000000000141f000000000000000a1f000000000000000a"
    "1f706f6f6c412c706f6f6c421f00060a24181e4000"
)
LEAF_GOLDEN_HEX = "a7ebf334693f1b7e6a5b71e6663c1b8458d69c941342d556da821925fc3a8646"
ROOT3_GOLDEN_HEX = "752492acf6a29da8824124e65d677f7091c2536412b901b30d6fded9960daedb"


def _rows(n):
    out = []
    for i in range(n):
        r = dict(ROW)
        r["id"] = i
        r["job_id"] = f"j{i}"
        out.append(r)
    return out


# --- canonical form (golden) ------------------------------------------------

def test_canonical_row_bytes_golden():
    assert M.canonical_row_bytes(ROW) == bytes.fromhex(CANON_GOLDEN_HEX)


def test_leaf_hash_golden_and_prefix():
    # leaf = sha256(0x00 || canonical)
    assert M.leaf_hash(ROW).hex() == LEAF_GOLDEN_HEX
    expected = hashlib.sha256(b"\x00" + M.canonical_row_bytes(ROW)).digest()
    assert M.leaf_hash(ROW) == expected


def test_node_hash_prefix_and_not_commutative():
    a, b = b"a" * 32, b"b" * 32
    assert M.node_hash(a, b) == hashlib.sha256(b"\x01" + a + b).digest()
    # l'ordre compte (sinon malleabilite de l'arbre)
    assert M.node_hash(a, b) != M.node_hash(b, a)


# --- merkle root: cas de base -----------------------------------------------

def test_empty_root_is_sha256_empty():
    assert M.EMPTY_MERKLE_ROOT == hashlib.sha256(b"").hexdigest()
    assert M.merkle_root_from_leaves([]) == hashlib.sha256(b"").digest()


def test_singleton_is_the_leaf_itself():
    leaves = [M.leaf_hash(r) for r in _rows(1)]
    assert M.merkle_root_from_leaves(leaves) == leaves[0]


def test_two_leaves_is_node_hash():
    leaves = [M.leaf_hash(r) for r in _rows(2)]
    assert M.merkle_root_from_leaves(leaves) == M.node_hash(leaves[0], leaves[1])


def test_root_three_leaves_golden_and_deterministic():
    leaves = [M.leaf_hash(r) for r in _rows(3)]
    root = M.merkle_root_from_leaves(leaves)
    assert root.hex() == ROOT3_GOLDEN_HEX
    assert M.merkle_root_from_leaves(leaves) == root  # deterministe


# --- non-regression CVE-2012-2459 -------------------------------------------

def test_odd_count_does_not_duplicate_last_leaf():
    """Sur un nombre impair de feuilles, la racine NE doit PAS egaler celle
    obtenue en dupliquant la derniere feuille (faille Bitcoin CVE-2012-2459)."""
    leaves = [M.leaf_hash(r) for r in _rows(3)]
    root = M.merkle_root_from_leaves(leaves)
    root_dup_last = M.merkle_root_from_leaves(leaves + [leaves[-1]])
    assert root != root_dup_last
