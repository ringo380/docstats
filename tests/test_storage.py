"""Tests for SQLite storage."""

import pytest
from docstats.models import NPIResult
from docstats.storage import Storage
from tests.conftest import SAMPLE_NPI1_RESULT, SAMPLE_NPI2_RESULT


@pytest.fixture
def user_id(storage: Storage) -> int:
    """Create a test user and return its id."""
    return storage.create_user("test@example.com", "hashed")


def test_save_and_get_provider(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    provider = storage.save_provider(result, user_id, notes="test note")

    assert provider.npi == "1234567890"
    assert provider.notes == "test note"

    retrieved = storage.get_provider("1234567890", user_id)
    assert retrieved is not None
    assert retrieved.npi == "1234567890"
    assert retrieved.notes == "test note"
    assert "John" in retrieved.display_name


def test_list_providers(storage: Storage, user_id: int):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    r2 = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
    storage.save_provider(r1, user_id)
    storage.save_provider(r2, user_id)

    providers = storage.list_providers(user_id)
    assert len(providers) == 2
    npis = {p.npi for p in providers}
    assert "1234567890" in npis
    assert "9876543210" in npis


def test_delete_provider(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)

    assert storage.delete_provider("1234567890", user_id) is True
    assert storage.get_provider("1234567890", user_id) is None
    assert storage.delete_provider("1234567890", user_id) is False


def test_update_existing_provider(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id, notes="first")
    storage.save_provider(result, user_id, notes="updated")

    providers = storage.list_providers(user_id)
    assert len(providers) == 1
    assert providers[0].notes == "updated"


def test_providers_are_per_user(storage: Storage):
    """Providers saved by user A are not visible to user B."""
    uid_a = storage.create_user("a@example.com", "h")
    uid_b = storage.create_user("b@example.com", "h")
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, uid_a)

    assert storage.get_provider("1234567890", uid_a) is not None
    assert storage.get_provider("1234567890", uid_b) is None
    assert len(storage.list_providers(uid_b)) == 0


def test_get_provider_anonymous(storage: Storage, user_id: int):
    """Anonymous (user_id=None) always returns None."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    assert storage.get_provider("1234567890", None) is None


def test_log_and_get_history(storage: Storage, user_id: int):
    storage.log_search({"last_name": "smith", "state": "CA"}, 42, user_id=user_id)
    storage.log_search({"organization_name": "kaiser"}, 10, user_id=user_id)

    history = storage.get_history(limit=10, user_id=user_id)
    assert len(history) == 2
    assert history[0].result_count == 10  # newest first
    assert history[1].result_count == 42


def test_history_anonymous_returns_empty(storage: Storage, user_id: int):
    storage.log_search({"last_name": "smith"}, 5, user_id=user_id)
    assert storage.get_history(user_id=None) == []


def test_get_provider_not_found(storage: Storage, user_id: int):
    assert storage.get_provider("0000000000", user_id) is None


def test_rehydrate_saved_provider(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)

    provider = storage.get_provider("1234567890", user_id)
    assert provider is not None

    rehydrated = provider.to_npi_result()
    assert rehydrated.number == "1234567890"
    assert len(rehydrated.addresses) == 2
    assert len(rehydrated.taxonomies) == 2


def test_appt_address_column_exists(storage: Storage):
    """appt_address column should exist after init."""
    cols = [r[1] for r in storage._conn.execute("PRAGMA table_info(saved_providers)")]
    assert "appt_address" in cols


def test_set_appt_address(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address == "1 Shrader St, San Francisco, CA 94117"


def test_clear_appt_address(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    storage.clear_appt_address("1234567890", user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address is None


def test_save_provider_preserves_appt_address(storage: Storage, user_id: int):
    """Re-saving a provider must not reset its appt_address."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    storage.save_provider(result, user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address == "1 Shrader St, San Francisco, CA 94117"


def test_appt_suite_column_exists(storage: Storage):
    """appt_suite column should exist after init."""
    cols = [r[1] for r in storage._conn.execute("PRAGMA table_info(saved_providers)")]
    assert "appt_suite" in cols


def test_set_appt_suite(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_suite("1234567890", "Suite 6A", user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_suite == "Suite 6A"


def test_set_appt_suite_strips_whitespace(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_suite("1234567890", "  Room 201  ", user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_suite == "Room 201"


def test_clear_appt_suite(storage: Storage, user_id: int):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_suite("1234567890", "Suite 6A", user_id)
    storage.set_appt_suite("1234567890", None, user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_suite is None


def test_clear_appt_address_clears_suite(storage: Storage, user_id: int):
    """Clearing the address must also clear the suite."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_address("1234567890", "1 Shrader St, San Francisco, CA 94117", user_id)
    storage.set_appt_suite("1234567890", "Suite 6A", user_id)
    storage.clear_appt_address("1234567890", user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_address is None
    assert provider.appt_suite is None


def test_save_provider_preserves_appt_suite(storage: Storage, user_id: int):
    """Re-saving a provider must not reset its appt_suite."""
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, user_id)
    storage.set_appt_suite("1234567890", "Rm 100-B", user_id)
    storage.save_provider(result, user_id)
    provider = storage.get_provider("1234567890", user_id)
    assert provider.appt_suite == "Rm 100-B"


# --- search_providers tests ---


def test_search_providers_by_name(storage: Storage, user_id: int):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    r2 = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
    storage.save_provider(r1, user_id)
    storage.save_provider(r2, user_id)

    results = storage.search_providers(user_id, "smith")
    assert len(results) == 1
    assert results[0].npi == "1234567890"


def test_search_providers_by_npi(storage: Storage, user_id: int):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(r1, user_id)

    results = storage.search_providers(user_id, "1234")
    assert len(results) == 1
    assert results[0].npi == "1234567890"


def test_search_providers_by_specialty(storage: Storage, user_id: int):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    r2 = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
    storage.save_provider(r1, user_id)
    storage.save_provider(r2, user_id)

    results = storage.search_providers(user_id, "internal medicine")
    assert len(results) == 1
    assert results[0].npi == "1234567890"


def test_search_providers_by_notes(storage: Storage, user_id: int):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(r1, user_id, notes="great cardiologist")

    results = storage.search_providers(user_id, "cardiologist")
    assert len(results) == 1
    assert results[0].npi == "1234567890"


def test_search_providers_no_results(storage: Storage, user_id: int):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(r1, user_id)

    results = storage.search_providers(user_id, "zzzznotfound")
    assert len(results) == 0


def test_search_providers_per_user(storage: Storage):
    """Search must not return providers from other users."""
    uid_a = storage.create_user("a@example.com", "h")
    uid_b = storage.create_user("b@example.com", "h")
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(r1, uid_a)

    assert len(storage.search_providers(uid_a, "smith")) == 1
    assert len(storage.search_providers(uid_b, "smith")) == 0


def test_search_providers_by_city(storage: Storage, user_id: int):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    r2 = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
    storage.save_provider(r1, user_id)
    storage.save_provider(r2, user_id)

    results = storage.search_providers(user_id, "walnut creek")
    assert len(results) == 1
    assert results[0].npi == "9876543210"


def test_search_providers_wildcard_escaped(storage: Storage, user_id: int):
    """% and _ in query should be treated as literals, not LIKE wildcards."""
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(r1, user_id, notes="100% recommended")

    # "100%" should match the literal string, not act as a wildcard
    results = storage.search_providers(user_id, "100%")
    assert len(results) == 1

    # "_ohn" with _ as wildcard would match "John" — escaped, it should not
    results = storage.search_providers(user_id, "_ohn")
    assert len(results) == 0
