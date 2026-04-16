"""Concurrency helpers for batch HTTP operations.

Provides a shared configuration knob (``DOCSTATS_HTTP_CONCURRENCY``) and a semaphore
factory that batch callers — currently planned for batch NPI lookups (#47) — can use
to cap in-flight requests against a single API.

Usage::

    from docstats.concurrency import async_limiter

    limiter = async_limiter()  # uses env default, or async_limiter(10) to override

    async def lookup_one(npi: str) -> NPIResult:
        async with limiter:
            return await client.async_lookup(npi)

    results = await asyncio.gather(*(lookup_one(npi) for npi in npis))
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 5
_MIN_CONCURRENCY = 1


def get_default_concurrency() -> int:
    """Return the default concurrency cap, honoring ``DOCSTATS_HTTP_CONCURRENCY``."""
    raw = os.environ.get("DOCSTATS_HTTP_CONCURRENCY")
    if raw is None:
        return DEFAULT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid DOCSTATS_HTTP_CONCURRENCY=%r, using default", raw)
        return DEFAULT_CONCURRENCY
    if value < _MIN_CONCURRENCY:
        logger.warning(
            "DOCSTATS_HTTP_CONCURRENCY=%r below minimum (%d), using default",
            raw,
            _MIN_CONCURRENCY,
        )
        return DEFAULT_CONCURRENCY
    return value


def async_limiter(n: int | None = None) -> asyncio.Semaphore:
    """Return a new ``asyncio.Semaphore`` sized to ``n`` or the env default.

    Construct inside the running event loop — creating one at import time on Python
    < 3.10 binds it to the wrong loop.
    """
    size = n if n is not None else get_default_concurrency()
    if size < _MIN_CONCURRENCY:
        size = _MIN_CONCURRENCY
    return asyncio.Semaphore(size)
