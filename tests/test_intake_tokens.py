"""
Signed intake token primitive (Phase 1): mint -> verify round-trips; tampering,
wrong secret, and malformed tokens all yield None (a 404, never a crash); the
token stays inside the intake route charset [A-Za-z0-9_.-]. Pure module, no I/O
beyond the secret env var.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, intake_tokens as it  # noqa: E402

SECRET = "s3cret-signing-key-for-tests-only"
_CHARSET = re.compile(r"^[A-Za-z0-9_.-]{8,}$")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, SECRET)
    yield


def test_round_trip():
    tok = it.mint("gym_alpha_ig")
    assert it.verify(tok) == "gym_alpha_ig"


def test_mint_normalizes_case_before_signing():
    # a caller passing the display-cased key still lands the canonical key
    tok = it.mint("Gym_Alpha_IG")
    assert it.verify(tok) == "gym_alpha_ig"


def test_token_is_route_charset_safe():
    for key in ("gym_alpha_ig", "a_b", "north-naples_fb", "x" * 40):
        tok = it.mint(key)
        assert _CHARSET.match(tok), f"token out of charset for {key}: {tok}"
        assert "=" not in tok  # padding stripped


def test_two_keys_two_tokens():
    assert it.mint("gym_a_ig") != it.mint("gym_b_ig")


def test_tampered_client_key_fails():
    tok = it.mint("gym_alpha_ig")
    key_part, _, sig_part = tok.partition(".")
    forged = it._b64url(b"gym_victim_ig") + "." + sig_part
    assert it.verify(forged) is None


def test_tampered_signature_fails():
    tok = it.mint("gym_alpha_ig")
    key_part, _, sig_part = tok.partition(".")
    # flip the last char of the signature to something else in the charset
    flipped = sig_part[:-1] + ("A" if sig_part[-1] != "A" else "B")
    assert it.verify(key_part + "." + flipped) is None


def test_wrong_secret_fails():
    tok = it.mint("gym_alpha_ig")
    assert it.verify(tok, secret=b"a-completely-different-secret") is None


def test_malformed_tokens_return_none():
    for bad in ("", "no-separator", ".", "a.", ".b", "!!.??", "gym_alpha_ig"):
        assert it.verify(bad) is None


def test_no_secret_verify_is_none(monkeypatch):
    good = it.mint("gym_alpha_ig")
    monkeypatch.delenv(config.INTAKE_SIGNING_SECRET_ENV, raising=False)
    assert it.verify(good) is None
    assert it.secret_present() is False


def test_no_secret_mint_raises(monkeypatch):
    monkeypatch.delenv(config.INTAKE_SIGNING_SECRET_ENV, raising=False)
    with pytest.raises(ValueError):
        it.mint("gym_alpha_ig")


def test_empty_client_key_mint_raises():
    with pytest.raises(ValueError):
        it.mint("")


def test_secret_present_true_when_set():
    assert it.secret_present() is True
