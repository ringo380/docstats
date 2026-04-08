"""OIG LEIE (List of Excluded Individuals/Entities) client.

Checks whether a healthcare provider is excluded from federal programs
by querying the OIG exclusion database. Uses the downloadable CSV as
the data source since OIG has no public REST API.

The CSV is downloaded once and cached locally (refreshed monthly).
NPI field is available for records since 2008.

CSV columns (per leie_record_layout.pdf):
    LASTNAME, FIRSTNAME, MIDNAME, BUSNAME, GENERAL, SPECIALTY,
    UPIN, NPI, DOB, ADDRESS, CITY, STATE, ZIP CODE, EXCLTYPE,
    EXCLDATE, REINDATE, WAIVERDATE, WAIVERSTATE
"""

from __future__ import annotations

import csv
import io
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

LEIE_CSV_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
LEIE_CACHE_DIR = Path.home() / ".local" / "share" / "docstats" / "leie"
LEIE_CACHE_FILE = LEIE_CACHE_DIR / "leie.csv"
LEIE_MAX_AGE = 30 * 86400  # 30 days

REQUEST_TIMEOUT = 60.0  # CSV download can be large
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 2.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OIGError(Exception):
    """OIG LEIE lookup failure."""


class OIGClient:
    """Client for checking provider exclusion status against OIG LEIE.

    Downloads the full LEIE CSV on first use, caches it locally, and
    builds an in-memory NPI index for fast lookups.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or LEIE_CACHE_DIR
        self._cache_file = self._cache_dir / "leie.csv"
        self._http = httpx.Client(timeout=REQUEST_TIMEOUT)
        self._npi_index: dict[str, dict] | None = None

    def check_exclusion(self, npi: str) -> dict | None:
        """Check if a provider NPI is on the LEIE exclusion list.

        Returns a dict with exclusion details if excluded, None if clean.
        """
        if not npi or len(npi) != 10:
            return None

        self._ensure_index()
        assert self._npi_index is not None

        return self._npi_index.get(npi)

    def _ensure_index(self) -> None:
        """Build the NPI index from cached or freshly downloaded CSV."""
        if self._npi_index is not None:
            return

        csv_text = self._get_csv()
        self._npi_index = {}
        reader = csv.DictReader(io.StringIO(csv_text))

        for row in reader:
            npi = (row.get("NPI") or "").strip()
            if not npi or len(npi) != 10:
                continue

            excl_date = (row.get("EXCLDATE") or "").strip()
            rein_date = (row.get("REINDATE") or "").strip()

            # Skip reinstated providers (they are no longer excluded)
            if rein_date:
                continue

            self._npi_index[npi] = {
                "excluded": True,
                "exclusion_date": _format_date(excl_date),
                "exclusion_type": (row.get("EXCLTYPE") or "").strip(),
                "last_name": (row.get("LASTNAME") or "").strip(),
                "first_name": (row.get("FIRSTNAME") or "").strip(),
                "business_name": (row.get("BUSNAME") or "").strip(),
                "state": (row.get("STATE") or "").strip(),
                "specialty": (row.get("SPECIALTY") or "").strip(),
            }

        logger.info("OIG LEIE index built: %d excluded NPIs", len(self._npi_index))

    def _get_csv(self) -> str:
        """Return CSV text from cache or download."""
        if self._cache_file.exists():
            age = time.time() - self._cache_file.stat().st_mtime
            if age < LEIE_MAX_AGE:
                logger.debug("Using cached LEIE CSV (age: %.0f days)", age / 86400)
                return self._cache_file.read_text(encoding="utf-8-sig")

        return self._download_csv()

    def _download_csv(self) -> str:
        """Download the LEIE CSV with retry logic."""
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.get(LEIE_CSV_URL)
                if resp.status_code == 200:
                    text = resp.text
                    # Cache to disk
                    self._cache_dir.mkdir(parents=True, exist_ok=True)
                    self._cache_file.write_text(text, encoding="utf-8-sig")
                    logger.info("Downloaded LEIE CSV (%d bytes)", len(text))
                    return text
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "LEIE download returned %d, retrying in %.0fs", resp.status_code, delay
                    )
                    time.sleep(delay)
                    continue
                raise OIGError(f"LEIE download failed with status {resp.status_code}")
            except httpx.TimeoutException as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE ** attempt
                    logger.warning("LEIE download timed out, retrying in %.0fs", delay)
                    time.sleep(delay)
                    continue
            except httpx.RequestError as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE ** attempt
                    logger.warning("LEIE download error: %s, retrying in %.0fs", e, delay)
                    time.sleep(delay)
                    continue

        # If download fails but we have stale cache, use it
        if self._cache_file.exists():
            logger.warning("Using stale LEIE cache after download failure")
            return self._cache_file.read_text(encoding="utf-8-sig")

        raise OIGError(f"Failed to download LEIE CSV after {MAX_RETRIES + 1} attempts: {last_error}")

    def close(self) -> None:
        self._http.close()


def _format_date(raw: str) -> str | None:
    """Convert LEIE date format (YYYYMMDD) to ISO date string."""
    if not raw or len(raw) != 8:
        return None
    try:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    except (ValueError, IndexError):
        return None
