"""Shared HTTP retry logic for external API clients."""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def request_with_retry(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    label: str = "API",
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    retryable_status: frozenset[int] = DEFAULT_RETRYABLE_STATUS,
    error_class: type[Exception] = Exception,
    **kwargs,
) -> httpx.Response:
    """Execute an HTTP request with exponential backoff retry.

    Returns the successful response (status 200).
    Raises *error_class* on non-retryable failure or exhausted retries.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = http.request(method, url, **kwargs)
            if resp.status_code == 200:
                return resp
            if resp.status_code in retryable_status and attempt < max_retries:
                delay = backoff_base ** attempt
                logger.warning(
                    "%s returned %d, retrying in %.0fs", label, resp.status_code, delay,
                )
                time.sleep(delay)
                continue
            raise error_class(f"{label} returned {resp.status_code}")
        except httpx.TimeoutException as e:
            last_error = e
            if attempt < max_retries:
                delay = backoff_base ** attempt
                logger.warning("%s timed out, retrying in %.0fs", label, delay)
                time.sleep(delay)
                continue
        except httpx.RequestError as e:
            last_error = e
            if attempt < max_retries:
                delay = backoff_base ** attempt
                logger.warning("%s error: %s, retrying in %.0fs", label, e, delay)
                time.sleep(delay)
                continue

    raise error_class(f"{label} failed after {max_retries + 1} attempts: {last_error}")
