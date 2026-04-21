"""Shared helpers, constants, and dependencies for route modules."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, Response
from fastapi.templating import Jinja2Templates

from docstats.auth import get_current_user
from docstats.cache import ResponseCache
from docstats.client import NPPESClient
from docstats.scope import Scope
from docstats.storage import get_db_path, get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
MAPBOX_TOKEN = os.environ.get("MAPBOX_PUBLIC_TOKEN", "")

US_STATES = [
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
    ("DC", "District of Columbia"),
]

# --- Dependency injection ---

_client: NPPESClient | None = None


def get_client() -> NPPESClient:
    global _client
    if _client is None:
        db_path = get_db_path()
        cache = ResponseCache(db_path)
        _client = NPPESClient(cache=cache)
    return _client


def render(name: str, context: dict) -> Response:
    """Render a template, compatible with Starlette 0.50+."""
    request = context["request"]
    return templates.TemplateResponse(request, name, context)


def saved_count(storage: StorageBase, user_id: int | None) -> int:
    if user_id is None:
        return 0
    return len(storage.list_providers(user_id))


def assigned_open_count(
    storage: StorageBase, scope: Scope, user_id: int | None, *, cap: int = 200
) -> int:
    """Count of non-terminal referrals assigned to ``user_id`` in ``scope``.

    Powers the Referrals nav badge (Phase 7.C). Anonymous scope / missing
    user_id returns 0. Fetches up to ``cap`` rows and filters in Python;
    the badge shows "200+" when the real count exceeds 200, so the helper
    protects pages from pathological list sizes without a dedicated
    count-only storage method.
    """
    if user_id is None or scope.is_anonymous:
        return 0
    # Import locally to keep _common.py free of domain cycles.
    from docstats.domain.referrals import TERMINAL_STATUSES

    referrals = storage.list_referrals(scope, assigned_to_user_id=user_id, limit=cap)
    return sum(1 for r in referrals if r.status not in TERMINAL_STATUSES)


def get_scope(
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
) -> Scope:
    """Return the active Scope for the request.

    - Anonymous request → ``Scope()`` (all fields None).
    - Logged-in user with no ``active_org_id`` → solo mode, ``user_id`` set.
    - Logged-in user with ``active_org_id`` → re-verified against
      :meth:`StorageBase.get_membership`. If the membership is missing or
      soft-deleted (e.g. the user was removed from the org but their session
      cookie still claims it), we silently fall back to solo mode. Routes that
      require org context should assert ``scope.is_org`` themselves.

    The membership lookup is one extra DB read per authenticated request. Fine
    for Phase 0; a cached version can land in Phase 7 if hot paths need it.
    """
    if not current_user:
        return Scope()

    user_id = current_user["id"]
    active_org_id = current_user.get("active_org_id")
    if active_org_id is None:
        return Scope(user_id=user_id)

    membership = storage.get_membership(active_org_id, user_id)
    if membership is None:
        # Stale active_org_id. Clear it lazily so the next request is fast.
        logger.info(
            "Clearing stale active_org_id=%s for user_id=%s (no active membership)",
            active_org_id,
            user_id,
        )
        try:
            storage.set_active_org(user_id, None)
        except Exception:
            logger.exception("Failed to clear stale active_org_id for user_id=%s", user_id)
        return Scope(user_id=user_id)

    return Scope(
        user_id=user_id,
        organization_id=active_org_id,
        membership_role=membership.role,
    )
