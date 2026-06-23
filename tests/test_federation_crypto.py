"""Tests de caracterisation — crypto fediration (audit 2026-06-22, priorite #1).

Verrouille le comportement ACTUEL des primitives load-bearing de iamine.core.federation :
signature Ed25519, fenetre hop/forward-chain, et forme canonique de l'enveloppe
signee (toute derive silencieuse casse l'interop inter-pools sans alerte).
DB-free : fonctions pures.
"""
import pytest

from iamine.core import federation as F

# Seed Ed25519 fixe (32 octets) -> golden deterministe.
SEED = bytes(range(32))


# --- signature Ed25519 -------------------------------------------------------

def test_sign_verify_roundtrip():
    pub = F._pubkey_from_privkey(SEED)
    sig = F.sign(SEED, b"hello")
    assert F.verify(pub, sig, b"hello") is True


def test_verify_rejects_tampered_message():
    pub = F._pubkey_from_privkey(SEED)
    sig = F.sign(SEED, b"hello")
    assert F.verify(pub, sig, b"hellp") is False


def test_verify_rejects_tampered_signature():
    pub = F._pubkey_from_privkey(SEED)
    sig = bytearray(F.sign(SEED, b"hello"))
    sig[0] ^= 0x01
    assert F.verify(pub, bytes(sig), b"hello") is False


def test_verify_rejects_wrong_pubkey():
    other_pub = F._pubkey_from_privkey(bytes([0xAA]) * 32)
    sig = F.sign(SEED, b"hello")
    assert F.verify(other_pub, sig, b"hello") is False


def test_verify_never_raises_on_garbage():
    # verify avale toute exception et renvoie False (jamais d'exception qui fuit).
    assert F.verify(b"too-short", b"bad", b"msg") is False


# --- hop counter + forward chain (R1) ---------------------------------------

def test_hop_ok_cases():
    assert F.envelope_check_hop(0, [], "X") is None
    assert F.envelope_check_hop(1, ["A"], "X") is None
    assert F.envelope_check_hop(2, ["A", "B"], "X") is None  # HOP_MAX = 2


def test_hop_out_of_range():
    assert "hop out of range" in F.envelope_check_hop(-1, [], "X")
    assert "hop out of range" in F.envelope_check_hop(3, ["A", "B", "C"], "X")


def test_hop_loop_detection_takes_precedence_over_length():
    # self dans la chaine -> loop, meme si len(chain)==hop (verifie l'ordre).
    res = F.envelope_check_hop(1, ["X"], "X")
    assert res is not None and "loop detected" in res


def test_hop_chain_length_inconsistency():
    assert "inconsistent" in F.envelope_check_hop(1, [], "X")
    assert "inconsistent" in F.envelope_check_hop(0, ["A"], "X")


def test_envelope_bump():
    hop, chain = F.envelope_bump(1, ["A"], "X")
    assert hop == 2
    assert chain == ["A", "X"]


# --- forme canonique de l'enveloppe (golden) --------------------------------

# Octets EXACTS signes/verifies pour des entrees fixes. Geler ce golden empeche
# toute modification silencieuse de la canonical form (= rupture d'interop).
ENV_GOLDEN_HEX = "504f53540a2f76312f780a313730300a6e6f6e6365310a310a706f6f6c410a424f4459"


def test_canonical_envelope_body_golden():
    out = F.canonical_envelope_body("post", "/v1/x", "1700", "nonce1", "1", "poolA", b"BODY")
    assert out == bytes.fromhex(ENV_GOLDEN_HEX)


def test_canonical_envelope_uppercases_method():
    lower = F.canonical_envelope_body("post", "/p", "1", "n", "0", "", b"")
    upper = F.canonical_envelope_body("POST", "/p", "1", "n", "0", "", b"")
    assert lower == upper
    assert lower.startswith(b"POST\n")


def test_canonical_envelope_deterministic():
    args = ("GET", "/v1/status", "1700", "n2", "0", "", b"abc")
    assert F.canonical_envelope_body(*args) == F.canonical_envelope_body(*args)
