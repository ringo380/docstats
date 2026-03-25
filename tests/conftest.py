"""Shared test fixtures."""

from __future__ import annotations

import pytest
from pathlib import Path
import tempfile

from docstats.storage import Storage
from docstats.cache import ResponseCache


SAMPLE_NPI1_RESULT = {
    "number": "1234567890",
    "enumeration_type": "NPI-1",
    "basic": {
        "first_name": "JOHN",
        "last_name": "SMITH",
        "middle_name": "ROBERT",
        "credential": "M.D.",
        "name_prefix": "DR.",
        "sex": "M",
        "sole_proprietor": "NO",
        "enumeration_date": "2005-05-23",
        "last_updated": "2023-01-15",
        "status": "A",
    },
    "addresses": [
        {
            "address_1": "123 MAIN STREET",
            "address_2": "SUITE 200",
            "address_purpose": "LOCATION",
            "city": "SAN FRANCISCO",
            "state": "CA",
            "postal_code": "941103518",
            "country_code": "US",
            "telephone_number": "4155551234",
            "fax_number": "4155551235",
        },
        {
            "address_1": "PO BOX 9999",
            "address_purpose": "MAILING",
            "city": "SAN FRANCISCO",
            "state": "CA",
            "postal_code": "94110",
            "country_code": "US",
            "telephone_number": "4155551234",
        },
    ],
    "taxonomies": [
        {
            "code": "207R00000X",
            "desc": "Internal Medicine",
            "primary": True,
            "license": "A12345",
            "state": "CA",
        },
        {
            "code": "207RC0000X",
            "desc": "Cardiovascular Disease",
            "primary": False,
            "license": "A12346",
            "state": "CA",
        },
    ],
    "identifiers": [],
    "other_names": [],
    "endpoints": [],
    "practiceLocations": [],
}

SAMPLE_NPI2_RESULT = {
    "number": "9876543210",
    "enumeration_type": "NPI-2",
    "basic": {
        "organization_name": "KAISER PERMANENTE MEDICAL CENTER",
        "organizational_subpart": "NO",
        "enumeration_date": "2006-11-22",
        "last_updated": "2022-08-01",
        "status": "A",
        "authorized_official_first_name": "JANE",
        "authorized_official_last_name": "DOE",
        "authorized_official_title_or_position": "CEO",
    },
    "addresses": [
        {
            "address_1": "4567 HOSPITAL DRIVE",
            "address_purpose": "LOCATION",
            "city": "WALNUT CREEK",
            "state": "CA",
            "postal_code": "945963600",
            "country_code": "US",
            "telephone_number": "9255551000",
            "fax_number": "9255551001",
        },
    ],
    "taxonomies": [
        {
            "code": "282N00000X",
            "desc": "General Acute Care Hospital",
            "primary": True,
            "state": "CA",
        },
    ],
    "identifiers": [],
    "other_names": [
        {
            "organization_name": "KAISER FOUNDATION HOSPITAL",
            "type": "Former Legal Business Name",
        }
    ],
    "endpoints": [],
    "practiceLocations": [],
}

SAMPLE_API_RESPONSE = {
    "result_count": 2,
    "results": [SAMPLE_NPI1_RESULT, SAMPLE_NPI2_RESULT],
}


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Return a path to a temporary database file."""
    return tmp_path / "test.db"


@pytest.fixture
def storage(tmp_db: Path) -> Storage:
    """Return a Storage instance using a temp database."""
    return Storage(db_path=tmp_db)


@pytest.fixture
def cache(tmp_db: Path) -> ResponseCache:
    """Return a ResponseCache instance using a temp database."""
    return ResponseCache(db_path=tmp_db, ttl_seconds=3600)
