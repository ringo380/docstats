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

# Direct Trust addresses look like email but route through DirectTrust-accredited
# HISPs. Format is ``local-part@direct.<domain>`` per RFC 5322; DirectTrust
# convention reserves a sub-domain (typically containing ``direct``), but we
# don't enforce the sub-domain shape here — vendors disagree on the convention
# and the trust bundle is what actually validates routability.
DIRECT_ADDRESS_MAX_LENGTH = 254
DIRECT_ADDRESS_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ValidationError(ValueError):
    """Raised when user input fails format validation."""


def validate_npi(npi: str) -> str:
    """Return the NPI if valid (10 digits), raise ValidationError otherwise."""
    npi = (npi or "").strip()
    if not NPI_PATTERN.match(npi):
        raise ValidationError("NPI must be exactly 10 digits.")
    return npi


def npi_luhn_ok(npi: str) -> bool:
    """Return True if ``npi`` passes the NPI Luhn check digit (CMS algorithm).

    The NPI check digit is computed by prepending the NUCC industry
    identifier prefix ``80840`` to the 9-digit base, then running standard
    Luhn over the resulting 14 digits to derive a check digit which forms
    the NPI's 10th character. Validation reverses the process by running
    Luhn over the 15-digit ``80840 + NPI`` and confirming the sum is a
    multiple of 10.

    Returns False (rather than raising) for any input that doesn't match
    the 10-digit format — callers compose this with ``validate_npi``.
    """
    if not NPI_PATTERN.match(npi or ""):
        return False
    s = "80840" + npi
    total = 0
    # Walk right-to-left; double every second position starting at index 1
    # (i.e. position 2 in 1-indexed-from-right). Position 1 (the check
    # digit) is summed as-is; doubled values > 9 collapse via -9.
    for i, ch in enumerate(reversed(s)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


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


def validate_direct_address(addr: str) -> str:
    """Return normalized Direct Trust address, raise ValidationError otherwise.

    Direct addresses are syntactically email-shaped — the trust bundle
    enforces actual routability at the HISP layer. We just guard against
    obvious garbage at the input boundary.
    """
    addr = (addr or "").strip().lower()
    if not addr or len(addr) > DIRECT_ADDRESS_MAX_LENGTH or not DIRECT_ADDRESS_PATTERN.match(addr):
        raise ValidationError("Please enter a valid Direct Trust address.")
    return addr


def require_valid_npi(npi: str = Path(..., min_length=10, max_length=10)) -> str:
    """FastAPI dependency — rejects non-10-digit NPIs with 422."""
    try:
        return validate_npi(npi)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
