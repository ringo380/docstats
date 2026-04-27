"""Token encryption round-trip + key-missing fail-closed."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from docstats.ehr.crypto import EHRConfigError, decrypt_token, encrypt_token


def test_round_trip(monkeypatch):
    monkeypatch.setenv("EHR_TOKEN_KEY", Fernet.generate_key().decode())
    plaintext = "epic-access-token-abc123"
    ct = encrypt_token(plaintext)
    assert ct != plaintext
    assert decrypt_token(ct) == plaintext


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("EHR_TOKEN_KEY", raising=False)
    with pytest.raises(EHRConfigError):
        encrypt_token("x")
    with pytest.raises(EHRConfigError):
        decrypt_token("x")


def test_malformed_key_raises(monkeypatch):
    monkeypatch.setenv("EHR_TOKEN_KEY", "not-a-fernet-key")
    with pytest.raises(EHRConfigError):
        encrypt_token("x")


def test_decrypt_with_wrong_key_raises(monkeypatch):
    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()
    monkeypatch.setenv("EHR_TOKEN_KEY", k1)
    ct = encrypt_token("hello")
    monkeypatch.setenv("EHR_TOKEN_KEY", k2)
    with pytest.raises(InvalidToken):
        decrypt_token(ct)
