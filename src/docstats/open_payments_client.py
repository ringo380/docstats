"""CMS Open Payments (Sunshine Act) client.

Fetches industry payment data for healthcare providers from the CMS
Open Payments public dataset. No authentication required.

API pattern (POST):
    https://openpaymentsdata.cms.gov/api/1/datastore/query/{dataset_id}/0
    Body: {"conditions": [...], "limit": N, "properties": [...]}

Queries the most recent program year first, falls back to prior year.
"""

from __future__ import annotations

import logging

import httpx

from docstats.http_retry import request_with_retry

logger = logging.getLogger(__name__)

API_BASE = "https://openpaymentsdata.cms.gov/api/1/datastore/query"

# General Payment dataset IDs by year (most recent first)
DATASET_IDS = {
    2024: "e6b17c6a-2534-4207-a4a1-6746a14911ff",
    2023: "fb3a65aa-c901-4a38-a813-b04b00dfa2a9",
    2022: "df01c2f8-dc1f-4e79-96cb-8208beaf143c",
}
YEARS_TO_TRY = [2024, 2023]

# Only fetch fields we need to reduce response size
PROPERTIES = [
    "covered_recipient_npi",
    "total_amount_of_payment_usdollars",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name",
    "nature_of_payment_or_transfer_of_value",
    "program_year",
]

REQUEST_TIMEOUT = 30.0
MAX_ROWS = 200  # Enough to aggregate; very few providers exceed this


class OpenPaymentsError(Exception):
    """Open Payments API failure."""


class OpenPaymentsClient:
    """Client for CMS Open Payments data.

    Aggregates individual payment records into a summary per provider.
    """

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=REQUEST_TIMEOUT)

    def lookup_payments(self, npi: str) -> dict | None:
        """Fetch and aggregate payment data for a provider NPI.

        Tries the most recent program year first; falls back to prior year
        if no results. Returns None if no payments found in any year.
        """
        for year in YEARS_TO_TRY:
            dataset_id = DATASET_IDS.get(year)
            if not dataset_id:
                continue

            rows = self._query(dataset_id, npi)
            if rows:
                return self._aggregate(rows, year)

        return None

    def _aggregate(self, rows: list[dict], year: int) -> dict:
        """Aggregate payment rows into a summary."""
        total = 0.0
        payer_totals: dict[str, float] = {}

        for row in rows:
            try:
                amount = float(row.get("total_amount_of_payment_usdollars", 0))
            except (ValueError, TypeError):
                amount = 0.0

            total += amount

            payer_name = (
                row.get("applicable_manufacturer_or_applicable_gpo_making_payment_name") or "Unknown"
            ).strip()
            payer_totals[payer_name] = payer_totals.get(payer_name, 0.0) + amount

        # Sort payers by total descending
        top_payers = [
            {"name": name, "amount": round(amt, 2)}
            for name, amt in sorted(payer_totals.items(), key=lambda x: x[1], reverse=True)
        ]

        return {
            "total_payments": round(total, 2),
            "payment_count": len(rows),
            "payment_year": year,
            "top_payers": top_payers[:10],  # Top 10 payers
        }

    def _query(self, dataset_id: str, npi: str) -> list[dict]:
        """Execute a DKAN datastore query filtered by NPI."""
        url = f"{API_BASE}/{dataset_id}/0"
        body = {
            "conditions": [
                {"property": "covered_recipient_npi", "value": npi, "operator": "="}
            ],
            "limit": MAX_ROWS,
            "properties": PROPERTIES,
        }
        resp = request_with_retry(
            self._http, "POST", url,
            json=body,
            label="Open Payments API",
            error_class=OpenPaymentsError,
        )
        return resp.json().get("results", [])

    def close(self) -> None:
        self._http.close()
