"""Tests de caracterisation — client_ip anti-spoof (sec-pub-10, audit 2026-06-22).

Verrouille l'ordre de confiance de iamine.core.credits.client_ip : cf-connecting-ip
(non spoofable derriere Cloudflare) prioritaire sur x-real-ip puis x-forwarded-for.
Cette cle sert au throttling guest ; un mauvais ordre rouvrirait le contournement
de quota par en-tete forge.
"""
from iamine.core.credits import client_ip


class FakeReq:
    def __init__(self, headers=None, client_host=None):
        self.headers = headers or {}
        self.client = type("C", (), {"host": client_host})() if client_host is not None else None


def test_prefers_cf_connecting_ip_over_everything():
    req = FakeReq(headers={
        "cf-connecting-ip": "1.1.1.1",
        "x-real-ip": "2.2.2.2",
        "x-forwarded-for": "3.3.3.3",
    }, client_host="4.4.4.4")
    assert client_ip(req) == "1.1.1.1"


def test_falls_back_to_x_real_ip():
    req = FakeReq(headers={"x-real-ip": "2.2.2.2", "x-forwarded-for": "3.3.3.3"})
    assert client_ip(req) == "2.2.2.2"


def test_falls_back_to_first_xff_hop_trimmed():
    req = FakeReq(headers={"x-forwarded-for": "3.3.3.3 , 9.9.9.9"})
    assert client_ip(req) == "3.3.3.3"


def test_falls_back_to_direct_peer_host():
    req = FakeReq(headers={}, client_host="4.4.4.4")
    assert client_ip(req) == "4.4.4.4"


def test_unknown_when_nothing_available():
    req = FakeReq(headers={}, client_host=None)
    assert client_ip(req) == "unknown"
