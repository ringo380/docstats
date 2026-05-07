"""Redox aggregator routes (Phase 12.E) — org-scoped admin-only.

Distinct from ``routes/ehr.py`` because Redox is fundamentally different from
SMART-on-FHIR vendors:

- No per-user OAuth dance (no ``authorize`` redirect, no PKCE, no callback)
- Connection is owned by an organization, not a user
- Auth is JWT-bearer assertion (RFC 7523) signed with an RSA keypair
- Tokens are NOT persisted — re-minted in-process via short-lived cache

Three handlers cover the lifecycle:

- ``GET  /ehr/redox/connect``    — admin form for destination path
- ``POST /ehr/redox/connect``    — validates by minting a token + persists row
- ``POST /ehr/redox/disconnect`` — admin revokes the org connection

Feature flag: ``EHR_REDOX_ENABLED=1`` gates every handler; off → 404.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from docstats.auth import require_user
from docstats.domain import audit
from docstats.domain.ehr import REDOX_SCOPES
from docstats.ehr import redox  # noqa: F401 — side-effect: registers vendor
from docstats.ehr.registry import EHRError
from docstats.routes._common import render
from docstats.routes.admin import require_admin_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ehr/redox", tags=["ehr", "redox"])

_VENDOR_KEY = "redox"
_DEFAULT_DESTINATION = "redox-fhir-sandbox/Development"


def _flag_enabled() -> bool:
    return os.environ.get("EHR_REDOX_ENABLED", "").strip() == "1"


def _require_enabled() -> None:
    if not _flag_enabled():
        raise HTTPException(status_code=404)


@router.get("/connect", response_class=HTMLResponse)
def redox_connect_form(
    request: Request,
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Render the admin connect form."""
    _require_enabled()
    assert scope.organization_id is not None  # require_admin_scope guarantees
    existing = storage.get_active_org_ehr_connection(scope.organization_id, _VENDOR_KEY)
    return render(
        "ehr_redox_connect.html",
        {
            "request": request,
            "active_connection": existing,
            "default_destination": _DEFAULT_DESTINATION,
            "scopes": REDOX_SCOPES,
        },
    )


@router.post("/connect")
def redox_connect_submit(
    request: Request,
    destination_path: str = Form(..., max_length=255),
    scope: Scope = Depends(require_admin_scope),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Validate creds + persist an org-scoped Redox connection.

    Validation strategy: try to mint a token via the vendor module. If the
    token endpoint returns 200, creds are valid; we don't need to make a FHIR
    call at this point (destination path is just stored — its first FHIR
    request will validate it during patient lookup).
    """
    _require_enabled()
    assert scope.organization_id is not None
    org_id = scope.organization_id

    cleaned_dest = destination_path.strip().strip("/")
    if not cleaned_dest:
        return _redirect_with_error("missing_destination")

    # Validate auth by minting a token. Fails closed if env config is missing
    # or the dashboard hasn't activated the keypair.
    try:
        redox.request_access_token(scope=REDOX_SCOPES, force_refresh=True)
    except redox.RedoxConfigError as exc:
        logger.warning("redox connect config error: %s", exc)
        return _redirect_with_error("server_config")
    except EHRError as exc:
        logger.warning("redox connect token mint failed: %s", exc)
        return _redirect_with_error("token_exchange")

    # Persist (revokes any prior active row for this org+vendor in the same tx).
    storage.create_org_ehr_connection(
        organization_id=org_id,
        ehr_vendor=_VENDOR_KEY,
        iss=cleaned_dest,
        scope=REDOX_SCOPES,
    )

    audit.record(
        storage,
        action="ehr.connected",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=org_id,
        metadata={"ehr_vendor": _VENDOR_KEY, "iss": cleaned_dest},
    )

    return RedirectResponse("/admin/org?ehr_connected=redox", status_code=303)


@router.post("/disconnect")
def redox_disconnect(
    request: Request,
    scope: Scope = Depends(require_admin_scope),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    """Revoke the active org Redox connection (idempotent)."""
    _require_enabled()
    assert scope.organization_id is not None
    org_id = scope.organization_id

    revoked = storage.revoke_org_ehr_connection(org_id, _VENDOR_KEY)
    if revoked:
        audit.record(
            storage,
            action="ehr.disconnected",
            request=request,
            actor_user_id=current_user["id"],
            scope_organization_id=org_id,
            metadata={"ehr_vendor": _VENDOR_KEY, "revoked_count": revoked},
        )
    return RedirectResponse("/admin/org?ehr_disconnected=redox", status_code=303)


def _redirect_with_error(reason: str) -> RedirectResponse:
    return RedirectResponse(f"/ehr/redox/connect?error={reason}", status_code=303)
