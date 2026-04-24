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

# Fax numbers: E.164-normalized US/Canada numbers (11 digits starting with 1).
# Documo accepts international but Phase 9.C scope is US-only.
FAX_DIGITS_PATTERN = re.compile(r"^1\d{10}$")

EMAIL_MAX_LENGTH = 254  # RFC 5321 practical limit
PASSWORD_MAX_LENGTH = 72  # bcrypt truncates beyond this; reject explicitly
IP_MAX_LENGTH = 45  # IPv6 max (8 groups of 4 hex + 7 colons); IPv4 fits easily
USER_AGENT_MAX_LENGTH = 500  # defensive bound; real UAs are ~200 chars
FAX_NUMBER_MAX_LENGTH = 20  # "+1 (555) 555-5555 x1234" — generous upper bound


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


def validate_fax_number(fax: str) -> str:
    """Return a normalized E.164 fax number (``+1XXXXXXXXXX``) or raise.

    Accepts common US input shapes — ``(555) 555-5555``, ``555-555-5555``,
    ``+1 555 555 5555``, etc.  Strips formatting, requires 10 digits (bare
    US) or 11 digits starting with ``1`` (already-qualified).  Non-US
    numbers are rejected in Phase 9.C scope (Documo supports international;
    product domain is US for now).
    """
    raw = (fax or "").strip()
    if not raw or len(raw) > FAX_NUMBER_MAX_LENGTH:
        raise ValidationError("Please enter a valid fax number.")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if not FAX_DIGITS_PATTERN.match(digits):
        raise ValidationError(
            "Fax number must be a US/Canada number (10 digits, or 11 digits starting with 1)."
        )
    return "+" + digits


def require_valid_npi(npi: str = Path(..., min_length=10, max_length=10)) -> str:
    """FastAPI dependency — rejects non-10-digit NPIs with 422."""
    try:
        return validate_npi(npi)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
