"""Tests for input validators."""

from __future__ import annotations

import pytest

from docstats.validators import (
    EMAIL_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    ValidationError,
    validate_email,
    validate_npi,
)


class TestValidateNpi:
    def test_accepts_10_digits(self):
        assert validate_npi("1234567890") == "1234567890"

    def test_strips_whitespace(self):
        assert validate_npi("  1234567890  ") == "1234567890"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "123",
            "12345678901",  # 11 digits
            "123456789a",  # contains letter
            "123-456-7890",  # hyphens
            "abcdefghij",
            "1 234567890",  # internal whitespace
        ],
    )
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValidationError):
            validate_npi(bad)

    def test_rejects_none(self):
        with pytest.raises(ValidationError):
            validate_npi(None)  # type: ignore[arg-type]


class TestValidateEmail:
    def test_accepts_simple(self):
        assert validate_email("alice@example.com") == "alice@example.com"

    def test_lowercases_and_strips(self):
        assert validate_email("  Alice@Example.COM  ") == "alice@example.com"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "notanemail",
            "missing-at.com",
            "two@@example.com",
            "has spaces@example.com",
            "no-dot@nodomain",
        ],
    )
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValidationError):
            validate_email(bad)

    def test_rejects_too_long(self):
        long_email = "a" * (EMAIL_MAX_LENGTH + 1) + "@x.co"
        with pytest.raises(ValidationError):
            validate_email(long_email)

    def test_rejects_none(self):
        with pytest.raises(ValidationError):
            validate_email(None)  # type: ignore[arg-type]


def test_password_cap_constant_matches_bcrypt_limit():
    # bcrypt silently truncates beyond 72 bytes; we reject explicitly.
    assert PASSWORD_MAX_LENGTH == 72
