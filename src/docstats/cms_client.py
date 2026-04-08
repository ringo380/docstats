"""CMS Provider Data client (data.cms.gov).

Fetches Medicare enrollment data and facility affiliations from the CMS
Provider Data Catalog DKAN API. No authentication required.

Datasets:
- mj5m-pzi6: National Downloadable File (clinician enrollment, group practice)
- 27ea-46a8: Facility Affiliation Data (hospital affiliations)

API pattern (POST):
    https://data.cms.gov/provider-data/api/1/datastore/query/{dataset_id}/0
    Body: {"conditions": [{"property": "npi", "value": "<npi>", "operator": "="}], "limit": 50}
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://data.cms.gov/provider-data/api/1/datastore/query"
DATASET_CLINICIAN = "mj5m-pzi6"
DATASET_FACILITY = "27ea-46a8"

REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 2.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CMSError(Exception):
    """CMS Provider Data API failure."""


class CMSClient:
    """Client for CMS Provider Data Catalog API.

    Fetches Medicare enrollment and facility affiliation data by NPI.
    """

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=REQUEST_TIMEOUT)

    def lookup_clinician(self, npi: str) -> dict | None:
        """Fetch clinician enrollment data from the National Downloadable File.

        Returns a dict with enrollment info, or None if not found.
        A provider with multiple group practices will have multiple rows;
        we aggregate them into one result.
        """
        rows = self._query(DATASET_CLINICIAN, npi)
        if not rows:
            return None

        # First row has the core clinician info
        first = rows[0]
        result: dict = {
            "enrolled": True,
            "primary_specialty": first.get("pri_spec", ""),
            "secondary_specialties": [],
            "credential": first.get("cred", ""),
            "medical_school": first.get("med_sch", ""),
            "graduation_year": first.get("grd_yr", ""),
            "accepts_assignment": first.get("ind_assgn") == "Y",
            "telehealth": first.get("telehlth") == "Y",
            "group_affiliations": [],
        }

        # Collect secondary specialties from first row
        for key in ("sec_spec_1", "sec_spec_2", "sec_spec_3", "sec_spec_4"):
            val = first.get(key, "").strip()
            if val:
                result["secondary_specialties"].append(val)

        # Collect unique group affiliations across all rows
        seen_groups: set[str] = set()
        for row in rows:
            name = (row.get("facility_name") or "").strip()
            pac_id = (row.get("org_pac_id") or "").strip()
            if name and name not in seen_groups:
                seen_groups.add(name)
                result["group_affiliations"].append({
                    "name": name,
                    "pac_id": pac_id,
                    "num_members": row.get("num_org_mem", ""),
                })

        return result

    def lookup_facility_affiliations(self, npi: str) -> list[dict]:
        """Fetch hospital/facility affiliations for a clinician.

        Returns a list of facility dicts (may be empty).
        """
        rows = self._query(DATASET_FACILITY, npi)
        facilities: list[dict] = []
        seen: set[str] = set()

        for row in rows:
            ccn = (row.get("facility_affiliations_certification_number") or "").strip()
            ftype = (row.get("facility_type") or "").strip()
            if ccn and ccn not in seen:
                seen.add(ccn)
                facilities.append({
                    "ccn": ccn,
                    "type": ftype,
                })

        return facilities

    def _query(self, dataset_id: str, npi: str) -> list[dict]:
        """Execute a DKAN datastore query filtered by NPI."""
        url = f"{API_BASE}/{dataset_id}/0"
        body = {
            "conditions": [
                {"property": "npi", "value": npi, "operator": "="}
            ],
            "limit": 50,
        }

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.post(url, json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("results", [])
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "CMS API returned %d for %s, retrying in %.0fs",
                        resp.status_code, dataset_id, delay,
                    )
                    time.sleep(delay)
                    continue
                raise CMSError(f"CMS API returned {resp.status_code} for dataset {dataset_id}")
            except httpx.TimeoutException as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE ** attempt
                    logger.warning("CMS API timeout for %s, retrying in %.0fs", dataset_id, delay)
                    time.sleep(delay)
                    continue
            except httpx.RequestError as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE ** attempt
                    logger.warning("CMS API error: %s, retrying in %.0fs", e, delay)
                    time.sleep(delay)
                    continue

        raise CMSError(f"CMS API failed after {MAX_RETRIES + 1} attempts: {last_error}")

    async def async_lookup_clinician(self, npi: str) -> dict | None:
        """Async wrapper for lookup_clinician."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.lookup_clinician, npi)

    async def async_lookup_facilities(self, npi: str) -> list[dict]:
        """Async wrapper for lookup_facility_affiliations."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.lookup_facility_affiliations, npi)

    def close(self) -> None:
        self._http.close()
