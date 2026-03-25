"""Normalization utilities for NPPES API data.

The API returns names in UPPERCASE, phone numbers as raw digits,
and postal codes without hyphens. This module cleans all of that up.
"""

from __future__ import annotations

import re

# Credentials that should stay uppercase
_UPPERCASE_CREDENTIALS = {
    "MD", "DO", "DDS", "DMD", "OD", "DC", "DPM", "PhD", "PharmD",
    "PA", "NP", "RN", "LPN", "APRN", "CNP", "CNS", "CRNA", "DNP",
    "LCSW", "LMFT", "LPC", "LMHC", "PsyD", "EdD", "AuD", "DPT",
    "OTR", "SLP", "CCC", "RD", "LD", "RPh", "BCBA", "BCBA-D",
    "II", "III", "IV", "JR", "SR",
    "MBA", "MPH", "MS", "MA", "MHA", "MSN", "MSW", "MEd",
    "FACP", "FACS", "FACOG", "FAAP", "FAAN", "FACEP",
}

# Words that should stay lowercase in names (unless first word)
_LOWERCASE_WORDS = {"de", "del", "la", "le", "van", "von", "der", "den", "di"}


def format_name(raw: str | None) -> str:
    """Convert UPPERCASE name to proper title case.

    Handles compound names, particles (de, van, etc.), and
    preserves credential-like suffixes.
    """
    if not raw or raw.strip() in ("", "--"):
        return ""

    words = raw.strip().split()
    result = []
    for i, word in enumerate(words):
        upper = word.upper()
        # Check if it's a known credential/suffix
        if upper in {c.upper() for c in _UPPERCASE_CREDENTIALS}:
            # Find the correctly-cased version
            for cred in _UPPERCASE_CREDENTIALS:
                if cred.upper() == upper:
                    result.append(cred)
                    break
        elif word.lower() in _LOWERCASE_WORDS and i > 0:
            result.append(word.lower())
        elif "-" in word:
            # Hyphenated names: each part gets capitalized
            result.append("-".join(p.capitalize() for p in word.split("-")))
        else:
            result.append(word.capitalize())

    return " ".join(result)


def format_phone(raw: str | None) -> str | None:
    """Format a raw phone string into (XXX) XXX-XXXX.

    Handles 10-digit strings and already-formatted numbers.
    """
    if not raw or raw.strip() in ("", "--"):
        return None

    digits = re.sub(r"\D", "", raw)

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    # Return cleaned-up original if we can't parse it
    return raw.strip() if raw.strip() else None


def format_postal_code(raw: str | None) -> str:
    """Format postal code: 9 digits -> ZIP+4, 5 digits stays as-is."""
    if not raw:
        return ""

    digits = re.sub(r"\D", "", raw)

    if len(digits) == 9:
        return f"{digits[:5]}-{digits[5:]}"
    if len(digits) >= 5:
        return digits[:5]

    return raw.strip()


def format_credential(raw: str | None) -> str:
    """Clean up credential string (e.g., '.M.D.' -> 'MD')."""
    if not raw or raw.strip() in ("", "--"):
        return ""

    cleaned = raw.strip().strip(".")
    # Remove dots between single letters (M.D. -> MD)
    if re.match(r"^[A-Za-z](\.[A-Za-z])+\.?$", cleaned):
        cleaned = cleaned.replace(".", "")

    # Try to match known credentials
    upper = cleaned.upper().strip()
    for cred in _UPPERCASE_CREDENTIALS:
        if cred.upper() == upper:
            return cred

    return cleaned.strip()


def clean_sentinel(value: str | None) -> str | None:
    """Replace API sentinel values ('--', empty) with None."""
    if value is None:
        return None
    stripped = value.strip()
    if stripped in ("", "--", "N/A"):
        return None
    return stripped
