"""Epic SMART-on-FHIR sandbox client (Phase 12.A).

Synchronous httpx wrapped in `request_with_retry` for consistent timeout +
retry policy. Route layer wraps these calls in an executor when invoked from
async handlers.

Confidential client: client_secret is sent on the token endpoint via HTTP
Basic auth (Epic's preferred form). Public-client / PKCE-only is not used —
referme.help is registered as confidential.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from docstats.http_retry import (
    get_default_max_retries,
    get_default_timeout,
    request_with_retry,
)

logger = logging.getLogger(__name__)


class EpicError(RuntimeError):
    """Epic SMART-on-FHIR call failed."""


DEFAULT_BASE_URL = "https://fhir.epic.com/interconnect-fhir-oauth"
DISCOVERY_PATH = "/.well-known/smart-configuration"
DISCOVERY_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class EpicEndpoints:
    """Resolved auth/token/fhir endpoints from .well-known discovery."""

    authorize_endpoint: str
    token_endpoint: str
    fhir_base: str  # FHIR R4 root for Patient/{id} reads


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int  # seconds
    scope: str
    patient_fhir_id: str | None
    id_token: str | None


# Module-level discovery cache: { base_url: (endpoints, fetched_at_unix) }
_DISCOVERY_CACHE: dict[str, tuple[EpicEndpoints, float]] = {}


def _client_id() -> str:
    cid = os.getenv("EPIC_CLIENT_ID", "").strip()
    if not cid:
        raise EpicError("EPIC_CLIENT_ID not set")
    return cid


def _client_secret() -> str:
    sec = os.getenv("EPIC_CLIENT_SECRET", "").strip()
    if not sec:
        raise EpicError("EPIC_CLIENT_SECRET not set")
    return sec


def _redirect_uri() -> str:
    uri = os.getenv("EPIC_REDIRECT_URI", "").strip()
    if not uri:
        raise EpicError("EPIC_REDIRECT_URI not set")
    return uri


def _base_url() -> str:
    return os.getenv("EPIC_SANDBOX_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _basic_auth_header() -> str:
    raw = f"{_client_id()}:{_client_secret()}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def discover(*, force_refresh: bool = False) -> EpicEndpoints:
    """Fetch + cache `.well-known/smart-configuration`.

    The fhir_base returned by Epic discovery points at the FHIR R4 root.
    Cached for 24h in-process; force_refresh bypasses the cache.
    """
    base = _base_url()
    if not force_refresh:
        cached = _DISCOVERY_CACHE.get(base)
        if cached and (time.time() - cached[1]) < DISCOVERY_TTL_SECONDS:
            return cached[0]

    url = f"{base}{DISCOVERY_PATH}"
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(http, "GET", url, label="Epic discovery", error_class=EpicError)
    payload = resp.json()
    try:
        endpoints = EpicEndpoints(
            authorize_endpoint=payload["authorization_endpoint"],
            token_endpoint=payload["token_endpoint"],
            # SMART discovery exposes capabilities + endpoints; FHIR base is
            # advertised under "fhir_base" or derivable from the iss path. Epic
            # consistently exposes it as "issuer" pointing at fhir-root.
            fhir_base=payload.get("issuer") or payload.get("fhir_base") or base,
        )
    except KeyError as e:
        raise EpicError(f"Epic discovery missing field: {e}") from e

    _DISCOVERY_CACHE[base] = (endpoints, time.time())
    return endpoints


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256.

    Used even on confidential clients — Epic supports PKCE alongside
    client_secret and it's harmless extra defense against code interception.
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def make_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorize_url(*, state: str, code_challenge: str, scope: str) -> str:
    """Construct the Epic authorize URL for standalone launch."""
    endpoints = discover()
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "aud": endpoints.fhir_base,
    }
    return f"{endpoints.authorize_endpoint}?{urlencode(params)}"


def exchange_code(*, code: str, code_verifier: str) -> TokenResponse:
    """POST authorization code → access token."""
    endpoints = discover()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": code_verifier,
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http,
            "POST",
            endpoints.token_endpoint,
            label="Epic token exchange",
            error_class=EpicError,
            max_retries=get_default_max_retries(),
            data=data,
            headers=headers,
        )
    payload = resp.json()
    if "access_token" not in payload:
        raise EpicError(f"Epic token response missing access_token: {payload!r}")
    return TokenResponse(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", ""),
        patient_fhir_id=payload.get("patient"),
        id_token=payload.get("id_token"),
    )


def refresh(refresh_token: str) -> TokenResponse:
    """Exchange a refresh_token for a new access_token."""
    endpoints = discover()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http,
            "POST",
            endpoints.token_endpoint,
            label="Epic token refresh",
            error_class=EpicError,
            data=data,
            headers=headers,
        )
    payload = resp.json()
    if "access_token" not in payload:
        raise EpicError(f"Epic refresh response missing access_token: {payload!r}")
    return TokenResponse(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or refresh_token,
        expires_in=int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", ""),
        patient_fhir_id=payload.get("patient"),
        id_token=payload.get("id_token"),
    )


def fetch_patient(*, access_token: str, patient_fhir_id: str) -> dict:
    """GET Patient/{id} from Epic's FHIR R4 endpoint."""
    endpoints = discover()
    url = f"{endpoints.fhir_base.rstrip('/')}/Patient/{patient_fhir_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http, "GET", url, label="Epic Patient.read", error_class=EpicError, headers=headers
        )
    return resp.json()  # type: ignore[no-any-return]
