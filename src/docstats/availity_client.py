"""Availity Healthcare HIPAA Transactions API client.

Handles OAuth2 client_credentials token management and the two primary
endpoints used in Phase 11:

  - POST /availity/v1/coverages  — eligibility inquiry (X12 270/271)
  - GET  /availity/v1/payers     — payer directory

Token lifecycle:
    Availity tokens expire after 300 seconds (5 minutes).  This client
    caches the token in-process and refreshes automatically when it's
    within TOKEN_REFRESH_BUFFER_SECONDS of expiry.

Rate limits:
    Sandbox demo tier: 500 calls/day, 5 calls/second.
    The ``_rate_limiter`` asyncio.Semaphore caps web-route concurrency.
    CLI/sync callers use ``_sync_throttle`` (simple per-call sleep).

Environment variables (read at call time):
    AVAILITY_API_KEY       — OAuth2 client_id (required)
    AVAILITY_API_SECRET    — OAuth2 client_secret (required)
    AVAILITY_ENVIRONMENT   — "sandbox" or "production" (default: sandbox)
    AVAILITY_EB_VALUE_ADDS_KEY — optional; activates extended benefits tier
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

from docstats.http_retry import get_default_timeout, request_with_retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.availity.com"
TOKEN_URL = f"{BASE_URL}/v1/token"
COVERAGES_URL = f"{BASE_URL}/availity/v1/coverages"
PAYERS_URL = f"{BASE_URL}/availity/v1/payers"

SANDBOX_SCOPE = "healthcare-hipaa-transactions-demo"
PRODUCTION_SCOPE = "healthcare-hipaa-transactions"

TOKEN_REFRESH_BUFFER_SECONDS = 30  # refresh when ≤30s left

# Sandbox demo tier: 5 calls/second hard cap.
# We use a semaphore of 4 plus per-call 0.25s sleep to stay comfortably under.
_RATE_LIMIT_CONCURRENCY = 4
_RATE_LIMIT_SLEEP = 0.25  # seconds between sync calls


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AvailityError(Exception):
    """Unrecoverable Availity API error."""


class AvailityUnavailableError(AvailityError):
    """Transient Availity error (429 / 5xx / timeout) — retry is appropriate."""


class AvailityDisabledError(AvailityError):
    """Raised when AVAILITY_API_KEY or AVAILITY_API_SECRET are not set."""


# ---------------------------------------------------------------------------
# Token cache (module-level, process-scoped)
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {}


def _is_environment() -> str:
    return os.environ.get("AVAILITY_ENVIRONMENT", "sandbox").lower()


def _get_credentials() -> tuple[str, str]:
    api_key = os.environ.get("AVAILITY_API_KEY", "")
    api_secret = os.environ.get("AVAILITY_API_SECRET", "")
    if not api_key or not api_secret:
        raise AvailityDisabledError("AVAILITY_API_KEY and AVAILITY_API_SECRET must be set")
    return api_key, api_secret


def _current_scope() -> str:
    env = _is_environment()
    return SANDBOX_SCOPE if env == "sandbox" else PRODUCTION_SCOPE


def _token_is_fresh() -> bool:
    expires_at = float(_token_cache.get("expires_at", 0.0))
    return time.time() < expires_at - TOKEN_REFRESH_BUFFER_SECONDS


def _store_token(token_data: dict) -> str:
    expires_in = int(token_data.get("expires_in", 300))
    _token_cache["access_token"] = token_data["access_token"]
    _token_cache["expires_at"] = time.time() + expires_in
    return str(token_data["access_token"])


# ---------------------------------------------------------------------------
# AvailityClient
# ---------------------------------------------------------------------------


class AvailityClient:
    """Sync + async Availity HIPAA Transactions client.

    Instantiate once per process (or per test).  The sync methods run
    token refresh and API calls on the calling thread.  The async methods
    wrap them in an executor so they're safe to call from FastAPI routes.

    Usage::

        client = AvailityClient()
        result = client.check_eligibility({
            "payerId": "BCBSM",
            "providerNpi": "1234567890",
            "memberId": "XYZ123",
            "patientBirthDate": "1980-01-15",
            "patientLastName": "Smith",
            "patientFirstName": "Jane",
            "serviceType": "30",
        })
    """

    def __init__(self) -> None:
        timeout = get_default_timeout()
        self._http = httpx.Client(timeout=timeout)
        self._rate_limiter: asyncio.Semaphore | None = None
        self._last_sync_call: float = 0.0

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        if _token_is_fresh():
            return str(_token_cache["access_token"])

        api_key, api_secret = _get_credentials()
        scope = _current_scope()

        logger.debug("Refreshing Availity OAuth2 token (env=%s)", _is_environment())
        try:
            resp = request_with_retry(
                self._http,
                "POST",
                TOKEN_URL,
                label="Availity token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": api_key,
                    "client_secret": api_secret,
                    "scope": scope,
                },
                retryable_status=frozenset({429, 500, 502, 503, 504}),
                error_class=AvailityUnavailableError,
            )
        except AvailityUnavailableError:
            raise
        except Exception as e:
            raise AvailityError(f"Token request failed: {e}") from e

        try:
            data = resp.json()
        except Exception as e:
            raise AvailityError(f"Token response not JSON: {e}") from e

        if "access_token" not in data:
            raise AvailityError(f"No access_token in response: {data}")

        return _store_token(data)

    # ------------------------------------------------------------------
    # Sync API calls
    # ------------------------------------------------------------------

    def _sync_headers(self, scenario_id: str | None = None) -> dict[str, str]:
        token = self._get_token()
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if scenario_id:
            headers["X-Api-Mock-Scenario-ID"] = scenario_id
        return headers

    def _throttle(self) -> None:
        """Ensure at least _RATE_LIMIT_SLEEP seconds between sync calls."""
        elapsed = time.time() - self._last_sync_call
        if elapsed < _RATE_LIMIT_SLEEP:
            time.sleep(_RATE_LIMIT_SLEEP - elapsed)
        self._last_sync_call = time.time()

    def check_eligibility(
        self,
        payload: dict[str, Any],
        *,
        scenario_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit an eligibility inquiry and return the parsed 271 JSON.

        ``payload`` must contain at minimum:
            payerId, providerNpi, memberId, patientBirthDate,
            patientLastName, patientFirstName, serviceType

        ``scenario_id`` activates Availity sandbox mock scenarios when set.
        """
        self._throttle()
        headers = self._sync_headers(scenario_id)

        try:
            resp = request_with_retry(
                self._http,
                "POST",
                COVERAGES_URL,
                label="Availity eligibility",
                headers=headers,
                content=json.dumps(payload),
                retryable_status=frozenset({429, 500, 502, 503, 504}),
                error_class=AvailityUnavailableError,
            )
        except AvailityUnavailableError:
            raise
        except Exception as e:
            raise AvailityError(f"Eligibility request failed: {e}") from e

        try:
            return resp.json()  # type: ignore[no-any-return]
        except Exception as e:
            raise AvailityError(f"Eligibility response not JSON: {e}") from e

    def list_payers(
        self,
        *,
        state: str | None = None,
        service_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the Availity payer directory, optionally filtered."""
        self._throttle()
        headers = self._sync_headers()

        params: dict[str, str] = {}
        if state:
            params["state"] = state
        if service_type:
            params["serviceType"] = service_type

        try:
            resp = request_with_retry(
                self._http,
                "GET",
                PAYERS_URL,
                label="Availity payers",
                headers=headers,
                params=params or None,
                retryable_status=frozenset({429, 500, 502, 503, 504}),
                error_class=AvailityUnavailableError,
            )
        except AvailityUnavailableError:
            raise
        except Exception as e:
            raise AvailityError(f"Payers request failed: {e}") from e

        try:
            data = resp.json()
        except Exception as e:
            raise AvailityError(f"Payers response not JSON: {e}") from e

        # Response is either {"payers": [...]} or a bare list
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        return data.get("payers") or []  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Async wrappers (for FastAPI routes)
    # ------------------------------------------------------------------

    def _get_rate_limiter(self) -> asyncio.Semaphore:
        if self._rate_limiter is None:
            self._rate_limiter = asyncio.Semaphore(_RATE_LIMIT_CONCURRENCY)
        return self._rate_limiter

    async def async_check_eligibility(
        self,
        payload: dict[str, Any],
        *,
        scenario_id: str | None = None,
    ) -> dict[str, Any]:
        """Async wrapper around check_eligibility for use in FastAPI routes."""
        loop = asyncio.get_running_loop()
        limiter = self._get_rate_limiter()
        async with limiter:
            return await loop.run_in_executor(
                None, lambda: self.check_eligibility(payload, scenario_id=scenario_id)
            )

    async def async_list_payers(
        self,
        *,
        state: str | None = None,
        service_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Async wrapper around list_payers for use in FastAPI routes."""
        loop = asyncio.get_running_loop()
        limiter = self._get_rate_limiter()
        async with limiter:
            return await loop.run_in_executor(
                None, lambda: self.list_payers(state=state, service_type=service_type)
            )


# ---------------------------------------------------------------------------
# Module-level singleton for web routes
# ---------------------------------------------------------------------------

_client: AvailityClient | None = None


def get_availity_client() -> AvailityClient:
    """Return the module-level AvailityClient singleton.

    Raises AvailityDisabledError if credentials are not configured.
    """
    global _client
    _get_credentials()  # raises immediately if env vars missing
    if _client is None:
        _client = AvailityClient()
    return _client
