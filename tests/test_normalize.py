"""Tests for normalization utilities."""

from docstats.normalize import (
    clean_sentinel,
    format_credential,
    format_name,
    format_phone,
    format_postal_code,
)


class TestFormatName:
    def test_uppercase_to_title(self):
        assert format_name("JOHN SMITH") == "John Smith"

    def test_preserves_credentials(self):
        assert format_name("MD") == "MD"
        assert format_name("DO") == "DO"

    def test_hyphenated_names(self):
        assert format_name("SMITH-JONES") == "Smith-Jones"

    def test_name_particles(self):
        assert format_name("LUDWIG VAN BEETHOVEN") == "Ludwig van Beethoven"
        assert format_name("MARIA DE LA CRUZ") == "Maria de la Cruz"

    def test_empty_and_sentinel(self):
        assert format_name("") == ""
        assert format_name(None) == ""
        assert format_name("--") == ""

    def test_single_word(self):
        assert format_name("JOHNSON") == "Johnson"

    def test_suffixes(self):
        assert format_name("JAMES SMITH III") == "James Smith III"
        assert format_name("ROBERT JONES JR") == "Robert Jones JR"


class TestFormatPhone:
    def test_ten_digits(self):
        assert format_phone("4155551234") == "(415) 555-1234"

    def test_eleven_digits_with_country(self):
        assert format_phone("14155551234") == "(415) 555-1234"

    def test_already_formatted(self):
        assert format_phone("(415) 555-1234") == "(415) 555-1234"

    def test_with_dashes(self):
        assert format_phone("415-555-1234") == "(415) 555-1234"

    def test_empty_and_sentinel(self):
        assert format_phone("") is None
        assert format_phone(None) is None
        assert format_phone("--") is None

    def test_short_number(self):
        # Non-standard numbers returned as-is
        assert format_phone("5551234") == "5551234"


class TestFormatPostalCode:
    def test_nine_digits(self):
        assert format_postal_code("941103518") == "94110-3518"

    def test_five_digits(self):
        assert format_postal_code("94110") == "94110"

    def test_already_hyphenated(self):
        # Digits extracted = 9, so it re-formats as ZIP+4
        assert format_postal_code("94110-3518") == "94110-3518"

    def test_empty(self):
        assert format_postal_code("") == ""
        assert format_postal_code(None) == ""


class TestFormatCredential:
    def test_dotted_md(self):
        assert format_credential("M.D.") == "MD"

    def test_clean_md(self):
        assert format_credential("MD") == "MD"

    def test_dotted_do(self):
        assert format_credential("D.O.") == "DO"

    def test_empty_and_sentinel(self):
        assert format_credential("") == ""
        assert format_credential(None) == ""
        assert format_credential("--") == ""

    def test_unknown_credential(self):
        assert format_credential("CUSTOM-CRED") == "CUSTOM-CRED"


class TestCleanSentinel:
    def test_double_dash(self):
        assert clean_sentinel("--") is None

    def test_na(self):
        assert clean_sentinel("N/A") is None

    def test_empty(self):
        assert clean_sentinel("") is None

    def test_none(self):
        assert clean_sentinel(None) is None

    def test_valid_value(self):
        assert clean_sentinel("hello") == "hello"

    def test_whitespace(self):
        assert clean_sentinel("  ") is None
