"""Admin console — Phase 6.

Role-gated org administration. Every route here requires:

- An authenticated user (``require_user`` via :func:`require_admin_scope`).
- An active org membership (``scope.is_org`` True).
- A membership role at or above ``admin`` (``has_role_at_least(role, "admin")``).

Solo users and sub-admin org members get a 403. The route body never executes
for them — the dependency raises before the handler runs.

This file ships Phase 6.A (foundation + ``GET /admin`` overview). Subsequent
slices land the other admin surfaces (specialty rules, payer rules, org
settings, audit viewer, members) as additional routes on the same router.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from docstats.auth import require_user
from docstats.domain.orgs import Organization, has_role_at_least
from docstats.routes._common import get_scope, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin_scope(
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(get_scope),
) -> Scope:
    """FastAPI dependency: require an org admin (or owner) for the active org.

    Raises ``HTTPException(403)`` for:

    - Authenticated solo users (no active org)
    - Org members whose role is below ``admin`` in the ROLES ladder

    Returns the resolved :class:`Scope` (guaranteed ``is_org`` and
    ``membership_role`` set) for downstream handlers.
    """
    if not scope.is_org:
        raise HTTPException(
            status_code=403,
            detail="Admin console requires an active organization. Switch orgs or contact your owner.",
        )
    if not has_role_at_least(scope.membership_role, "admin"):
        raise HTTPException(
            status_code=403,
            detail="Admin role required.",
        )
    return scope


def _require_org(scope: Scope, storage: StorageBase) -> Organization:
    """Load the active org row; 404 if it vanished (should be impossible given
    ``require_admin_scope`` already verified membership, but defensive).
    """
    assert scope.organization_id is not None  # require_admin_scope guarantee
    org = storage.get_organization(scope.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return org


def _ctx(
    request: Request,
    user: dict,
    storage: StorageBase,
    scope: Scope,
    org: Organization,
    *,
    active_section: str,
    **extra: object,
) -> dict:
    """Common template context for admin pages.

    ``active_section`` drives the sidebar highlighting. Values align with the
    sub-phases: ``overview``, ``members``, ``specialty-rules``, ``payer-rules``,
    ``org-settings``, ``audit``.
    """
    return {
        "request": request,
        "active_page": "admin",
        "active_section": active_section,
        "user": user,
        "saved_count": saved_count(storage, user["id"]),
        "scope": scope,
        "org": org,
        **extra,
    }


@router.get("", response_class=HTMLResponse)
async def admin_overview(
    request: Request,
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Admin overview: org snapshot + counts + recent audit events."""
    org = _require_org(scope, storage)

    memberships = storage.list_memberships_for_org(scope.organization_id)  # type: ignore[arg-type]
    active_members = [m for m in memberships if m.is_active]

    # Specialty rule counts: globals (organization_id IS NULL) + this org's
    # overrides. ``include_globals=True`` returns BOTH when organization_id is
    # passed; ``include_globals=False`` narrows to org-only.
    org_specialty_overrides = storage.list_specialty_rules(
        organization_id=scope.organization_id,
        include_globals=False,
    )
    global_specialty_rules = storage.list_specialty_rules(
        organization_id=None,
        include_globals=True,
    )
    # Globals are rows with organization_id IS NULL. When organization_id=None
    # and include_globals=True, the list is just globals (no overrides exist
    # without an org filter), so the len is safe.

    org_payer_overrides = storage.list_payer_rules(
        organization_id=scope.organization_id,
        include_globals=False,
    )
    global_payer_rules = storage.list_payer_rules(
        organization_id=None,
        include_globals=True,
    )

    recent_events = storage.list_audit_events(
        scope_organization_id=scope.organization_id,
        limit=10,
    )

    return render(
        "admin/overview.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            active_section="overview",
            member_count=len(active_members),
            specialty_global_count=len(global_specialty_rules),
            specialty_override_count=len(org_specialty_overrides),
            payer_global_count=len(global_payer_rules),
            payer_override_count=len(org_payer_overrides),
            recent_events=recent_events,
        ),
    )
