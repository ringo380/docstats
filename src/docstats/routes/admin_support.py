"""Admin support console — staff access to user accounts (grant-gated).

Staff can only view a user's account data when the user has created an active
staff_access_grant.  Every access is audited.  No PHI (patients/referrals) is
shown — only account fields and the user's own audit history.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from docstats.domain import audit
from docstats.routes._common import render
from docstats.routes.admin import _require_org, require_admin_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.storage_base import normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/support", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def admin_support(
    request: Request,
    email: str | None = Query(default=None, max_length=254),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Search form — enter a user's email to look up their support access status."""
    target_user = None
    active_grant = None
    not_found = False

    if email:
        normalized = normalize_email(email)
        target_user = storage.get_user_by_email(normalized)
        if target_user is None:
            not_found = True
        else:
            active_grant = storage.get_active_staff_access_grant(target_user["id"])

    org = _require_org(scope, storage)
    return render(
        "admin/support.html",
        {
            "request": request,
            "active_section": "support",
            "org": org,
            "email_query": email or "",
            "target_user": target_user,
            "active_grant": active_grant,
            "not_found": not_found,
            "audit_events": [],
        },
    )


@router.get("/user/{user_id}", response_class=HTMLResponse)
async def admin_support_user(
    request: Request,
    user_id: int,
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """View a user's account — requires an active staff access grant from the user."""
    active_grant = storage.get_active_staff_access_grant(user_id)
    target_user = storage.get_user_by_id(user_id)

    org = _require_org(scope, storage)
    if not active_grant or not active_grant.is_active():
        return render(
            "admin/support.html",
            {
                "request": request,
                "active_section": "support",
                "org": org,
                "email_query": "",
                "target_user": target_user,
                "active_grant": None,
                "not_found": False,
                "audit_events": [],
                "access_denied": True,
            },
        )

    # Grant is active — load audit events and record the access.
    by_actor = storage.list_audit_events(actor_user_id=user_id, limit=50)
    by_scope = storage.list_audit_events(scope_user_id=user_id, limit=50)
    seen: set[int] = set()
    merged: list = []
    for ev in by_actor + by_scope:
        if ev.id not in seen:
            seen.add(ev.id)
            merged.append(ev)
    merged.sort(key=lambda e: e.created_at, reverse=True)
    audit_events = merged[:50]

    audit.record(
        storage,
        action="staff_access.accessed",
        request=request,
        actor_user_id=scope.user_id,
        scope_user_id=user_id,
        metadata={"grant_id": active_grant.id, "expires_at": active_grant.expires_at.isoformat()},
    )

    return render(
        "admin/support.html",
        {
            "request": request,
            "active_section": "support",
            "org": org,
            "email_query": "",
            "target_user": target_user,
            "active_grant": active_grant,
            "not_found": False,
            "audit_events": audit_events,
            "access_denied": False,
        },
    )
