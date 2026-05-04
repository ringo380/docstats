"""eClinicalWorks (eCW) SMART-on-FHIR client (Phase 12.D).

Synchronous httpx wrapped in `request_with_retry`. Route layer wraps these
calls in an executor when invoked from async handlers.

Confidential client: client_secret is sent on the token endpoint via HTTP
Basic auth (same pattern as Epic). PKCE is layered on top — eCW supports
PKCE for confidential clients and it's harmless extra defense against code
interception.

Key differences from Epic:
- ``aud`` parameter IS required on the authorize URL (eCW rejects without).
- Medications use ``MedicationRequest`` (Cerner-style, not Epic's
  ``MedicationStatement``).
- Multi-tenant: eCW has no single sandbox FHIR root. ``ECW_SANDBOX_FHIR_BASE``
  is a hard-required env var (no fallback). Production multi-practice support
  (per-practice FHIR base discovery) is deferred — Phase 12.D ships against
  one configured sandbox tenant.

Key differences from Cerner:
- Confidential client (Basic auth header) — Cerner is public/PKCE-only.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from docstats.ehr import registry as _ehr_registry
from docstats.ehr.registry import EHRError
from docstats.http_retry import (
    get_default_timeout,
    request_with_retry,
)

logger = logging.getLogger(__name__)

_TOKEN_FIELDS: frozenset[str] = frozenset(
    {"access_token", "refresh_token", "id_token", "code", "client_secret"}
)


def _redact(payload: object) -> object:
    if isinstance(payload, dict):
        return {k: ("***" if k in _TOKEN_FIELDS else _redact(v)) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


class ECWError(EHRError):
    """eClinicalWorks SMART-on-FHIR call failed."""


DISCOVERY_PATH = "/.well-known/smart-configuration"
DISCOVERY_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class ECWEndpoints:
    """Resolved auth/token/fhir endpoints from .well-known discovery."""

    authorize_endpoint: str
    token_endpoint: str
    fhir_base: str


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str
    patient_fhir_id: str | None
    id_token: str | None


_DISCOVERY_CACHE: dict[str, tuple[ECWEndpoints, float]] = {}


def reset_discovery_cache() -> None:
    """Clear the in-process discovery cache (test hook + post-rotation refresh)."""
    _DISCOVERY_CACHE.clear()


def _client_id() -> str:
    cid = os.getenv("ECW_CLIENT_ID", "").strip()
    if not cid:
        raise ECWError("ECW_CLIENT_ID not set")
    return cid


def _client_secret() -> str:
    sec = os.getenv("ECW_CLIENT_SECRET", "").strip()
    if not sec:
        raise ECWError("ECW_CLIENT_SECRET not set")
    return sec


def _redirect_uri() -> str:
    uri = os.getenv("ECW_REDIRECT_URI", "").strip()
    if not uri:
        raise ECWError("ECW_REDIRECT_URI not set")
    return uri


def _default_fhir_base() -> str:
    """Return the configured sandbox FHIR base, fail-closed when unset.

    Unlike Epic (single sandbox URL) or Cerner (tenant-id derivation), eCW
    has a different FHIR base per practice/installation. Phase 12.D ships
    against one configured sandbox; production multi-practice discovery is a
    later phase. We do NOT fall back to a hardcoded URL — a missing env var
    must surface as a clear error rather than silently pointing at the
    wrong tenant.
    """
    base = os.getenv("ECW_SANDBOX_FHIR_BASE", "").strip()
    if not base:
        raise ECWError("ECW_SANDBOX_FHIR_BASE not set")
    return base.rstrip("/")


def _basic_auth_header() -> str:
    raw = f"{_client_id()}:{_client_secret()}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def discover(*, force_refresh: bool = False, base_url_override: str | None = None) -> ECWEndpoints:
    """Fetch + cache ``.well-known/smart-configuration``.

    ``base_url_override`` supports EHR-launch where the FHIR base is supplied
    by the EHR via the ``iss`` query param. Cached 24h in-process; cache key
    is the FHIR base, so per-practice bases will naturally cache separately
    when production multi-tenant support lands.
    """
    base = base_url_override.rstrip("/") if base_url_override else _default_fhir_base()
    if not force_refresh:
        cached = _DISCOVERY_CACHE.get(base)
        if cached and (time.time() - cached[1]) < DISCOVERY_TTL_SECONDS:
            return cached[0]

    url = f"{base}{DISCOVERY_PATH}"
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(http, "GET", url, label="ECW discovery", error_class=ECWError)
    payload = resp.json()
    try:
        endpoints = ECWEndpoints(
            authorize_endpoint=payload["authorization_endpoint"],
            token_endpoint=payload["token_endpoint"],
            fhir_base=base,
        )
    except KeyError as e:
        raise ECWError(f"ECW discovery missing field: {e}") from e

    _DISCOVERY_CACHE[base] = (endpoints, time.time())
    return endpoints


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256.

    Used even though we are a confidential client — eCW supports PKCE
    alongside client_secret and it's a defense-in-depth measure against
    authorization-code interception.
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def make_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorize_url(*, state: str, code_challenge: str, scope: str) -> str:
    """Construct the eCW authorize URL for standalone launch.

    eCW requires the ``aud`` parameter to be the FHIR base URL.
    """
    endpoints = discover()
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": scope,
        "state": state,
        "aud": endpoints.fhir_base,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints.authorize_endpoint}?{urlencode(params)}"


def build_ehr_launch_authorize_url(
    *, state: str, code_challenge: str, scope: str, launch_token: str, iss_override: str
) -> str:
    """Construct the eCW authorize URL for EHR-launch flow."""
    endpoints = discover(base_url_override=iss_override)
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": scope,
        "state": state,
        "aud": endpoints.fhir_base,
        "launch": launch_token,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints.authorize_endpoint}?{urlencode(params)}"


def exchange_code(
    *, code: str, code_verifier: str, iss_override: str | None = None
) -> TokenResponse:
    """POST authorization code → access token using HTTP Basic auth."""
    endpoints = discover(base_url_override=iss_override)
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
            label="ECW token exchange",
            error_class=ECWError,
            max_retries=0,
            data=data,
            headers=headers,
        )
    payload = resp.json()
    if "access_token" not in payload:
        raise ECWError(f"ECW token response missing access_token: {_redact(payload)!r}")
    return TokenResponse(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", ""),
        patient_fhir_id=payload.get("patient"),
        id_token=payload.get("id_token"),
    )


def refresh(refresh_token: str, *, iss_override: str | None = None) -> TokenResponse:
    """Exchange a refresh_token for a new access_token using HTTP Basic auth.

    ``iss_override`` is required for multi-tenant correctness: eCW practices
    each have their own FHIR base, so refresh must hit the same tenant the
    connection was minted against. Defaults to the configured sandbox base.
    """
    endpoints = discover(base_url_override=iss_override)
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
            label="ECW token refresh",
            error_class=ECWError,
            max_retries=0,
            data=data,
            headers=headers,
        )
    payload = resp.json()
    if "access_token" not in payload:
        raise ECWError(f"ECW refresh response missing access_token: {_redact(payload)!r}")
    return TokenResponse(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or refresh_token,
        expires_in=int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", ""),
        patient_fhir_id=payload.get("patient"),
        id_token=payload.get("id_token"),
    )


def fetch_patient(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> dict:
    """GET Patient/{id} from eCW's FHIR R4 endpoint.

    ``iss_override`` is required for multi-tenant correctness — the connection
    stores its tenant FHIR base on ``EHRConnection.iss``.
    """
    endpoints = discover(base_url_override=iss_override)
    url = f"{endpoints.fhir_base.rstrip('/')}/Patient/{patient_fhir_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http, "GET", url, label="ECW Patient.read", error_class=ECWError, headers=headers
        )
    return resp.json()  # type: ignore[no-any-return]


def _fetch_fhir_bundle_entries(
    *,
    access_token: str,
    resource_type: str,
    params: dict[str, str],
    label: str,
    iss_override: str | None = None,
) -> list[dict]:
    endpoints = discover(base_url_override=iss_override)
    query = urlencode(params)
    url = f"{endpoints.fhir_base.rstrip('/')}/{resource_type}?{query}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http, "GET", url, label=label, error_class=ECWError, headers=headers
        )
    bundle = resp.json()
    return [entry["resource"] for entry in bundle.get("entry") or [] if "resource" in entry]


def fetch_conditions(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> list[dict]:
    return _fetch_fhir_bundle_entries(
        access_token=access_token,
        resource_type="Condition",
        params={"patient": patient_fhir_id, "clinical-status": "active"},
        label="ECW Condition.search",
        iss_override=iss_override,
    )


def fetch_medications(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> list[dict]:
    """GET active MedicationRequest resources for a patient.

    eCW uses MedicationRequest (Cerner-style), NOT MedicationStatement
    (Epic-style). Wrong resource → 404.
    """
    return _fetch_fhir_bundle_entries(
        access_token=access_token,
        resource_type="MedicationRequest",
        params={"patient": patient_fhir_id, "status": "active"},
        label="ECW MedicationRequest.search",
        iss_override=iss_override,
    )


def fetch_allergies(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> list[dict]:
    return _fetch_fhir_bundle_entries(
        access_token=access_token,
        resource_type="AllergyIntolerance",
        params={"patient": patient_fhir_id},
        label="ECW AllergyIntolerance.search",
        iss_override=iss_override,
    )


def fetch_document_references(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> list[dict]:
    return _fetch_fhir_bundle_entries(
        access_token=access_token,
        resource_type="DocumentReference",
        params={"patient": patient_fhir_id, "status": "current"},
        label="ECW DocumentReference.search",
        iss_override=iss_override,
    )


def fetch_document_content(url: str, *, access_token: str, fhir_base: str) -> tuple[bytes, str]:
    """Download a DocumentReference attachment from eCW.

    Relative paths are resolved against ``fhir_base``.
    Returns ``(content_bytes, mime_type)``.
    """
    if not url.startswith(("http://", "https://")):
        url = f"{fhir_base.rstrip('/')}/{url.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/pdf, application/fhir+json, */*",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http,
            "GET",
            url,
            label="ECW DocumentReference content",
            error_class=ECWError,
            max_retries=0,
            headers=headers,
        )
    mime = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    return resp.content, mime


def write_service_request(
    *,
    access_token: str,
    patient_fhir_id: str,
    referral_id: int,
    specialty_desc: str | None,
    reason: str | None,
    requesting_provider_name: str | None,
    iss_override: str | None = None,
) -> str:
    """POST a minimal FHIR R4 ServiceRequest to eCW. Returns the resource id."""
    import json as _json

    endpoints = discover(base_url_override=iss_override)
    url = f"{endpoints.fhir_base.rstrip('/')}/ServiceRequest"

    resource: dict = {
        "resourceType": "ServiceRequest",
        "status": "active",
        "intent": "referral",
        "subject": {"reference": f"Patient/{patient_fhir_id}"},
        "identifier": [
            {
                "system": "urn:docstats:referral",
                "value": str(referral_id),
            }
        ],
    }
    if specialty_desc:
        # FHIR R4 ServiceRequest uses `performerType` (single CodeableConcept),
        # not `specialty`.
        resource["performerType"] = {"text": specialty_desc}
    if reason:
        resource["reasonCode"] = [{"text": reason}]
    if requesting_provider_name:
        resource["requester"] = {"display": requesting_provider_name}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json",
        "Content-Type": "application/fhir+json",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http,
            "POST",
            url,
            label="ECW ServiceRequest.write",
            error_class=ECWError,
            max_retries=0,
            content=_json.dumps(resource).encode(),
            headers=headers,
        )
    body = resp.json()
    resource_id: str | None = body.get("id")
    if not resource_id:
        location = resp.headers.get("Location", "")
        resource_id = location.rstrip("/").split("/")[-1] if location else None
    if not resource_id:
        raise ECWError(f"ECW ServiceRequest write returned no id: {_redact(body)!r}")
    return resource_id


_ehr_registry.register("ecw_smart", sys.modules[__name__])
