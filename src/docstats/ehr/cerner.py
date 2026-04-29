"""Cerner/Oracle Health SMART-on-FHIR sandbox client (Phase 12.C).

Synchronous httpx wrapped in `request_with_retry`. Route layer wraps calls
in an executor when invoked from async handlers.

Public client: registered as Application Privacy = Public in the Cerner
console. The token endpoint is authenticated via PKCE only (no
client_secret, no HTTP Basic). ``client_id`` is sent in the form body per
RFC 6749 §2.3.1.

Key differences from Epic:
- FHIR base derived from ``CERNER_SANDBOX_TENANT_ID`` env var.
- ``aud`` parameter is NOT included in the authorize URL (Cerner doesn't require it).
- Clinical medications use ``MedicationRequest`` (not ``MedicationStatement``).
- Token URLs are always discovered via ``.well-known/smart-configuration``.
- Public/PKCE-only (Epic uses confidential + Basic auth).
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


class CernerError(EHRError):
    """Cerner/Oracle Health SMART-on-FHIR call failed."""


DISCOVERY_PATH = "/.well-known/smart-configuration"
DISCOVERY_TTL_SECONDS = 24 * 3600

_CERNER_FHIR_HOST = "https://fhir-myrecord.cerner.com/r4"


@dataclass(frozen=True)
class CernerEndpoints:
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


_DISCOVERY_CACHE: dict[str, tuple[CernerEndpoints, float]] = {}


def _tenant_id() -> str:
    tid = os.getenv("CERNER_SANDBOX_TENANT_ID", "").strip()
    if not tid:
        raise CernerError("CERNER_SANDBOX_TENANT_ID not set")
    return tid


def _client_id() -> str:
    cid = os.getenv("CERNER_CLIENT_ID", "").strip()
    if not cid:
        raise CernerError("CERNER_CLIENT_ID not set")
    return cid


def _redirect_uri() -> str:
    uri = os.getenv("CERNER_REDIRECT_URI", "").strip()
    if not uri:
        raise CernerError("CERNER_REDIRECT_URI not set")
    return uri


def _default_fhir_base() -> str:
    return f"{_CERNER_FHIR_HOST}/{_tenant_id()}"


def discover(
    *, force_refresh: bool = False, base_url_override: str | None = None
) -> CernerEndpoints:
    """Fetch + cache ``.well-known/smart-configuration``.

    ``base_url_override`` supports EHR-launch where the FHIR base is
    supplied by the EHR via the ``iss`` query param. Cached 24h in-process.
    """
    base = base_url_override.rstrip("/") if base_url_override else _default_fhir_base()
    if not force_refresh:
        cached = _DISCOVERY_CACHE.get(base)
        if cached and (time.time() - cached[1]) < DISCOVERY_TTL_SECONDS:
            return cached[0]

    url = f"{base}{DISCOVERY_PATH}"
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http, "GET", url, label="Cerner discovery", error_class=CernerError
        )
    payload = resp.json()
    try:
        endpoints = CernerEndpoints(
            authorize_endpoint=payload["authorization_endpoint"],
            token_endpoint=payload["token_endpoint"],
            fhir_base=base,
        )
    except KeyError as e:
        raise CernerError(f"Cerner discovery missing field: {e}") from e

    _DISCOVERY_CACHE[base] = (endpoints, time.time())
    return endpoints


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def make_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorize_url(*, state: str, code_challenge: str, scope: str) -> str:
    """Construct the Cerner authorize URL for standalone launch.

    Cerner does not require an ``aud`` parameter in the authorize request.
    """
    endpoints = discover()
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints.authorize_endpoint}?{urlencode(params)}"


def build_ehr_launch_authorize_url(
    *, state: str, code_challenge: str, scope: str, launch_token: str, iss_override: str
) -> str:
    """Construct the Cerner authorize URL for EHR-launch (sidebar) flow."""
    endpoints = discover(base_url_override=iss_override)
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": scope,
        "state": state,
        "launch": launch_token,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints.authorize_endpoint}?{urlencode(params)}"


def exchange_code(
    *, code: str, code_verifier: str, iss_override: str | None = None
) -> TokenResponse:
    """POST authorization code → access token."""
    endpoints = discover(base_url_override=iss_override)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": code_verifier,
        "client_id": _client_id(),
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http,
            "POST",
            endpoints.token_endpoint,
            label="Cerner token exchange",
            error_class=CernerError,
            max_retries=0,
            data=data,
            headers=headers,
        )
    payload = resp.json()
    if "access_token" not in payload:
        raise CernerError(f"Cerner token response missing access_token: {_redact(payload)!r}")
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
        "client_id": _client_id(),
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http,
            "POST",
            endpoints.token_endpoint,
            label="Cerner token refresh",
            error_class=CernerError,
            max_retries=0,
            data=data,
            headers=headers,
        )
    payload = resp.json()
    if "access_token" not in payload:
        raise CernerError(f"Cerner refresh response missing access_token: {_redact(payload)!r}")
    return TokenResponse(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or refresh_token,
        expires_in=int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", ""),
        patient_fhir_id=payload.get("patient"),
        id_token=payload.get("id_token"),
    )


def fetch_patient(*, access_token: str, patient_fhir_id: str) -> dict:
    """GET Patient/{id} from Cerner's FHIR R4 endpoint."""
    endpoints = discover()
    url = f"{endpoints.fhir_base.rstrip('/')}/Patient/{patient_fhir_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http, "GET", url, label="Cerner Patient.read", error_class=CernerError, headers=headers
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
            http, "GET", url, label=label, error_class=CernerError, headers=headers
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
        label="Cerner Condition.search",
        iss_override=iss_override,
    )


def fetch_medications(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> list[dict]:
    """GET active MedicationRequest resources for a patient.

    Cerner uses MedicationRequest (not MedicationStatement like Epic).
    """
    return _fetch_fhir_bundle_entries(
        access_token=access_token,
        resource_type="MedicationRequest",
        params={"patient": patient_fhir_id, "status": "active"},
        label="Cerner MedicationRequest.search",
        iss_override=iss_override,
    )


def fetch_allergies(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> list[dict]:
    return _fetch_fhir_bundle_entries(
        access_token=access_token,
        resource_type="AllergyIntolerance",
        params={"patient": patient_fhir_id},
        label="Cerner AllergyIntolerance.search",
        iss_override=iss_override,
    )


def fetch_document_references(
    *, access_token: str, patient_fhir_id: str, iss_override: str | None = None
) -> list[dict]:
    return _fetch_fhir_bundle_entries(
        access_token=access_token,
        resource_type="DocumentReference",
        params={"patient": patient_fhir_id, "status": "current"},
        label="Cerner DocumentReference.search",
        iss_override=iss_override,
    )


def fetch_document_content(url: str, *, access_token: str, fhir_base: str) -> tuple[bytes, str]:
    """Download a DocumentReference attachment from Cerner.

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
            label="Cerner DocumentReference content",
            error_class=CernerError,
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
    """POST a minimal FHIR R4 ServiceRequest to Cerner. Returns the resource id."""
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
        resource["specialty"] = [{"text": specialty_desc}]
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
            label="Cerner ServiceRequest.write",
            error_class=CernerError,
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
        raise CernerError(f"Cerner ServiceRequest write returned no id: {_redact(body)!r}")
    return resource_id


_ehr_registry.register("cerner_oauth", sys.modules[__name__])
