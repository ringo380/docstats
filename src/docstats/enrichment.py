"""Provider enrichment orchestrator.

Fans out to multiple public API clients (OIG LEIE, CMS Medicare, Open Payments)
to enrich provider data beyond what NPPES provides. Each source is fetched in
parallel and cached independently with source-specific TTLs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Cache TTLs (seconds)
TTL_OIG = 30 * 86400       # 30 days (monthly CSV release)
TTL_MEDICARE = 7 * 86400   # 7 days (quarterly releases)
TTL_OPEN_PAYMENTS = 30 * 86400  # 30 days (annual release)


class EnrichmentData(BaseModel):
    """Aggregated enrichment data from multiple public APIs."""

    npi: str

    # OIG LEIE
    oig_excluded: bool | None = None  # None=unchecked, True=excluded, False=clean
    oig_exclusion_date: str | None = None
    oig_exclusion_type: str | None = None

    # CMS Medicare
    medicare_enrolled: bool | None = None
    medicare_primary_specialty: str | None = None
    medicare_credential: str | None = None
    medicare_medical_school: str | None = None
    medicare_graduation_year: str | None = None
    medicare_accepts_assignment: bool | None = None
    medicare_telehealth: bool | None = None
    group_affiliations: list[dict[str, str]] = []
    hospital_affiliations: list[dict[str, str]] = []

    # Open Payments (Phase 3)
    total_payments: float | None = None
    payment_count: int | None = None
    payment_year: int | None = None
    top_payers: list[dict[str, Any]] = []

    # Metadata
    fetched_at: datetime | None = None
    sources_checked: list[str] = []
    sources_failed: list[str] = []


class EnrichmentCache:
    """Source-aware cache for enrichment API responses.

    Stores raw JSON strings keyed by source + NPI, with per-source TTLs.
    Uses the same SQLite database as the main app.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_table()

    def _init_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS enrichment_cache (
                cache_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                response_json TEXT NOT NULL,
                cached_at TEXT NOT NULL DEFAULT (datetime('now')),
                ttl_seconds INTEGER NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_enrichment_cache_source
            ON enrichment_cache(source)
        """)
        self._conn.commit()

    @staticmethod
    def _make_key(source: str, npi: str) -> str:
        raw = f"{source}:{npi}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, source: str, npi: str) -> str | None:
        """Retrieve cached JSON for a source+NPI if not expired."""
        self._evict_expired()
        key = self._make_key(source, npi)
        row = self._conn.execute(
            "SELECT response_json FROM enrichment_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None

    def set(self, source: str, npi: str, response_json: str, ttl_seconds: int) -> None:
        """Store a response in the cache."""
        key = self._make_key(source, npi)
        self._conn.execute(
            """
            INSERT INTO enrichment_cache
                (cache_key, source, response_json, cached_at, ttl_seconds)
            VALUES (?, ?, ?, datetime('now'), ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                source=excluded.source,
                response_json=excluded.response_json,
                cached_at=datetime('now'),
                ttl_seconds=excluded.ttl_seconds
            """,
            (key, source, response_json, ttl_seconds),
        )
        self._conn.commit()

    def _evict_expired(self) -> None:
        self._conn.execute("""
            DELETE FROM enrichment_cache
            WHERE datetime(cached_at, '+' || ttl_seconds || ' seconds') < datetime('now')
        """)
        self._conn.commit()

    def clear(self, source: str | None = None) -> None:
        """Remove cached entries, optionally filtered by source."""
        if source:
            self._conn.execute("DELETE FROM enrichment_cache WHERE source = ?", (source,))
        else:
            self._conn.execute("DELETE FROM enrichment_cache")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


async def enrich_provider(npi: str, cache: EnrichmentCache) -> EnrichmentData:
    """Fetch enrichment data from all configured sources in parallel.

    Returns partial results if some sources fail — never raises.
    """
    sources_checked: list[str] = []
    sources_failed: list[str] = []
    data = EnrichmentData(npi=npi)

    # Import clients lazily to avoid circular imports and allow per-phase rollout
    tasks: list[tuple[str, asyncio.Task]] = []

    try:
        from docstats.oig_client import OIGClient  # noqa: F401
        task = asyncio.create_task(_fetch_oig(npi, cache))
        tasks.append(("oig", task))
    except ImportError:
        pass

    try:
        from docstats.cms_client import CMSClient  # noqa: F401
        task = asyncio.create_task(_fetch_medicare(npi, cache))
        tasks.append(("medicare", task))
    except ImportError:
        pass

    try:
        from docstats.open_payments_client import OpenPaymentsClient  # noqa: F401
        task = asyncio.create_task(_fetch_open_payments(npi, cache))
        tasks.append(("open_payments", task))
    except ImportError:
        pass

    # Await all tasks
    for source_name, task in tasks:
        try:
            result = await task
            sources_checked.append(source_name)
            if source_name == "oig" and result is not None:
                data.oig_excluded = result.get("excluded", False)
                data.oig_exclusion_date = result.get("exclusion_date")
                data.oig_exclusion_type = result.get("exclusion_type")
            elif source_name == "oig":
                # API responded, provider not found = not excluded
                data.oig_excluded = False
            elif source_name == "medicare" and result is not None:
                data.medicare_enrolled = result.get("enrolled", False)
                data.medicare_primary_specialty = result.get("primary_specialty") or None
                data.medicare_credential = result.get("credential") or None
                data.medicare_medical_school = result.get("medical_school") or None
                data.medicare_graduation_year = result.get("graduation_year") or None
                data.medicare_accepts_assignment = result.get("accepts_assignment")
                data.medicare_telehealth = result.get("telehealth")
                data.group_affiliations = result.get("group_affiliations", [])
                data.hospital_affiliations = result.get("hospital_affiliations", [])
            elif source_name == "medicare":
                data.medicare_enrolled = False
            elif source_name == "open_payments" and result is not None:
                data.total_payments = result.get("total_payments")
                data.payment_count = result.get("payment_count")
                data.payment_year = result.get("payment_year")
                data.top_payers = result.get("top_payers", [])
        except Exception:
            logger.exception("Enrichment source %s failed for NPI %s", source_name, npi)
            sources_failed.append(source_name)

    data.sources_checked = sources_checked
    data.sources_failed = sources_failed
    data.fetched_at = datetime.now(tz=timezone.utc)
    return data


async def _fetch_oig(npi: str, cache: EnrichmentCache) -> dict | None:
    """Check OIG LEIE for exclusion status. Returns dict if excluded, None if clean."""
    # Check cache first (SQLite is fast, no executor needed)
    cached = cache.get("oig", npi)
    if cached is not None:
        return json.loads(cached)

    def _sync_fetch() -> dict | None:
        from docstats.oig_client import OIGClient
        client = OIGClient()
        try:
            return client.check_exclusion(npi)
        finally:
            client.close()

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync_fetch)
    # Cache the result (even "not excluded" to avoid re-fetching)
    cache_value = json.dumps(result) if result else "null"
    cache.set("oig", npi, cache_value, TTL_OIG)
    return result


async def _fetch_medicare(npi: str, cache: EnrichmentCache) -> dict | None:
    """Fetch Medicare enrollment and facility affiliation data from CMS."""
    cached = cache.get("medicare", npi)
    if cached is not None:
        return json.loads(cached)

    def _sync_fetch() -> dict | None:
        from docstats.cms_client import CMSClient
        client = CMSClient()
        try:
            clinician = client.lookup_clinician(npi)
            if clinician is None:
                return None
            facilities = client.lookup_facility_affiliations(npi)
            clinician["hospital_affiliations"] = facilities
            return clinician
        finally:
            client.close()

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync_fetch)
    cache_value = json.dumps(result) if result else "null"
    cache.set("medicare", npi, cache_value, TTL_MEDICARE)
    return result


async def _fetch_open_payments(npi: str, cache: EnrichmentCache) -> dict | None:
    """Fetch industry payment data from CMS Open Payments."""
    cached = cache.get("open_payments", npi)
    if cached is not None:
        return json.loads(cached)

    def _sync_fetch() -> dict | None:
        from docstats.open_payments_client import OpenPaymentsClient
        client = OpenPaymentsClient()
        try:
            return client.lookup_payments(npi)
        finally:
            client.close()

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync_fetch)
    cache_value = json.dumps(result) if result else "null"
    cache.set("open_payments", npi, cache_value, TTL_OPEN_PAYMENTS)
    return result
