"""Fernet symmetric encryption for SMART-on-FHIR tokens at rest.

Reads `EHR_TOKEN_KEY` (urlsafe-base64 32-byte Fernet key) at call-time so a
deployment can rotate the key with a process restart. Missing/invalid key
raises `EHRConfigError` — fail closed; never write/read plaintext tokens.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class EHRConfigError(RuntimeError):
    """EHR_TOKEN_KEY missing or malformed."""


def _cipher() -> Fernet:
    key = os.getenv("EHR_TOKEN_KEY", "").strip()
    if not key:
        raise EHRConfigError("EHR_TOKEN_KEY is not set")
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as e:
        raise EHRConfigError(f"EHR_TOKEN_KEY is malformed: {e}") from e


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token. Returns urlsafe-base64 ciphertext as str."""
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a token. Raises EHRConfigError on key issues, InvalidToken on bad ct."""
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise
