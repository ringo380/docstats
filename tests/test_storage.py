"""Tests for SQLite storage."""

from docstats.models import NPIResult
from docstats.storage import Storage
from tests.conftest import SAMPLE_NPI1_RESULT, SAMPLE_NPI2_RESULT


def test_save_and_get_provider(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    provider = storage.save_provider(result, notes="test note")

    assert provider.npi == "1234567890"
    assert provider.notes == "test note"

    retrieved = storage.get_provider("1234567890")
    assert retrieved is not None
    assert retrieved.npi == "1234567890"
    assert retrieved.notes == "test note"
    assert "John" in retrieved.display_name


def test_list_providers(storage: Storage):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    r2 = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
    storage.save_provider(r1)
    storage.save_provider(r2)

    providers = storage.list_providers()
    assert len(providers) == 2
    npis = {p.npi for p in providers}
    assert "1234567890" in npis
    assert "9876543210" in npis


def test_delete_provider(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)

    assert storage.delete_provider("1234567890") is True
    assert storage.get_provider("1234567890") is None
    assert storage.delete_provider("1234567890") is False


def test_update_existing_provider(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result, notes="first")
    storage.save_provider(result, notes="updated")

    providers = storage.list_providers()
    assert len(providers) == 1
    assert providers[0].notes == "updated"


def test_log_and_get_history(storage: Storage):
    storage.log_search({"last_name": "smith", "state": "CA"}, 42)
    storage.log_search({"organization_name": "kaiser"}, 10)

    history = storage.get_history(limit=10)
    assert len(history) == 2
    assert history[0].result_count == 10  # newest first
    assert history[1].result_count == 42


def test_get_provider_not_found(storage: Storage):
    assert storage.get_provider("0000000000") is None


def test_rehydrate_saved_provider(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)

    provider = storage.get_provider("1234567890")
    assert provider is not None

    rehydrated = provider.to_npi_result()
    assert rehydrated.number == "1234567890"
    assert len(rehydrated.addresses) == 2
    assert len(rehydrated.taxonomies) == 2
