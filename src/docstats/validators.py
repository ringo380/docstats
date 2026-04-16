"""Shared input validators for web routes, CLI, and storage.

Centralizes format checks so the same rule is enforced everywhere a value
enters the system. Kept small and dependency-free (stdlib + fastapi only).
"""

from __future__ import annotations

import re

from fastapi import HTTPException, Path

NPI_PATTERN = re.compile(r"^\d{10}$")
# Conservative email pattern — not a full RFC 5322 check, but rejects the
# obvious malformed inputs (missing @, whitespace, no dot in domain).
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

EMAIL_MAX_LENGTH = 254  # RFC 5321 practical limit
PASSWORD_MAX_LENGTH = 72  # bcrypt truncates beyond this; reject explicitly


class ValidationError(ValueError):
    """Raised when user input fails format validation."""


def validate_npi(npi: str) -> str:
    """Return the NPI if valid (10 digits), raise ValidationError otherwise."""
    npi = (npi or "").strip()
    if not NPI_PATTERN.match(npi):
        raise ValidationError("NPI must be exactly 10 digits.")
    return npi


def validate_email(email: str) -> str:
    """Return normalized email if valid, raise ValidationError otherwise."""
    email = (email or "").strip().lower()
    if not email or len(email) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.match(email):
        raise ValidationError("Please enter a valid email address.")
    return email


def require_valid_npi(npi: str = Path(..., min_length=10, max_length=10)) -> str:
    """FastAPI dependency — rejects non-10-digit NPIs with 422."""
    try:
        return validate_npi(npi)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
