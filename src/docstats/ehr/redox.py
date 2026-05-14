"""Redox aggregator client (Phase 12.E).

Redox is a healthcare integration aggregator that exposes ~200 EHRs via a
single FHIR R4 API. Unlike Epic/Cerner/eCW (per-user SMART-on-FHIR), Redox is
a backend-to-backend integration: one OAuth API key per Redox organization,
shared across all users of a docstats org.

Authentication: JWT-bearer assertion grant (RFC 7523). For each token request:

1. Build a JWT with header ``{alg: RS384, kid: <REDOX_KEY_ID>, typ: JWT}``
   and claims ``{iss: client_id, sub: client_id, aud: token_url,
   exp: now+5m, iat: now, jti: uuid}``.
2. Sign with the RSA private key.
3. POST to the token endpoint with form params
   ``grant_type=client_credentials``,
   ``client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer``,
   ``client_assertion=<signed-JWT>``,
   ``scope=<scopes>``.
4. Receive a Bearer access token (typically ~5min lifetime). NO refresh token.

Tokens are NOT persisted to ``ehr_connections``: we re-mint on demand via a
short in-process cache. The connection row exists purely to record which org
has Redox enabled.

Configuration (env vars, read at call time):
- ``REDOX_CLIENT_ID``                 — OAuth client UUID from dashboard
- ``REDOX_KEY_ID``                    — JWT ``kid`` for the keypair
- ``REDOX_PRIVATE_KEY_PEM`` (preferred for prod) OR ``REDOX_PRIVATE_KEY_PATH``
  (preferred for dev; path to PEM file with 0600 perms)
- ``REDOX_TOKEN_URL``  (default ``https://api.redoxengine.com/v2/auth/token``)
- ``REDOX_FHIR_BASE``  (default ``https://api.redoxengine.com/fhir/R4``)
- ``REDOX_FHIR_DESTINATION`` (default ``redox-fhir-sandbox/Development``) — the
  per-org/per-environment path component appended to the FHIR base. Production
  values are unique per Redox-customer org (e.g. ``acme-clinic/Production``).
  In docstats this is normally stored on the connection row's ``iss`` column,
  not in env; the env var is the dev/test fallback.
"""

from __future__ import annotations

import json as _json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlencode

import httpx
import jwt as _jwt

from docstats.ehr import registry as _ehr_registry
from docstats.ehr.registry import EHRError
from docstats.http_retry import (
    get_default_timeout,
    request_with_retry,
)

logger = logging.getLogger(__name__)


_TOKEN_FIELDS: frozenset[str] = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "client_assertion",
        "private_key",
        "private_key_pem",
    }
)


def _redact(payload: object) -> object:
    if isinstance(payload, dict):
        return {
            k: ("***" if k.lower() in _TOKEN_FIELDS else _redact(v)) for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


class RedoxError(EHRError):
    """Redox API call or auth flow failed."""


class RedoxConfigError(RedoxError):
    """Missing/invalid Redox env config."""


_DEFAULT_TOKEN_URL = "https://api.redoxengine.com/v2/auth/token"
_DEFAULT_FHIR_BASE = "https://api.redoxengine.com/fhir/R4"
_DEFAULT_FHIR_DESTINATION = "redox-fhir-sandbox/Development"
_JWT_ASSERTION_TTL_SECONDS = 300  # 5 min — Redox max
_JWT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _client_id() -> str:
    val = os.environ.get("REDOX_CLIENT_ID", "").strip()
    if not val:
        raise RedoxConfigError("REDOX_CLIENT_ID is not set")
    return val


def _key_id() -> str:
    val = os.environ.get("REDOX_KEY_ID", "").strip()
    if not val:
        raise RedoxConfigError("REDOX_KEY_ID is not set")
    return val


def _token_url() -> str:
    return os.environ.get("REDOX_TOKEN_URL", _DEFAULT_TOKEN_URL).strip() or _DEFAULT_TOKEN_URL


def _fhir_base() -> str:
    return os.environ.get("REDOX_FHIR_BASE", _DEFAULT_FHIR_BASE).strip() or _DEFAULT_FHIR_BASE


def _resolve_destination(destination_path: str | None) -> str:
    """Pick the destination path component, preferring caller arg over env default.

    Redox FHIR URLs look like
    ``{base}/{org-name}/{Environment}/{Resource}`` where ``{org-name}/{Environment}``
    is the destination path. ``redox-fhir-sandbox/Development`` is Redox's
    built-in test sandbox. Production destinations are unique per Redox org
    and must be stored on the connection row.
    """
    if destination_path:
        return destination_path.strip("/")
    env_val = os.environ.get("REDOX_FHIR_DESTINATION", "").strip()
    if env_val:
        return env_val.strip("/")
    return _DEFAULT_FHIR_DESTINATION


def _load_private_key() -> str:
    """Return the PEM-encoded RSA private key from env / file.

    Preference order:
    1. ``REDOX_PRIVATE_KEY_PEM``  (full PEM as env var, preferred for prod)
    2. ``REDOX_PRIVATE_KEY_PATH`` (path to PEM file, preferred for dev)

    Raises ``RedoxConfigError`` if neither is set or the file is unreadable.
    """
    pem_inline = os.environ.get("REDOX_PRIVATE_KEY_PEM", "").strip()
    if pem_inline:
        return pem_inline
    path = os.environ.get("REDOX_PRIVATE_KEY_PATH", "").strip()
    if not path:
        raise RedoxConfigError("Neither REDOX_PRIVATE_KEY_PEM nor REDOX_PRIVATE_KEY_PATH is set")
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise RedoxConfigError(f"Failed to read REDOX_PRIVATE_KEY_PATH: {exc}") from exc


# ---- JWT assertion + token mint -----------------------------------------


def build_client_assertion(*, now: float | None = None) -> str:
    """Build a signed RS384 JWT for the OAuth client_credentials grant.

    Uses ``REDOX_CLIENT_ID``, ``REDOX_KEY_ID``, and the configured private key.
    ``now`` is for test injection; production passes None.
    """
    iat = int(now if now is not None else time.time())
    exp = iat + _JWT_ASSERTION_TTL_SECONDS
    cid = _client_id()
    kid = _key_id()
    private_key = _load_private_key()

    claims = {
        "iss": cid,
        "sub": cid,
        "aud": _token_url(),
        "exp": exp,
        "iat": iat,
        "jti": uuid.uuid4().hex,
    }
    headers = {"alg": "RS384", "kid": kid, "typ": "JWT"}
    return _jwt.encode(claims, private_key, algorithm="RS384", headers=headers)


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    expires_at: float  # Unix seconds


_token_cache: dict[str, _CachedToken] = {}
_token_cache_lock = Lock()


def _cache_key(client_id: str, scope: str) -> str:
    return f"{client_id}|{scope}"


def reset_token_cache() -> None:
    """Test hook: clear the in-process token cache."""
    with _token_cache_lock:
        _token_cache.clear()


def request_access_token(*, scope: str, force_refresh: bool = False) -> str:
    """Return a Bearer access token from Redox, using the in-process cache.

    Tokens are minted via JWT-bearer assertion. The cache key is
    ``(client_id, scope)`` so two callers requesting different scopes get
    independent tokens. Set ``force_refresh=True`` to bypass the cache (used
    after a 401 from a downstream call).
    """
    cid = _client_id()
    key = _cache_key(cid, scope)
    now = time.time()
    if not force_refresh:
        with _token_cache_lock:
            cached = _token_cache.get(key)
            if cached is not None and cached.expires_at - now > 30:
                return cached.access_token

    assertion = build_client_assertion()
    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_assertion_type": _JWT_ASSERTION_TYPE,
            "client_assertion": assertion,
            "scope": scope,
        }
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http,
            "POST",
            _token_url(),
            label="Redox token mint",
            error_class=RedoxError,
            content=body,
            headers=headers,
        )
    payload = resp.json()
    access_token_raw = payload.get("access_token")
    if not isinstance(access_token_raw, str) or not access_token_raw:
        raise RedoxError(f"Redox token response missing access_token: {_redact(payload)!r}")
    access_token: str = access_token_raw
    expires_in = int(payload.get("expires_in", 300))
    with _token_cache_lock:
        _token_cache[key] = _CachedToken(
            access_token=access_token,
            expires_at=now + expires_in,
        )
    return access_token


# ---- FHIR helpers --------------------------------------------------------


def _fhir_get(
    *,
    path: str,
    access_token: str,
    destination_path: str | None = None,
    accept: str = "application/fhir+json",
) -> dict:
    base = _fhir_base().rstrip("/")
    dest = _resolve_destination(destination_path)
    url = f"{base}/{dest}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": accept,
    }
    with httpx.Client(timeout=get_default_timeout()) as http:
        resp = request_with_retry(
            http, "GET", url, label=f"Redox GET {path}", error_class=RedoxError, headers=headers
        )
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RedoxError(f"Redox {path} returned non-object payload: {type(payload).__name__}")
    return payload


def _bundle_entries(bundle: dict) -> list[dict]:
    return [entry["resource"] for entry in bundle.get("entry") or [] if "resource" in entry]


def find_patient_by_mrn(
    *,
    access_token: str,
    mrn: str,
    mrn_system: str | None = None,
    destination_path: str | None = None,
) -> str | None:
    """Search Redox for a Patient by MRN identifier. Returns FHIR Patient.id or None.

    ``mrn_system`` is the OID/URI namespace of the MRN; if omitted, Redox
    searches across all identifier systems for the value alone (per FHIR R4
    Patient?identifier=<value> semantics).

    Raises ``RedoxError`` if multiple matches return — caller must disambiguate
    upstream (different MRN namespace, etc.).
    """
    if mrn_system:
        identifier = f"{mrn_system}|{mrn}"
    else:
        identifier = mrn
    params = {"identifier": identifier}
    bundle = _fhir_get(
        path=f"Patient?{urlencode(params)}",
        access_token=access_token,
        destination_path=destination_path,
    )
    entries = _bundle_entries(bundle)
    if not entries:
        return None
    if len(entries) > 1:
        raise RedoxError(
            f"Redox Patient?identifier returned {len(entries)} matches for MRN; "
            "supply mrn_system to disambiguate"
        )
    pid = entries[0].get("id")
    if not isinstance(pid, str) or not pid:
        raise RedoxError("Redox Patient match has no id field")
    return pid


def fetch_patient(
    *, access_token: str, patient_fhir_id: str, destination_path: str | None = None
) -> dict:
    """GET a single FHIR R4 Patient resource by id."""
    return _fhir_get(
        path=f"Patient/{patient_fhir_id}",
        access_token=access_token,
        destination_path=destination_path,
    )


def _fetch_search_bundle(
    *,
    access_token: str,
    resource_type: str,
    params: dict[str, str],
    destination_path: str | None = None,
) -> list[dict]:
    bundle = _fhir_get(
        path=f"{resource_type}?{urlencode(params)}",
        access_token=access_token,
        destination_path=destination_path,
    )
    return _bundle_entries(bundle)


def fetch_conditions(
    *, access_token: str, patient_fhir_id: str, destination_path: str | None = None
) -> list[dict]:
    return _fetch_search_bundle(
        access_token=access_token,
        resource_type="Condition",
        params={"patient": patient_fhir_id, "clinical-status": "active"},
        destination_path=destination_path,
    )


def fetch_medications(
    *, access_token: str, patient_fhir_id: str, destination_path: str | None = None
) -> list[dict]:
    """GET active MedicationRequest resources (Redox uses the request resource)."""
    return _fetch_search_bundle(
        access_token=access_token,
        resource_type="MedicationRequest",
        params={"patient": patient_fhir_id, "status": "active"},
        destination_path=destination_path,
    )


def fetch_allergies(
    *, access_token: str, patient_fhir_id: str, destination_path: str | None = None
) -> list[dict]:
    return _fetch_search_bundle(
        access_token=access_token,
        resource_type="AllergyIntolerance",
        params={"patient": patient_fhir_id},
        destination_path=destination_path,
    )


def fetch_document_references(
    *, access_token: str, patient_fhir_id: str, destination_path: str | None = None
) -> list[dict]:
    return _fetch_search_bundle(
        access_token=access_token,
        resource_type="DocumentReference",
        params={"patient": patient_fhir_id, "status": "current"},
        destination_path=destination_path,
    )


# ---- Write-back ---------------------------------------------------------


def write_service_request(
    *,
    access_token: str,
    patient_fhir_id: str,
    referral_id: int,
    specialty_desc: str | None,
    reason: str | None,
    requesting_provider_name: str | None,
    destination_path: str | None = None,
) -> str:
    """POST a minimal FHIR R4 ServiceRequest to Redox. Returns the resource id."""
    base = _fhir_base().rstrip("/")
    dest = _resolve_destination(destination_path)
    url = f"{base}/{dest}/ServiceRequest"

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
        # FHIR R4 ServiceRequest uses ``performerType`` (single CodeableConcept),
        # not ``specialty``.
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
            label="Redox ServiceRequest.write",
            error_class=RedoxError,
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
        raise RedoxError(f"Redox ServiceRequest write returned no id: {_redact(body)!r}")
    return resource_id


def read_service_request(
    *,
    access_token: str,
    service_request_id: str,
    destination_path: str | None = None,
):  # -> ServiceRequestSnapshot
    """Issue #157: GET ServiceRequest/{id} via Redox.

    Redox is org-scoped JWT-bearer so the multi-tenant routing argument is
    ``destination_path`` (the ``{org}/{Environment}`` segment), not
    ``iss_override`` like the SMART vendors. The poller maps this from the
    connection's ``iss`` field.
    """
    from docstats.ehr import parse_service_request_payload

    body = _fhir_get(
        path=f"ServiceRequest/{service_request_id}",
        access_token=access_token,
        destination_path=destination_path,
    )
    return parse_service_request_payload(body)


_ehr_registry.register("redox", sys.modules[__name__])
