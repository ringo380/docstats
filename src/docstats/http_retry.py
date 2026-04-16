"""Shared HTTP retry logic and timeout/retry configuration for external API clients.

Environment variables
---------------------
- ``DOCSTATS_HTTP_TIMEOUT``      — default httpx request timeout (seconds, float).
- ``DOCSTATS_HTTP_MAX_RETRIES``  — number of retries after the initial attempt (int).

Both are read at call time (not import time) so tests and deployments can adjust
behavior without re-importing the module.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# Built-in fallbacks used when env vars are unset or invalid.
# Default retry profile: 3 retries with backoff_base 2.0 (i.e. 2**attempt) → 1s, 2s, 4s.
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_MIN_RETRY_AFTER_SECONDS = 0.5


def get_default_timeout() -> float:
    """Return the default httpx timeout (seconds), honoring DOCSTATS_HTTP_TIMEOUT."""
    raw = os.environ.get("DOCSTATS_HTTP_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid DOCSTATS_HTTP_TIMEOUT=%r, using default", raw)
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning("Non-positive DOCSTATS_HTTP_TIMEOUT=%r, using default", raw)
        return DEFAULT_TIMEOUT_SECONDS
    return value


def get_default_max_retries() -> int:
    """Return the default retry count, honoring DOCSTATS_HTTP_MAX_RETRIES."""
    raw = os.environ.get("DOCSTATS_HTTP_MAX_RETRIES")
    if raw is None:
        return DEFAULT_MAX_RETRIES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid DOCSTATS_HTTP_MAX_RETRIES=%r, using default", raw)
        return DEFAULT_MAX_RETRIES
    if value < 0:
        logger.warning("Negative DOCSTATS_HTTP_MAX_RETRIES=%r, using default", raw)
        return DEFAULT_MAX_RETRIES
    return value


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse an integer-seconds ``Retry-After`` header.

    Returns the delay (clamped to >= ``_MIN_RETRY_AFTER_SECONDS``) or ``None`` if the
    header is missing, non-numeric, or below the clamp. HTTP-date form is not supported
    — the APIs we talk to only emit integer seconds.
    """
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        value = float(header)
    except (TypeError, ValueError):
        return None
    if value < _MIN_RETRY_AFTER_SECONDS:
        return None
    return value


def request_with_retry(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    label: str = "API",
    max_retries: int | None = None,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    retryable_status: frozenset[int] = DEFAULT_RETRYABLE_STATUS,
    error_class: type[Exception] = Exception,
    **kwargs,
) -> httpx.Response:
    """Execute an HTTP request with exponential backoff retry.

    Delays follow ``backoff_base ** attempt`` (1s, 2s, 4s with the defaults). When the
    server returns ``Retry-After`` on a retryable status, the header value overrides the
    computed delay.

    Returns the successful response (status 200).
    Raises *error_class* on non-retryable failure or exhausted retries. When exhaustion
    is caused by a transport error (timeout / connect / read), the underlying exception
    is attached as ``__cause__``; when exhaustion is caused by repeated retryable status
    codes, ``__cause__`` is ``None`` because the last operation was a successful HTTP
    round-trip with a non-200 status.
    """
    if max_retries is None:
        max_retries = get_default_max_retries()

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = http.request(method, url, **kwargs)
            if resp.status_code == 200:
                return resp
            if resp.status_code in retryable_status and attempt < max_retries:
                delay = _retry_after_seconds(resp) or backoff_base**attempt
                logger.warning(
                    "%s returned %d, retrying in %.1fs (attempt %d/%d)",
                    label,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
                continue
            raise error_class(f"{label} returned {resp.status_code}")
        except httpx.TimeoutException as e:
            last_error = e
            if attempt < max_retries:
                delay = backoff_base**attempt
                logger.warning(
                    "%s timed out, retrying in %.1fs (attempt %d/%d)",
                    label,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
                continue
        except httpx.RequestError as e:
            last_error = e
            if attempt < max_retries:
                delay = backoff_base**attempt
                logger.warning(
                    "%s error: %s, retrying in %.1fs (attempt %d/%d)",
                    label,
                    e,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
                continue

    raise error_class(
        f"{label} failed after {max_retries + 1} attempts: {last_error}"
    ) from last_error
