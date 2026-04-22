"""Shared helpers, constants, and dependencies for route modules."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import cast

from fastapi import Depends, Request, Response
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


def _storage_for_request(request: Request) -> StorageBase:
    override = getattr(request.app, "dependency_overrides", {}).get(get_storage)
    if override is not None:
        return cast(StorageBase, override())
    return get_storage()


def _nav_scope_for_user(storage: StorageBase, user: dict) -> Scope:
    user_id = user["id"]
    active_org_id = user.get("active_org_id")
    if active_org_id is not None:
        try:
            membership = storage.get_membership(active_org_id, user_id)
        except Exception:
            logger.exception("Nav membership lookup failed user_id=%s", user_id)
        else:
            if membership is not None and membership.is_active:
                return Scope(
                    user_id=user_id,
                    organization_id=active_org_id,
                    membership_role=membership.role,
                )
    return Scope(user_id=user_id)


def _inject_nav_context(context: dict) -> None:
    user = context.get("user")
    if not user:
        context.setdefault("saved_count", 0)
        context.setdefault("assigned_open_count", 0)
        return

    needs_saved = "saved_count" not in context
    needs_assigned = "assigned_open_count" not in context
    if not needs_saved and not needs_assigned:
        return

    try:
        storage = context.get("storage") or _storage_for_request(context["request"])
    except Exception:
        logger.exception("Failed to load storage for shared nav context")
        context.setdefault("saved_count", 0)
        context.setdefault("assigned_open_count", 0)
        return

    user_id = user.get("id")
    if needs_saved:
        context["saved_count"] = saved_count(storage, user_id)
    if needs_assigned:
        scope = context.get("scope") or _nav_scope_for_user(storage, user)
        context["assigned_open_count"] = assigned_open_count(storage, scope, user_id)


def redirect_htmx(request: Request, dest: str) -> Response:
    """Return an ``HX-Redirect`` (200) for htmx callers, else a 303 redirect.

    htmx doesn't follow 3xx redirects, so every mutating handler's exit
    path should use this helper rather than inlining the conditional.
    """
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


def render(name: str, context: dict) -> Response:
    """Render a template, compatible with Starlette 0.50+."""
    _inject_nav_context(context)
    request = context["request"]
    return templates.TemplateResponse(request, name, context)


def format_actor(user_row: dict | None) -> str:
    """Return a display name for a user dict from storage.

    Prefer ``first_name last_name`` when both are set; fall back to
    ``display_name``; then the bare email. Returns ``"—"`` when the row
    is None (actor hard-deleted — audit FK is SET NULL on user delete).

    Mirrors the nav-bar display-name formula from base.html.
    """
    if user_row is None:
        return "—"
    first = (user_row.get("first_name") or "").strip()
    last = (user_row.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last}"
    display = (user_row.get("display_name") or "").strip()
    if display:
        return display
    email = (user_row.get("email") or "").strip()
    return email or "—"


def build_actor_map(storage: StorageBase, events: list) -> dict[int, str]:
    """Fetch display names for every distinct ``actor_user_id`` on the event list.

    Per-request cache: ~50 events × small actor cardinality means this is at
    most a handful of ``get_user_by_id`` calls.
    """
    ids: set[int] = {e.actor_user_id for e in events if e.actor_user_id is not None}
    return {uid: format_actor(storage.get_user_by_id(uid)) for uid in ids}


def saved_count(storage: StorageBase, user_id: int | None) -> int:
    if user_id is None:
        return 0
    return storage.count_providers(user_id)


def assigned_open_count(storage: StorageBase, scope: Scope, user_id: int | None) -> int:
    """Count of non-terminal referrals assigned to ``user_id`` in ``scope``.

    Powers the Referrals nav badge (Phase 7.C). Anonymous scope / missing
    user_id returns 0. Uses a storage count query filtered to non-terminal
    statuses before counting, so recently completed referrals cannot hide
    older open work.
    """
    if user_id is None or scope.is_anonymous:
        return 0
    # Import locally to keep _common.py free of domain cycles.
    from docstats.domain.referrals import STATUS_VALUES, TERMINAL_STATUSES

    open_statuses = tuple(s for s in STATUS_VALUES if s not in TERMINAL_STATUSES)
    return storage.count_referrals(scope, assigned_to_user_id=user_id, statuses=open_statuses)


def resolve_assignee_filter(
    assignee: str | None,
    assigned_to_user_id: int | None,
    current_user_id: int,
) -> tuple[int | None, str | None]:
    """Resolve workspace/export assignee shorthand to an effective user id.

    ``assignee=me`` resolves to the caller. A numeric ``assignee`` resolves
    to that user id and takes precedence over the legacy
    ``assigned_to_user_id`` query parameter. Unknown aliases are ignored so
    bad bookmarks fall back to the explicit numeric filter.
    """
    assignee_clean = assignee.strip() if assignee is not None else None
    assignee_clean = assignee_clean or None
    if assignee_clean == "me":
        return current_user_id, assignee_clean
    if assignee_clean and assignee_clean.isdigit():
        return int(assignee_clean), assignee_clean
    return assigned_to_user_id, assignee_clean


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
