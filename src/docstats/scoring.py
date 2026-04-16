"""Local result ranking for NPPES search results.

The NPPES API returns results in an arbitrary order. This module scores
and ranks results based on how well they match the user's search criteria.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from docstats.models import NPIResult
from docstats.zip_coords import haversine_miles, zip_to_coords

if TYPE_CHECKING:
    from docstats.enrichment import EnrichmentData


@dataclass
class SearchQuery:
    """Captures the user's search parameters for scoring."""

    last_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    organization_name: str | None = None
    specialty: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    geo_state: str | None = None  # browser-detected user state for proximity boost
    geo_lat: float | None = None  # browser-detected latitude (rounded to ~1km)
    geo_lon: float | None = None  # browser-detected longitude (rounded to ~1km)


def score_result(
    result: NPIResult,
    query: SearchQuery,
    enrichment: EnrichmentData | None = None,
) -> int:
    """Score a single result against the user's search query.

    Higher scores indicate better matches. Range is roughly 0-155
    (before enrichment adjustments).
    """
    score = 0
    basic = result.basic

    # Active status is the strongest signal -- deactivated providers should sink
    if basic.get("status") == "A":
        score += 50

    # Name matching (individual providers)
    if result.is_individual:
        if query.last_name:
            raw_last = (basic.get("last_name") or "").upper()
            if raw_last == query.last_name.upper():
                score += 30
            elif raw_last.startswith(query.last_name.upper()):
                score += 15

        if query.first_name:
            raw_first = (basic.get("first_name") or "").upper()
            if raw_first == query.first_name.upper():
                score += 20
            elif raw_first.startswith(query.first_name.upper()):
                score += 10

        if query.middle_name:
            raw_mid = (basic.get("middle_name") or "").upper()
            query_mid = query.middle_name.upper()
            if raw_mid and raw_mid == query_mid:
                score += 15
            elif raw_mid and len(query_mid) == 1 and raw_mid.startswith(query_mid):
                score += 10
            elif raw_mid and len(query_mid) > 1 and raw_mid.startswith(query_mid):
                score += 12
            elif raw_mid and len(query_mid) > 1:
                similarity = SequenceMatcher(None, query_mid, raw_mid).ratio()
                if similarity >= 0.75:
                    score += 8

    # Org name matching
    if result.is_organization and query.organization_name:
        raw_org = (basic.get("organization_name") or "").upper()
        query_org = query.organization_name.upper()
        if raw_org == query_org:
            score += 30
        elif query_org in raw_org:
            score += 15

    # Location matching
    addr = result.location_address
    if addr:
        if query.postal_code:
            result_zip = (addr.postal_code or "")[:5]
            query_zip = query.postal_code[:5]
            if result_zip == query_zip:
                score += 20
            else:
                # Fall through to city/state matching
                if query.state and addr.state.upper() == query.state.upper():
                    score += 10
                if query.city and addr.city.upper() == query.city.upper():
                    score += 10
        else:
            if query.state and addr.state.upper() == query.state.upper():
                score += 10
            if query.city and addr.city.upper() == query.city.upper():
                score += 10

    # Geolocation proximity boost (only when user didn't manually filter by location)
    if (
        query.geo_lat is not None
        and query.geo_lon is not None
        and not query.state
        and not query.postal_code
    ):
        addr = result.location_address
        if addr and addr.postal_code:
            provider_coords = zip_to_coords(addr.postal_code)
            if provider_coords:
                dist = haversine_miles(query.geo_lat, query.geo_lon, *provider_coords)
                if dist <= 15:
                    score += 25
                elif dist <= 30:
                    score += 20
                elif dist <= 60:
                    score += 15
                elif dist <= 120:
                    score += 10
        elif addr and query.geo_state and addr.state.upper() == query.geo_state.upper():
            # Fallback: no ZIP centroid data — use state match
            score += 8

    # Taxonomy/specialty matching
    if query.specialty:
        spec_upper = query.specialty.upper()
        for t in result.taxonomies:
            if spec_upper in t.desc.upper():
                score += 10 if t.primary else 5
                break

    # Enrichment-based adjustments (optional, from cached data)
    if enrichment is not None:
        if enrichment.oig_excluded is True:
            score -= 100  # Excluded from federal programs -- critical safety signal
        if enrichment.medicare_enrolled is True:
            score += 10  # Confirms active practice

    return score


def rank_results(results: list[NPIResult], query: SearchQuery) -> list[NPIResult]:
    """Sort results by relevance score, highest first."""
    scored = [(score_result(r, query), r) for r in results]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]
