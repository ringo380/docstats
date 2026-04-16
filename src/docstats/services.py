"""Shared service logic for CLI and web layers.

Encapsulates search, lookup, and save workflows so both interfaces
use the same business logic (including scoring and history logging).
"""

from __future__ import annotations

from docstats.client import NPPESClient
from docstats.models import NPIResponse, NPIResult, SavedProvider
from docstats.scoring import SearchQuery, rank_results
from docstats.storage_base import StorageBase


def search_providers(
    client: NPPESClient,
    storage: StorageBase,
    *,
    last_name: str | None = None,
    first_name: str | None = None,
    organization_name: str | None = None,
    taxonomy_description: str | None = None,
    state: str | None = None,
    city: str | None = None,
    postal_code: str | None = None,
    enumeration_type: str | None = None,
    limit: int = 10,
    use_cache: bool = True,
    user_id: int | None = None,
    sync: bool = False,
) -> NPIResponse:
    """Search NPPES, rank results, and log to history.

    Set sync=True for CLI (uses synchronous httpx); False for web (must
    call async_search via run_in_executor separately).

    Returns the full NPIResponse with results re-ordered by score.
    """
    response = client.search(
        last_name=last_name,
        first_name=first_name,
        organization_name=organization_name,
        taxonomy_description=taxonomy_description,
        state=state,
        city=city,
        postal_code=postal_code,
        enumeration_type=enumeration_type,
        limit=limit,
        use_cache=use_cache,
    )

    # Rank results
    query_obj = SearchQuery(
        last_name=last_name,
        first_name=first_name,
        organization_name=organization_name,
        specialty=taxonomy_description,
        city=city,
        state=state,
        postal_code=postal_code,
    )
    ranked = rank_results(response.results, query_obj)[:limit]
    response = NPIResponse(result_count=response.result_count, results=ranked)

    # Log to history
    params: dict[str, str] = {}
    for k, v in [
        ("last_name", last_name), ("first_name", first_name),
        ("organization_name", organization_name), ("taxonomy_description", taxonomy_description),
        ("state", state), ("city", city), ("postal_code", postal_code),
        ("enumeration_type", enumeration_type),
    ]:
        if v:
            params[k] = v
    storage.log_search(params, response.result_count, user_id=user_id)

    return response


def lookup_provider(
    client: NPPESClient,
    npi: str,
    *,
    use_cache: bool = True,
) -> NPIResult | None:
    """Look up a single provider by NPI. Returns None if not found."""
    return client.lookup(npi, use_cache=use_cache)


def save_provider(
    client: NPPESClient,
    storage: StorageBase,
    npi: str,
    user_id: int,
    *,
    notes: str | None = None,
    use_cache: bool = True,
) -> SavedProvider:
    """Look up a provider and save it. Raises NPPESError or ValueError."""
    result = client.lookup(npi, use_cache=use_cache)
    if result is None:
        raise ValueError(f"No provider found for NPI {npi}")
    return storage.save_provider(result, user_id, notes=notes)
