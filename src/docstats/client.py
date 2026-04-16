"""NPPES NPI Registry API client."""

from __future__ import annotations

import asyncio
import functools
import logging
import re

import httpx

from docstats.cache import ResponseCache
from docstats.concurrency import async_limiter
from docstats.http_retry import get_default_timeout, request_with_retry
from docstats.models import NPIResponse, NPIResult

logger = logging.getLogger(__name__)

API_BASE = "https://npiregistry.cms.hhs.gov/api/"
API_VERSION = "2.1"
DEFAULT_LIMIT = 10
MAX_LIMIT = 1200


class NPPESError(Exception):
    """Raised when the NPPES API returns an error."""


class _NPPESRetryExhausted(Exception):
    """Internal marker raised by request_with_retry; translated to NPPESError below."""


# Translate cryptic API errors into user-friendly messages
_ERROR_TRANSLATIONS = {
    "combination of individual name and organization name": (
        "Cannot search by individual name and organization name at the same time. "
        "Use the Individual or Organization tab to search one at a time."
    ),
    "at least two characters": ("Name fields require at least 2 characters."),
    "cannot be the only criteria": (
        "State alone is not enough to search. Add a name, specialty, or other filter."
    ),
}


def _translate_error(raw_msg: str) -> str:
    """Replace known NPPES API error messages with user-friendly versions."""
    lower = raw_msg.lower()
    for pattern, friendly in _ERROR_TRANSLATIONS.items():
        if pattern in lower:
            return friendly
    return raw_msg


class NPPESClient:
    """Synchronous client for the CMS NPPES NPI Registry API v2.1."""

    def __init__(self, cache: ResponseCache | None = None) -> None:
        self._http = httpx.Client(timeout=get_default_timeout())
        self._cache = cache

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> NPPESClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search(
        self,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        organization_name: str | None = None,
        taxonomy_description: str | None = None,
        city: str | None = None,
        state: str | None = None,
        postal_code: str | None = None,
        enumeration_type: str | None = None,
        use_first_name_alias: bool = False,
        limit: int = DEFAULT_LIMIT,
        skip: int = 0,
        use_cache: bool = True,
    ) -> NPIResponse:
        """Search providers by various criteria.

        All parameters map directly to documented NPPES API fields.
        At least one search criterion beyond state is required.
        """
        params: dict[str, str] = {"version": API_VERSION}

        if first_name:
            params["first_name"] = first_name.strip()
        if last_name:
            params["last_name"] = last_name.strip()
        if organization_name:
            params["organization_name"] = organization_name.strip()
        if taxonomy_description:
            params["taxonomy_description"] = taxonomy_description.strip()
        if city:
            params["city"] = city.strip()
        if state:
            params["state"] = state.strip().upper()
        if postal_code:
            params["postal_code"] = postal_code.strip()
        if enumeration_type:
            params["enumeration_type"] = enumeration_type.strip()
        if use_first_name_alias:
            params["use_first_name_alias"] = "True"

        # Validate we have at least one real search param
        search_params = {k for k in params if k not in ("version", "state")}
        if not search_params:
            raise NPPESError("At least one search parameter is required (name, specialty, etc.)")

        params["limit"] = str(min(limit, MAX_LIMIT))
        if skip > 0:
            params["skip"] = str(skip)

        return self._execute(params, use_cache=use_cache)

    def lookup(self, npi: str, *, use_cache: bool = True) -> NPIResult | None:
        """Look up a single provider by exact NPI number.

        Returns None if not found. Raises NPPESError on invalid format.
        """
        npi = npi.strip()
        if not re.match(r"^\d{10}$", npi):
            raise NPPESError(f"Invalid NPI format: '{npi}'. Must be exactly 10 digits.")

        params = {"version": API_VERSION, "number": npi}
        response = self._execute(params, use_cache=use_cache)

        if response.result_count == 0 or not response.results:
            return None
        return response.results[0]

    async def async_search(self, **kwargs) -> NPIResponse:
        """Async wrapper for search() — runs in a thread to avoid blocking the event loop."""
        return await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(self.search, **kwargs),
        )

    async def async_lookup(self, npi: str, **kwargs) -> NPIResult | None:
        """Async wrapper for lookup() — runs in a thread to avoid blocking the event loop."""
        return await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(self.lookup, npi, **kwargs),
        )

    async def async_lookup_many(
        self,
        npis: list[str],
        *,
        limiter: asyncio.Semaphore | None = None,
        use_cache: bool = True,
    ) -> list[NPIResult | None]:
        """Concurrently look up many NPIs, capped by ``limiter`` (or the env default).

        Results are returned in input order. Entries are ``None`` when an NPI is not
        found; ``NPPESError`` is re-raised from any worker that fails.
        """
        sem = limiter if limiter is not None else async_limiter()

        async def _one(npi: str) -> NPIResult | None:
            async with sem:
                return await self.async_lookup(npi, use_cache=use_cache)

        return await asyncio.gather(*(_one(npi) for npi in npis))

    def _execute(self, params: dict[str, str], *, use_cache: bool = True) -> NPIResponse:
        """Execute an API request with optional caching and automatic retry.

        Note: this client is synchronous — time.sleep() during retries will
        block the calling thread.  FastAPI async routes should call client
        methods via run_in_executor() to avoid stalling the event loop.
        """
        # Check cache first
        if use_cache and self._cache:
            cached = self._cache.get(params)
            if cached is not None:
                logger.debug("Cache hit for params: %s", params)
                return cached

        logger.debug("Requesting NPPES API: %s", params)

        try:
            resp = request_with_retry(
                self._http,
                "GET",
                API_BASE,
                label="NPPES API",
                error_class=_NPPESRetryExhausted,
                params=params,
            )
        except _NPPESRetryExhausted as e:
            raise NPPESError(_friendly_transport_error(e)) from e

        data = resp.json()

        # The API returns errors in an "Errors" field instead of HTTP status codes
        if "Errors" in data:
            errors = data["Errors"]
            if isinstance(errors, list):
                msgs = [e.get("description", str(e)) for e in errors]
                raw = "; ".join(msgs)
            else:
                raw = str(errors)
            raise NPPESError(_translate_error(raw))

        response = NPIResponse.model_validate(data)

        if use_cache and self._cache:
            self._cache.set(params, response)

        return response


def _friendly_transport_error(err: _NPPESRetryExhausted) -> str:
    """Map the low-level cause to a user-facing NPPES error message."""
    cause = err.__cause__
    # TimeoutException is a subclass of RequestError — check it first.
    if isinstance(cause, httpx.TimeoutException):
        return "The NPI Registry took too long to respond. Please try again."
    if isinstance(cause, httpx.RequestError):
        return "Could not reach the NPI Registry. Check your internet connection and try again."
    # Status-code exhaustion (no cause attached) falls through to the generic message.
    return "The NPI Registry is temporarily unavailable. Please try again."
