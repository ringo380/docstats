"""ZIP code centroid lookup for distance-based provider scoring.

Loads ~33K US ZIP centroids from a bundled gzipped JSON file.
Data source: US Census Bureau ZCTA centroids (2013 Gazetteer).
"""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

_DATA_FILE = Path(__file__).parent / "zip_centroids.json.gz"
_cache: dict[str, tuple[float, float]] | None = None


def _load() -> dict[str, tuple[float, float]]:
    global _cache
    if _cache is not None:
        return _cache
    with gzip.open(_DATA_FILE, "rt", encoding="utf-8") as f:
        raw = json.load(f)
    _cache = {k: (v[0], v[1]) for k, v in raw.items()}
    return _cache


def zip_to_coords(postal_code: str) -> tuple[float, float] | None:
    """Look up centroid (lat, lon) for a 5-digit ZIP code."""
    data = _load()
    zip5 = postal_code[:5] if postal_code else ""
    return data.get(zip5)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    R = 3959.0  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))
