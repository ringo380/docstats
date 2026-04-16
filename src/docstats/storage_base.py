"""Abstract base class and shared helpers for storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from difflib import SequenceMatcher

from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry


def normalize_email(email: str) -> str:
    """Normalize an email address for storage and lookup.

    Lowercases and strips whitespace. Does not validate format — callers that
    handle untrusted input should use ``docstats.validators.validate_email``
    first to reject malformed addresses with a friendly error.
    """
    return (email or "").strip().lower()


def fuzzy_score(provider: SavedProvider, query: str) -> float:
    """Score a saved provider against a search query for result ranking."""
    best = 0.0
    for field in (provider.display_name, provider.specialty, provider.notes):
        if field:
            ratio = SequenceMatcher(None, query, field.lower()).ratio()
            if ratio > best:
                best = ratio
    if provider.npi.startswith(query):
        best = max(best, 1.0)
    return best


class StorageBase(ABC):
    """Interface that both SQLite and Postgres storage backends implement."""

    # --- User CRUD ---

    @abstractmethod
    def create_user(self, email: str, password_hash: str) -> int: ...

    @abstractmethod
    def get_user_by_id(self, user_id: int) -> dict | None: ...

    @abstractmethod
    def get_user_by_email(self, email: str) -> dict | None: ...

    @abstractmethod
    def get_user_by_github_id(self, github_id: str) -> dict | None: ...

    @abstractmethod
    def upsert_github_user(
        self,
        github_id: str,
        github_login: str,
        email: str | None,
        display_name: str | None,
    ) -> int: ...

    @abstractmethod
    def update_last_login(self, user_id: int) -> None: ...

    @abstractmethod
    def set_user_pcp(self, user_id: int, pcp_npi: str) -> None: ...

    @abstractmethod
    def clear_user_pcp(self, user_id: int) -> None: ...

    @abstractmethod
    def update_user_profile(
        self,
        user_id: int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        middle_name: str | None = None,
        date_of_birth: str | None = None,
        display_name: str | None = None,
    ) -> None: ...

    @abstractmethod
    def record_terms_acceptance(
        self,
        user_id: int,
        *,
        terms_version: str,
        ip_address: str,
        user_agent: str,
    ) -> None: ...

    # --- Provider CRUD ---

    @abstractmethod
    def save_provider(
        self, result: NPIResult, user_id: int, notes: str | None = None
    ) -> SavedProvider: ...

    @abstractmethod
    def get_provider(self, npi: str, user_id: int | None) -> SavedProvider | None: ...

    @abstractmethod
    def list_providers(self, user_id: int) -> list[SavedProvider]: ...

    @abstractmethod
    def search_providers(self, user_id: int, query: str) -> list[SavedProvider]: ...

    @abstractmethod
    def delete_provider(self, npi: str, user_id: int) -> bool: ...

    @abstractmethod
    def update_notes(self, npi: str, notes: str | None, user_id: int) -> bool: ...

    @abstractmethod
    def set_appt_address(self, npi: str, address: str, user_id: int) -> bool: ...

    @abstractmethod
    def set_appt_suite(self, npi: str, suite: str | None, user_id: int) -> bool: ...

    @abstractmethod
    def clear_appt_address(self, npi: str, user_id: int) -> bool: ...

    @abstractmethod
    def set_televisit(self, npi: str, is_televisit: bool, user_id: int) -> bool: ...

    @abstractmethod
    def set_appt_contact(
        self, npi: str, phone: str | None, fax: str | None, user_id: int
    ) -> bool: ...

    @abstractmethod
    def update_enrichment(self, npi: str, enrichment_json: str, user_id: int) -> bool: ...

    # --- Search history ---

    @abstractmethod
    def log_search(
        self, params: dict[str, str], result_count: int, user_id: int | None = None
    ) -> None: ...

    @abstractmethod
    def get_history(
        self, limit: int = 20, user_id: int | None = None
    ) -> list[SearchHistoryEntry]: ...

    # --- ZIP code lookup ---

    @abstractmethod
    def lookup_zip(self, zip_code: str) -> dict[str, str] | None: ...

    @abstractmethod
    def close(self) -> None: ...
