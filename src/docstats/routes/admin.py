"""Admin console — Phase 6.

Role-gated org administration. Every route here requires:

- An authenticated user (``require_user`` via :func:`require_admin_scope`).
- An active org membership (``scope.is_org`` True).
- A membership role at or above ``admin`` (``has_role_at_least(role, "admin")``).

Solo users and sub-admin org members get a 403. The route body never executes
for them — the dependency raises before the handler runs.

This file ships all six Phase 6 admin surfaces:

- 6.A: foundation + ``GET /admin`` overview
- 6.B: specialty-rules editor
- 6.C: payer-rules editor
- 6.D: org settings
- 6.E: audit log viewer
- 6.F: members + invitations
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse

from docstats.auth import require_user
from docstats.domain.audit import record as audit_record
from docstats.domain.invitations import (
    DEFAULT_INVITATION_TTL_SECONDS,
    compute_expires_at,
    generate_token,
    validate_role,
)
from docstats.domain.orgs import ROLES, Organization, has_role_at_least
from docstats.domain.reference import PayerRule, SpecialtyRule
from docstats.domain.rules import REQUIRED_FIELD_CHECKS
from docstats.routes._common import US_STATES, get_scope, redirect_htmx, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.validators import EMAIL_MAX_LENGTH, ValidationError, validate_email, validate_npi

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


# ---------------------------------------------------------------------------
# Specialty rules (Phase 6.B)
#
# The rules engine merges globals + org overrides via "override wins per
# specialty_code" — editing a rule in the admin UI means creating (or
# updating) an ``organization_id = <this org>`` override row that shadows
# the platform default. Revert = hard-delete the override, falling back to
# the global again. Rule rows hard-delete (no soft-delete) by design — the
# admin audit log is the source of truth for who changed what when.
#
# Payload shapes (what the engine reads):
#
# - ``required_fields``        → {"fields": [referral-column-name, ...]}
# - ``recommended_attachments`` → {"kinds": [...], "labels": [str, ...]}
#   (engine only reads ``labels``; we expose labels only in the UI and
#   preserve the incoming ``kinds`` as-is so we don't drop data)
# - ``intake_questions``       → {"prompts": [str, ...]}
# - ``urgency_red_flags``      → {"keywords": [str, ...]}
# - ``common_rejection_reasons`` → {"reasons": [str, ...]}
#
# All list-valued payloads are surfaced as newline-separated textareas in
# the edit form — one item per line, empty lines stripped.
# ---------------------------------------------------------------------------


def _split_lines(raw: str | None) -> list[str]:
    """Parse a textarea value into a trimmed, empty-line-free list."""
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _join_lines(items: list[str] | None) -> str:
    """Render a list as newline-separated text for a textarea default."""
    return "\n".join(items or [])


def _required_field_options() -> list[tuple[str, str]]:
    """Ordered (field_name, human_label) pairs the engine knows how to check.

    The admin UI only lets admins pick from these — picking an unknown field
    name silently no-ops in the rules engine, so it's better to keep the
    select bounded to the real vocabulary. Callers that need to extend the
    vocabulary add to ``REQUIRED_FIELD_CHECKS`` in ``domain.rules``.
    """
    return list(REQUIRED_FIELD_CHECKS.items())


def _find_specialty_rule_for(
    storage: StorageBase,
    *,
    organization_id: int | None,
    specialty_code: str,
) -> SpecialtyRule | None:
    """Return the single rule row matching ``(organization_id, specialty_code)``.

    The partial unique index guarantees at most one global and at most one
    override per code; with both filters applied, we always get 0 or 1 row.
    Passing ``organization_id=None`` returns the global; passing an int
    returns that org's override (if any).
    """
    rows = storage.list_specialty_rules(
        organization_id=organization_id,
        include_globals=(organization_id is None),
        specialty_code=specialty_code,
    )
    for row in rows:
        if row.organization_id == organization_id:
            return row
    return None


def _pair_global_and_override(
    rules: list[SpecialtyRule],
) -> list[dict[str, Any]]:
    """Group a flat rule list into ``[{"global": ..., "override": ...}, ...]``.

    Input is ``storage.list_specialty_rules(organization_id=X,
    include_globals=True)`` which returns both globals and overrides ordered
    ``(specialty_code, organization_id NULLS FIRST, id)``. We iterate once
    and bucket by ``specialty_code``. Codes that only have an override (i.e.
    a custom specialty not in SPECIALTY_DEFAULTS) still render — the
    ``global`` key is then None.
    """
    by_code: dict[str, dict[str, SpecialtyRule | None]] = {}
    for rule in rules:
        bucket = by_code.setdefault(rule.specialty_code, {"global": None, "override": None})
        if rule.organization_id is None:
            bucket["global"] = rule
        else:
            bucket["override"] = rule
    out = []
    for code in sorted(by_code.keys()):
        bucket = by_code[code]
        out.append(
            {
                "code": code,
                "global_rule": bucket["global"],
                "override": bucket["override"],
                # Pick the display name users will read (override wins).
                "display_name": (
                    (bucket["override"].display_name if bucket["override"] else None)
                    or (bucket["global"].display_name if bucket["global"] else None)
                    or code
                ),
            }
        )
    return out


@router.get("/specialty-rules", response_class=HTMLResponse)
async def specialty_rules_list(
    request: Request,
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """List every specialty rule visible to this org: globals + overrides."""
    org = _require_org(scope, storage)
    rules = storage.list_specialty_rules(
        organization_id=scope.organization_id,
        include_globals=True,
    )
    rows = _pair_global_and_override(rules)
    return render(
        "admin/specialty_rules_list.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            active_section="specialty-rules",
            rows=rows,
        ),
    )


def _edit_form_values(
    *,
    source: SpecialtyRule | None,
    fallback_global: SpecialtyRule | None,
) -> dict[str, Any]:
    """Pick the row to seed the edit form from.

    When the admin clicks "Edit override" and an override already exists we
    show its values. When they click "Create override" (or no override yet),
    we pre-populate from the global so they start from a known-good baseline
    — saving without changes creates an override identical to the global,
    which is harmless (the effective rule is unchanged).
    """
    src = source if source is not None else fallback_global
    if src is None:
        return {
            "display_name": "",
            "required_field_choices": [],
            "recommended_attachment_labels": "",
            "intake_question_prompts": "",
            "urgency_red_flag_keywords": "",
            "common_rejection_reasons": "",
            "kinds_preserve": [],
        }
    req_fields = src.required_fields.get("fields", []) if src.required_fields else []
    return {
        "display_name": src.display_name or "",
        "required_field_choices": [f for f in req_fields if isinstance(f, str)],
        "recommended_attachment_labels": _join_lines(
            [x for x in (src.recommended_attachments.get("labels", []) or []) if isinstance(x, str)]
        ),
        "intake_question_prompts": _join_lines(
            [x for x in (src.intake_questions.get("prompts", []) or []) if isinstance(x, str)]
        ),
        "urgency_red_flag_keywords": _join_lines(
            [x for x in (src.urgency_red_flags.get("keywords", []) or []) if isinstance(x, str)]
        ),
        "common_rejection_reasons": _join_lines(
            [
                x
                for x in (src.common_rejection_reasons.get("reasons", []) or [])
                if isinstance(x, str)
            ]
        ),
        # Preserve ``kinds`` verbatim — engine doesn't read it, but the seed
        # data ships with it and we don't want to silently drop data on
        # first edit.
        "kinds_preserve": src.recommended_attachments.get("kinds", [])
        if src.recommended_attachments
        else [],
    }


@router.get("/specialty-rules/{specialty_code}", response_class=HTMLResponse)
async def specialty_rule_edit_form(
    request: Request,
    specialty_code: str = Path(..., min_length=1, max_length=32),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Edit-form GET. Works for both "create override" and "edit override"."""
    org = _require_org(scope, storage)
    global_rule = _find_specialty_rule_for(
        storage, organization_id=None, specialty_code=specialty_code
    )
    override = _find_specialty_rule_for(
        storage,
        organization_id=scope.organization_id,
        specialty_code=specialty_code,
    )
    if global_rule is None and override is None:
        # Neither a platform default nor an org override exists for this code.
        # Admins creating custom specialties from scratch is out of scope for
        # 6.B; we only expose the edit path for codes that already have a row.
        raise HTTPException(status_code=404, detail="Specialty rule not found.")
    form = _edit_form_values(source=override, fallback_global=global_rule)
    return render(
        "admin/specialty_rule_edit.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            active_section="specialty-rules",
            specialty_code=specialty_code,
            global_rule=global_rule,
            override=override,
            form=form,
            required_field_options=_required_field_options(),
            errors=None,
        ),
    )


def _collect_form_payload(
    *,
    display_name: str,
    required_fields: list[str],
    recommended_labels: str,
    intake_prompts: str,
    red_flag_keywords: str,
    rejection_reasons: str,
    kinds_preserve: list[str],
) -> dict[str, Any]:
    """Assemble the kwargs the storage layer expects."""
    return {
        "display_name": display_name.strip() or None,
        "required_fields": {
            "fields": [
                f
                for f in required_fields
                # Filter unknown field names at the boundary — storage
                # accepts anything, but the rules engine would silently
                # ignore them. Loud is better than lossy.
                if f in REQUIRED_FIELD_CHECKS
            ],
        },
        "recommended_attachments": {
            "kinds": kinds_preserve,
            "labels": _split_lines(recommended_labels),
        },
        "intake_questions": {"prompts": _split_lines(intake_prompts)},
        "urgency_red_flags": {"keywords": _split_lines(red_flag_keywords)},
        "common_rejection_reasons": {"reasons": _split_lines(rejection_reasons)},
    }


_MAX_REQUIRED_FIELDS = 64


@router.post("/specialty-rules/{specialty_code}", response_class=HTMLResponse)
async def specialty_rule_save(
    request: Request,
    specialty_code: str = Path(..., min_length=1, max_length=32),
    display_name: str = Form("", max_length=200),
    # Multiple checkboxes with the same ``name`` arrive as a list on the
    # form; FastAPI / Starlette auto-coerces when the annotation is list.
    required_field: list[str] = Form(default_factory=list),
    recommended_attachment_labels: str = Form("", max_length=4000),
    intake_question_prompts: str = Form("", max_length=4000),
    urgency_red_flag_keywords: str = Form("", max_length=4000),
    common_rejection_reasons: str = Form("", max_length=4000),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Create or update the org override for ``specialty_code``."""
    # Cap the list size at the boundary so a malicious client can't force
    # an unbounded iteration before the vocabulary filter kicks in.
    # ``REQUIRED_FIELD_CHECKS`` today has 10 entries; 64 is generous
    # headroom for future additions without breaking this cap.
    if len(required_field) > _MAX_REQUIRED_FIELDS:
        raise HTTPException(
            status_code=422,
            detail=f"Too many required_field entries (max {_MAX_REQUIRED_FIELDS}).",
        )
    # Side-effect only: validates the org row exists; redirect on save so
    # the full template ctx isn't needed here.
    _require_org(scope, storage)
    global_rule = _find_specialty_rule_for(
        storage, organization_id=None, specialty_code=specialty_code
    )
    existing_override = _find_specialty_rule_for(
        storage,
        organization_id=scope.organization_id,
        specialty_code=specialty_code,
    )
    if global_rule is None and existing_override is None:
        raise HTTPException(status_code=404, detail="Specialty rule not found.")

    # Preserve the kinds payload from whichever source the edit form seeded
    # from (override if present, else global). The form never exposed it, so
    # we must not drop it on save.
    kinds_preserve_src = existing_override or global_rule
    kinds_preserve: list[str] = []
    if kinds_preserve_src and kinds_preserve_src.recommended_attachments:
        raw_kinds = kinds_preserve_src.recommended_attachments.get("kinds", []) or []
        kinds_preserve = [k for k in raw_kinds if isinstance(k, str)]

    payload = _collect_form_payload(
        display_name=display_name,
        required_fields=required_field,
        recommended_labels=recommended_attachment_labels,
        intake_prompts=intake_question_prompts,
        red_flag_keywords=urgency_red_flag_keywords,
        rejection_reasons=common_rejection_reasons,
        kinds_preserve=kinds_preserve,
    )

    if existing_override is None:
        try:
            storage.create_specialty_rule(
                specialty_code=specialty_code,
                organization_id=scope.organization_id,
                display_name=payload["display_name"],
                required_fields=payload["required_fields"],
                recommended_attachments=payload["recommended_attachments"],
                intake_questions=payload["intake_questions"],
                urgency_red_flags=payload["urgency_red_flags"],
                common_rejection_reasons=payload["common_rejection_reasons"],
                source="admin_override",
            )
        except Exception:
            # Race: another admin created an override for the same
            # ``(organization_id, specialty_code)`` between our guard read
            # and this insert. The partial unique index surfaces that as a
            # backend-specific IntegrityError (sqlite3 / psycopg). Re-query
            # and fall through to the update branch — "last write wins" is
            # acceptable semantics for concurrent admin edits.
            existing_override = _find_specialty_rule_for(
                storage,
                organization_id=scope.organization_id,
                specialty_code=specialty_code,
            )
            if existing_override is None:
                # Couldn't recover — real error, re-raise.
                raise
        audit_action = "admin.specialty_rule.create_override"

    if existing_override is not None:
        updated = storage.update_specialty_rule(
            existing_override.id,
            display_name=payload["display_name"],
            required_fields=payload["required_fields"],
            recommended_attachments=payload["recommended_attachments"],
            intake_questions=payload["intake_questions"],
            urgency_red_flags=payload["urgency_red_flags"],
            common_rejection_reasons=payload["common_rejection_reasons"],
            source="admin_override",
            # ``bump_version=True`` (default) — real admin edits SHOULD
            # invalidate any rule-engine cache that keys on version_id,
            # even though the engine doesn't cache yet in Phase 3.
            #
            # ``overwrite=True`` is load-bearing: ``_collect_form_payload``
            # converts an empty ``display_name`` form field to ``None``,
            # and the default ``overwrite=False`` would silently skip
            # ``None`` kwargs ("leave unchanged"). An admin who clears the
            # field expects it cleared, not preserved.
            overwrite=True,
        )
        if updated is None:
            # TOCTOU: the override was deleted between our guard read and
            # this update (e.g. another admin just reverted it). Don't
            # emit an audit event for a write that didn't land — surface
            # a 404 so the client knows to retry.
            raise HTTPException(status_code=404, detail="Specialty rule override not found.")
        audit_action = "admin.specialty_rule.update_override"

    audit_record(
        storage,
        action=audit_action,
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="specialty_rule",
        entity_id=specialty_code,
    )
    return redirect_htmx(request, "/admin/specialty-rules")


@router.post("/specialty-rules/{specialty_code}/revert", response_class=HTMLResponse)
async def specialty_rule_revert(
    request: Request,
    specialty_code: str = Path(..., min_length=1, max_length=32),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Delete the org override, restoring the global platform default."""
    _require_org(scope, storage)
    override = _find_specialty_rule_for(
        storage,
        organization_id=scope.organization_id,
        specialty_code=specialty_code,
    )
    if override is None:
        # No override to revert — idempotent; just send them back.
        return redirect_htmx(request, "/admin/specialty-rules")

    deleted = storage.delete_specialty_rule(override.id)
    if not deleted:
        # Row vanished between our read and the delete; treat as already
        # reverted. Don't emit an audit event for a no-op.
        return redirect_htmx(request, "/admin/specialty-rules")

    audit_record(
        storage,
        action="admin.specialty_rule.revert_override",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="specialty_rule",
        entity_id=specialty_code,
    )
    return redirect_htmx(request, "/admin/specialty-rules")


# ---------------------------------------------------------------------------
# Payer rules (Phase 6.C)
#
# Mirrors the specialty-rules editor in shape — list + edit/create/revert
# override — but with a smaller payload. The rules engine (Phase 3) reads:
#
# - ``referral_required``             → bool (does the plan gate specialist
#                                       visits behind a PCP referral?)
# - ``auth_required_services``        → {"services": [str, ...]} (services
#                                       that need prior auth)
# - ``auth_typical_turnaround_days``  → int | None (heuristic)
# - ``records_required``              → {"kinds": [str, ...]} (what to
#                                       attach for the payer to accept)
# - ``notes``                         → str | None (free text)
#
# ``payer_key`` is the canonical identifier; seed defaults use the shape
# ``{payer_name}|{plan_type}`` (e.g. ``Kaiser Permanente|hmo``). The pipe
# and spaces are URL-encoded in admin URLs; FastAPI path params decode
# transparently. For the 6.C UI the admin can only edit codes that already
# have a row — creating a brand-new payer_key from scratch is out of scope
# (same bound applied to specialty rules in 6.B).
# ---------------------------------------------------------------------------


def _find_payer_rule_for(
    storage: StorageBase,
    *,
    organization_id: int | None,
    payer_key: str,
) -> PayerRule | None:
    """Return the single payer-rule row matching ``(organization_id, payer_key)``.

    Mirrors :func:`_find_specialty_rule_for`. Partial unique indices make at
    most one global and at most one org override per key, so with both
    filters applied we get 0 or 1 row.
    """
    rows = storage.list_payer_rules(
        organization_id=organization_id,
        include_globals=(organization_id is None),
        payer_key=payer_key,
    )
    for row in rows:
        if row.organization_id == organization_id:
            return row
    return None


def _pair_global_and_override_payers(
    rules: list[PayerRule],
) -> list[dict[str, Any]]:
    """Group a flat payer-rule list into ``[{"global": ..., "override": ...}, ...]``.

    Parallel to ``_pair_global_and_override`` for specialty rules.
    """
    by_key: dict[str, dict[str, PayerRule | None]] = {}
    for rule in rules:
        bucket = by_key.setdefault(rule.payer_key, {"global": None, "override": None})
        if rule.organization_id is None:
            bucket["global"] = rule
        else:
            bucket["override"] = rule
    out = []
    for key in sorted(by_key.keys()):
        bucket = by_key[key]
        out.append(
            {
                "key": key,
                "global_rule": bucket["global"],
                "override": bucket["override"],
                "display_name": (
                    (bucket["override"].display_name if bucket["override"] else None)
                    or (bucket["global"].display_name if bucket["global"] else None)
                    or key
                ),
            }
        )
    return out


@router.get("/payer-rules", response_class=HTMLResponse)
async def payer_rules_list(
    request: Request,
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """List every payer rule visible to this org: globals + overrides."""
    org = _require_org(scope, storage)
    rules = storage.list_payer_rules(
        organization_id=scope.organization_id,
        include_globals=True,
    )
    rows = _pair_global_and_override_payers(rules)
    return render(
        "admin/payer_rules_list.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            active_section="payer-rules",
            rows=rows,
        ),
    )


def _payer_edit_form_values(
    *,
    source: PayerRule | None,
    fallback_global: PayerRule | None,
) -> dict[str, Any]:
    """Seed the edit form from an existing override else the global default."""
    src = source if source is not None else fallback_global
    if src is None:
        return {
            "display_name": "",
            "referral_required": False,
            "auth_required_services": "",
            "auth_typical_turnaround_days": "",
            "records_required": "",
            "notes": "",
        }
    return {
        "display_name": src.display_name or "",
        "referral_required": bool(src.referral_required),
        "auth_required_services": _join_lines(
            [
                x
                for x in (src.auth_required_services.get("services", []) or [])
                if isinstance(x, str)
            ]
        ),
        "auth_typical_turnaround_days": (
            str(src.auth_typical_turnaround_days)
            if src.auth_typical_turnaround_days is not None
            else ""
        ),
        "records_required": _join_lines(
            [x for x in (src.records_required.get("kinds", []) or []) if isinstance(x, str)]
        ),
        "notes": src.notes or "",
    }


@router.get("/payer-rules/{payer_key}", response_class=HTMLResponse)
async def payer_rule_edit_form(
    request: Request,
    payer_key: str = Path(..., min_length=1, max_length=200),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Edit-form GET. Works for both "create override" and "edit override"."""
    org = _require_org(scope, storage)
    global_rule = _find_payer_rule_for(storage, organization_id=None, payer_key=payer_key)
    override = _find_payer_rule_for(
        storage,
        organization_id=scope.organization_id,
        payer_key=payer_key,
    )
    if global_rule is None and override is None:
        # No platform default AND no org override: out of scope for 6.C.
        raise HTTPException(status_code=404, detail="Payer rule not found.")
    form = _payer_edit_form_values(source=override, fallback_global=global_rule)
    return render(
        "admin/payer_rule_edit.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            active_section="payer-rules",
            payer_key=payer_key,
            global_rule=global_rule,
            override=override,
            form=form,
            errors=None,
        ),
    )


def _parse_turnaround_days(raw: str) -> int | None:
    """Empty string → None; int string → int; anything else → 422."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        val = int(s)
    except ValueError:
        raise HTTPException(status_code=422, detail="Auth turnaround days must be an integer.")
    if val < 0 or val > 365:
        raise HTTPException(status_code=422, detail="Auth turnaround days must be 0–365.")
    return val


@router.post("/payer-rules/{payer_key}/revert", response_class=HTMLResponse)
async def payer_rule_revert(
    request: Request,
    payer_key: str = Path(..., min_length=1, max_length=200),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Delete the org override, restoring the global platform default."""
    _require_org(scope, storage)
    override = _find_payer_rule_for(
        storage,
        organization_id=scope.organization_id,
        payer_key=payer_key,
    )
    if override is None:
        return redirect_htmx(request, "/admin/payer-rules")

    deleted = storage.delete_payer_rule(override.id)
    if not deleted:
        # TOCTOU: row vanished; treat as already reverted, no audit event.
        return redirect_htmx(request, "/admin/payer-rules")

    audit_record(
        storage,
        action="admin.payer_rule.revert_override",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="payer_rule",
        entity_id=payer_key,
    )
    return redirect_htmx(request, "/admin/payer-rules")


@router.post("/payer-rules/{payer_key}", response_class=HTMLResponse)
async def payer_rule_save(
    request: Request,
    payer_key: str = Path(..., min_length=1, max_length=200),
    display_name: str = Form("", max_length=200),
    # HTML checkbox convention: unchecked = absent from form data; Form
    # with a default of "off" lets us treat "on" as True and anything else
    # (absent or empty) as False.
    referral_required: str = Form("off", max_length=16),
    auth_required_services: str = Form("", max_length=4000),
    auth_typical_turnaround_days: str = Form("", max_length=8),
    records_required: str = Form("", max_length=4000),
    notes: str = Form("", max_length=2000),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Create or update the org override for ``payer_key``."""
    # Route ordering: ``/revert`` is declared above so its literal suffix
    # is registered first. The default (non-``:path``) path converter
    # stops at ``/``, so even if the order swapped the revert suffix
    # wouldn't be swallowed — but registering specific-before-parameterized
    # is the project convention and keeps this robust if ``payer_key``
    # ever admits slashes.
    _require_org(scope, storage)
    global_rule = _find_payer_rule_for(storage, organization_id=None, payer_key=payer_key)
    existing_override = _find_payer_rule_for(
        storage,
        organization_id=scope.organization_id,
        payer_key=payer_key,
    )
    if global_rule is None and existing_override is None:
        raise HTTPException(status_code=404, detail="Payer rule not found.")

    turnaround = _parse_turnaround_days(auth_typical_turnaround_days)
    ref_req = referral_required.lower() in {"on", "true", "1", "yes"}

    payload: dict[str, Any] = {
        "display_name": display_name.strip() or None,
        "referral_required": ref_req,
        "auth_required_services": {"services": _split_lines(auth_required_services)},
        "auth_typical_turnaround_days": turnaround,
        "records_required": {"kinds": _split_lines(records_required)},
        "notes": notes.strip() or None,
    }

    if existing_override is None:
        try:
            storage.create_payer_rule(
                payer_key=payer_key,
                organization_id=scope.organization_id,
                display_name=payload["display_name"],
                referral_required=payload["referral_required"],
                auth_required_services=payload["auth_required_services"],
                auth_typical_turnaround_days=payload["auth_typical_turnaround_days"],
                records_required=payload["records_required"],
                notes=payload["notes"],
                source="admin_override",
            )
        except Exception:
            # Race: another admin created an override for the same
            # ``(organization_id, payer_key)`` between our guard read and
            # this insert. Re-query and fall through to the update path.
            # Same pattern + rationale as specialty_rule_save above.
            existing_override = _find_payer_rule_for(
                storage,
                organization_id=scope.organization_id,
                payer_key=payer_key,
            )
            if existing_override is None:
                raise
        audit_action = "admin.payer_rule.create_override"

    if existing_override is not None:
        updated = storage.update_payer_rule(
            existing_override.id,
            display_name=payload["display_name"],
            referral_required=payload["referral_required"],
            auth_required_services=payload["auth_required_services"],
            auth_typical_turnaround_days=payload["auth_typical_turnaround_days"],
            records_required=payload["records_required"],
            notes=payload["notes"],
            source="admin_override",
            # ``overwrite=True`` load-bearing for the same reason as 6.B:
            # clearing ``display_name``, ``notes``, or ``auth_typical_turnaround_days``
            # from the form should write ``None`` through instead of the
            # default "leave unchanged" contract preserving the old value.
            overwrite=True,
        )
        if updated is None:
            # TOCTOU: the override was deleted between our guard read and
            # this update. Don't emit an audit event for a write that
            # didn't land — surface a 404 so the client knows to retry.
            raise HTTPException(status_code=404, detail="Payer rule override not found.")
        audit_action = "admin.payer_rule.update_override"

    audit_record(
        storage,
        action=audit_action,
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="payer_rule",
        entity_id=payer_key,
    )
    return redirect_htmx(request, "/admin/payer-rules")


# ---------------------------------------------------------------------------
# Org settings (Phase 6.D)
#
# Simple form — name + NPI + address + phone/fax. The org's ``slug`` is
# intentionally not editable here: changing it would break bookmarked
# URLs and any downstream integrations keyed on slug. Letterhead upload
# is deferred to Phase 6.5.
#
# Storage: ``update_organization(org_id, *, overwrite=True, ...)`` writes
# every kwarg literally so an empty form submission clears the optional
# fields. ``name`` is NOT NULL in the schema; the route validates it's
# non-empty before calling storage. Cross-tenant access is impossible —
# the org_id comes from the resolved ``Scope`` via ``require_admin_scope``,
# not from a path param.
# ---------------------------------------------------------------------------

_VALID_STATE_CODES: frozenset[str] = frozenset(code for code, _ in US_STATES)


def _org_settings_form_values(org: Organization) -> dict[str, str]:
    """Render the Organization row as a dict of form defaults (all strings)."""
    return {
        "name": org.name or "",
        "npi": org.npi or "",
        "address_line1": org.address_line1 or "",
        "address_line2": org.address_line2 or "",
        "address_city": org.address_city or "",
        "address_state": org.address_state or "",
        "address_zip": org.address_zip or "",
        "phone": org.phone or "",
        "fax": org.fax or "",
    }


@router.get("/org-settings", response_class=HTMLResponse)
async def org_settings_form(
    request: Request,
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Render the org-settings form seeded from the current row."""
    org = _require_org(scope, storage)
    return render(
        "admin/org_settings.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            active_section="org-settings",
            form=_org_settings_form_values(org),
            states=US_STATES,
            errors=None,
        ),
    )


def _clean(value: str | None) -> str | None:
    """Trim whitespace; collapse empty/whitespace-only to None."""
    if value is None:
        return None
    v = value.strip()
    return v or None


_ZIP_PATTERN = re.compile(r"^\d{5}(?:-?\d{4})?$")


@router.post("/org-settings", response_class=HTMLResponse)
async def org_settings_save(
    request: Request,
    name: str = Form(..., max_length=200),
    npi: str = Form("", max_length=10),
    address_line1: str = Form("", max_length=200),
    address_line2: str = Form("", max_length=200),
    address_city: str = Form("", max_length=100),
    address_state: str = Form("", max_length=2),
    address_zip: str = Form("", max_length=10),
    phone: str = Form("", max_length=40),
    fax: str = Form("", max_length=40),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Save org settings. Empty form fields clear the column."""
    org = _require_org(scope, storage)
    name_clean = name.strip()
    errors: list[str] = []
    if not name_clean:
        errors.append("Organization name is required.")

    npi_clean = _clean(npi)
    if npi_clean is not None:
        try:
            npi_clean = validate_npi(npi_clean)
        except ValidationError as e:
            errors.append(str(e))

    state_clean = _clean(address_state)
    if state_clean is not None:
        state_clean = state_clean.upper()
        if state_clean not in _VALID_STATE_CODES:
            errors.append(f"Unknown state code: {state_clean!r}.")

    zip_clean = _clean(address_zip)
    if zip_clean is not None and not _ZIP_PATTERN.match(zip_clean):
        errors.append("ZIP must be 5 digits or 5+4 (e.g. 94110 or 94110-1234).")

    # Preserve the admin's typing across every error path below — both the
    # route-level validation branch and the storage ValueError branch
    # re-render from this dict so a rejected save doesn't cost them the
    # values they already typed.
    submitted = {
        "name": name,
        "npi": npi,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "address_city": address_city,
        "address_state": address_state,
        "address_zip": address_zip,
        "phone": phone,
        "fax": fax,
    }

    if errors:
        return render(
            "admin/org_settings.html",
            _ctx(
                request,
                current_user,
                storage,
                scope,
                org,
                active_section="org-settings",
                form=submitted,
                states=US_STATES,
                errors=errors,
            ),
        )

    try:
        updated = storage.update_organization(
            org.id,
            name=name_clean,
            npi=npi_clean,
            address_line1=_clean(address_line1),
            address_line2=_clean(address_line2),
            address_city=_clean(address_city),
            address_state=state_clean,
            address_zip=zip_clean,
            phone=_clean(phone),
            fax=_clean(fax),
            overwrite=True,
        )
    except ValueError as e:
        return render(
            "admin/org_settings.html",
            _ctx(
                request,
                current_user,
                storage,
                scope,
                org,
                active_section="org-settings",
                form=submitted,
                states=US_STATES,
                errors=[str(e)],
            ),
        )

    if updated is None:
        # Row vanished mid-flight (soft-deleted). Vanishingly unlikely under
        # an admin's own session but the storage contract allows it — surface
        # a 404 so the client knows the write didn't land.
        raise HTTPException(status_code=404, detail="Organization not found.")

    audit_record(
        storage,
        action="admin.org.update",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="organization",
        entity_id=str(org.id),
    )
    return redirect_htmx(request, "/admin/org-settings")


# ---------------------------------------------------------------------------
# Audit log viewer (Phase 6.E)
#
# Read-only surface. Filters: action (exact match), entity_type, entity_id,
# actor_user_id, since/until (date-only inputs from the form; coerced to
# midnight UTC). Pagination: offset + fixed page size. Scope is pinned to
# the active org — an admin cannot see audit events for other tenants
# through this UI.
#
# Action vocabulary: presented as a ``<datalist>`` for autocomplete so the
# form stays typable for unknown action strings (e.g. actions added in
# future phases) while still offering suggestions for the common ones.
# ---------------------------------------------------------------------------

_AUDIT_PAGE_SIZE = 50

# The list doesn't have to be exhaustive — it just seeds the datalist UI.
# New actions added in future phases don't need to touch this list
# immediately; the free-text input still accepts them.
_KNOWN_AUDIT_ACTIONS: tuple[str, ...] = (
    "admin.member.invitation_revoke",
    "admin.member.invite",
    "admin.member.joined",
    "admin.member.remove",
    "admin.member.role_change",
    "admin.org.update",
    "admin.payer_rule.create_override",
    "admin.payer_rule.revert_override",
    "admin.payer_rule.update_override",
    "admin.specialty_rule.create_override",
    "admin.specialty_rule.revert_override",
    "admin.specialty_rule.update_override",
    "import.commit",
    "import.create",
    "import.map",
    "import.row.edit",
    "import.validate",
    "patient.create",
    "patient.delete",
    "patient.update",
    "provider.save",
    "provider.unsave",
    "referral.create",
    "referral.delete",
    "referral.export",
    "referral.status",
    "referral.update",
    "user.login",
    "user.login_failed",
    "user.login_github",
    "user.logout",
    "user.signup",
    "user.terms_accepted",
)


def _parse_actor_user_id(value: str | None) -> int | None:
    """Parse the audit filter's ``actor_user_id`` query param.

    Accepted as a string so a blank form submission (``?actor_user_id=``)
    doesn't fail FastAPI's int validator — browsers always include empty
    inputs in GET forms, so the filter must treat blank as "no filter"
    rather than "invalid". Non-integer or non-positive values raise 422.
    """
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"actor_user_id must be an integer: {value!r}")
    if parsed < 1:
        raise HTTPException(status_code=422, detail="actor_user_id must be >= 1")
    return parsed


def _parse_date_filter(value: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse an HTML ``<input type="date">`` value (``YYYY-MM-DD``) to UTC.

    The storage filter treats ``since`` as inclusive and ``until`` as
    exclusive. For the UI that means:

    - ``since`` → midnight UTC of the given date (inclusive, so all events
      from the start of the day onwards match).
    - ``until`` → midnight UTC of the day AFTER the given date (exclusive,
      so the full day the admin typed is included).

    Malformed inputs raise ``HTTPException(422)`` so a typo isn't silently
    ignored.
    """
    if value is None or not value.strip():
        return None
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Date must be YYYY-MM-DD: {value!r}")
    if end_of_day:
        # Advance one day so the caller's date is inclusive (exclusive upper).
        d = d + timedelta(days=1)
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


_MAX_AUDIT_OFFSET = 10_000


@router.get("/audit", response_class=HTMLResponse)
async def audit_log(
    request: Request,
    action: str | None = Query(None, max_length=64),
    entity_type: str | None = Query(None, max_length=32),
    entity_id: str | None = Query(None, max_length=64),
    # Accept as string so a blank form field (``?actor_user_id=``) doesn't
    # 422 through FastAPI's int validator. Coerced below; non-integer or
    # non-positive values raise 422 explicitly.
    actor_user_id: str | None = Query(None, max_length=16),
    since: str | None = Query(None, max_length=10),
    until: str | None = Query(None, max_length=10),
    offset: int = Query(0, ge=0, le=_MAX_AUDIT_OFFSET),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Paginated audit-log viewer scoped to the active org."""
    org = _require_org(scope, storage)

    # Normalize empty strings to None so the storage filter treats them as
    # "no filter" rather than exact-match "".
    action_clean = action.strip() if action and action.strip() else None
    entity_type_clean = entity_type.strip() if entity_type and entity_type.strip() else None
    entity_id_clean = entity_id.strip() if entity_id and entity_id.strip() else None

    actor_user_id_clean = _parse_actor_user_id(actor_user_id)

    since_dt = _parse_date_filter(since, end_of_day=False)
    until_dt = _parse_date_filter(until, end_of_day=True)

    # Fetch one extra row to know if there's a next page without a count query.
    events = storage.list_audit_events(
        scope_organization_id=scope.organization_id,
        action=action_clean,
        entity_type=entity_type_clean,
        entity_id=entity_id_clean,
        actor_user_id=actor_user_id_clean,
        since=since_dt,
        until=until_dt,
        limit=_AUDIT_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(events) > _AUDIT_PAGE_SIZE
    if has_next:
        events = events[:_AUDIT_PAGE_SIZE]
    has_prev = offset > 0
    # Clamp next_offset at the same cap we accept on input — otherwise the
    # admin paging past 9950 would render a Next link that triggers a 422
    # on click. Once the next step would exceed the cap, hide the link.
    raw_next = offset + _AUDIT_PAGE_SIZE
    next_offset = raw_next if has_next and raw_next <= _MAX_AUDIT_OFFSET else None
    prev_offset = max(0, offset - _AUDIT_PAGE_SIZE) if has_prev else None

    # Reflect the current filter state back into the template so form
    # inputs and pagination links preserve it.
    filters = {
        "action": action_clean or "",
        "entity_type": entity_type_clean or "",
        "entity_id": entity_id_clean or "",
        "actor_user_id": str(actor_user_id) if actor_user_id is not None else "",
        "since": since or "",
        "until": until or "",
    }
    return render(
        "admin/audit.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            active_section="audit",
            events=events,
            filters=filters,
            known_actions=_KNOWN_AUDIT_ACTIONS,
            page_size=_AUDIT_PAGE_SIZE,
            offset=offset,
            next_offset=next_offset,
            prev_offset=prev_offset,
        ),
    )


# ---------------------------------------------------------------------------
# Members + invitations (Phase 6.F)
#
# Member management: list active memberships, change role, remove
# (soft-delete). Guards: an org must always have at least one active
# owner; callers can't remove themselves if they're the sole admin
# (otherwise they'd lose access without a path to fix it).
#
# Invitations: admins generate a magic link for a specific email+role.
# Phase 9 wires email delivery; until then the create response shows
# the URL for copy-paste. Redemption flow lives in ``routes/invite.py``
# because it's PUBLIC (pre-login for new users) — not admin-gated like
# everything else in this file.
#
# Audit actions:
#   - admin.member.invite
#   - admin.member.invitation_revoke
#   - admin.member.role_change
#   - admin.member.remove
#   - admin.member.joined (emitted by the redemption route)
# ---------------------------------------------------------------------------


def _count_active_members_with_role(storage: StorageBase, organization_id: int, role: str) -> int:
    """Count active memberships at a given role in the org."""
    return sum(
        1
        for m in storage.list_memberships_for_org(organization_id)
        if m.is_active and m.role == role
    )


def _can_grant_role(actor_role: str | None, target_role: str) -> bool:
    """Return True iff the actor's role permits granting ``target_role``.

    Enforces the ROLES hierarchy at the route boundary: an actor may
    grant any role up to and including their own — admins can grant
    admin / coordinator / clinician / staff / read_only; only an owner
    can grant owner. Without this check, a plain admin could invite or
    promote any member (including themselves) to owner and silently
    bypass the sole-owner guards on role_change / remove, collapsing
    the documented ROLES hierarchy.

    ``actor_role`` of ``None`` or an unknown string fails closed.
    """
    if actor_role is None or actor_role not in ROLES or target_role not in ROLES:
        return False
    return ROLES.index(actor_role) >= ROLES.index(target_role)


def _members_page_context(
    request: Request,
    current_user: dict,
    storage: StorageBase,
    scope: Scope,
    org: Organization,
    *,
    flash: str | None = None,
    flash_error: str | None = None,
    magic_link: str | None = None,
) -> dict:
    """Build the full ctx for the /admin/members page.

    Centralized so the invite-success path (which shows a copyable
    magic link) shares the exact render path with plain GETs.
    """
    memberships = [
        m
        for m in storage.list_memberships_for_org(scope.organization_id)  # type: ignore[arg-type]
        if m.is_active
    ]
    # Attach user display info for each membership.
    member_rows = []
    for m in memberships:
        user = storage.get_user_by_id(m.user_id)
        member_rows.append(
            {
                "membership": m,
                "email": (user.get("email") if user else f"user#{m.user_id}"),
                "display_name": (user.get("display_name") if user else None) if user else None,
                "is_self": m.user_id == current_user["id"],
            }
        )
    invitations = storage.list_invitations_for_org(scope.organization_id)  # type: ignore[arg-type]
    return _ctx(
        request,
        current_user,
        storage,
        scope,
        org,
        active_section="members",
        member_rows=member_rows,
        invitations=invitations,
        roles=ROLES,
        flash=flash,
        flash_error=flash_error,
        magic_link=magic_link,
    )


@router.get("/members", response_class=HTMLResponse)
async def members_list(
    request: Request,
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """List active memberships + pending invitations."""
    org = _require_org(scope, storage)
    return render(
        "admin/members.html",
        _members_page_context(request, current_user, storage, scope, org),
    )


def _invite_link_for(request: Request, token: str) -> str:
    """Build an absolute URL for the redemption page."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/invite/{token}"


@router.post("/members/invite", response_class=HTMLResponse)
async def members_invite(
    request: Request,
    email: str = Form(..., max_length=EMAIL_MAX_LENGTH),
    role: str = Form(..., max_length=32),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Create a pending invitation and render the magic link."""
    org = _require_org(scope, storage)

    errors: list[str] = []
    try:
        email_clean = validate_email(email)
    except ValidationError as e:
        errors.append(str(e))
        email_clean = email.strip()

    try:
        role_clean = validate_role(role)
    except ValueError as e:
        errors.append(str(e))
        role_clean = ""

    # Guard: actor can't grant a role above their own (admin→owner
    # escalation). `_can_grant_role` fails closed on unknown/None roles.
    if role_clean and not _can_grant_role(scope.membership_role, role_clean):
        errors.append(
            f"You don't have permission to invite at role {role_clean!r}. "
            f"Ask an owner if you need to create another owner."
        )

    # Guard: can't invite a user who's already an active member. Check by
    # email lookup; if the user doesn't exist yet, no membership can
    # exist either.
    if not errors:
        existing_user = storage.get_user_by_email(email_clean)
        if existing_user is not None:
            existing_membership = storage.get_membership(
                scope.organization_id,  # type: ignore[arg-type]
                existing_user["id"],
            )
            if existing_membership is not None and existing_membership.is_active:
                errors.append(f"{email_clean} is already an active member of this org.")

    if errors:
        return render(
            "admin/members.html",
            _members_page_context(
                request,
                current_user,
                storage,
                scope,
                org,
                flash_error=" ".join(errors),
            ),
        )

    token = generate_token()
    # Auto-revoke any EXPIRED pending invitation to the same email
    # before creating a new one. The partial unique index on
    # ``(organization_id, email) WHERE accepted_at IS NULL AND
    # revoked_at IS NULL`` can't filter on ``expires_at`` (it's a
    # non-deterministic function in both SQLite and Postgres — forbidden
    # in partial-index predicates). Without this sweep, an admin who
    # lets an invitation expire naturally can never re-invite the same
    # email without manual DB surgery, since `list_invitations_for_org`
    # default-hides expired rows and the unique index still holds the
    # slot. Include_expired=True surfaces them so we can clean up.
    now_utc = datetime.now(tz=timezone.utc)
    for stale in storage.list_invitations_for_org(
        scope.organization_id,  # type: ignore[arg-type]
        include_expired=True,
    ):
        if stale.email == email_clean and not stale.is_pending(now=now_utc):
            # Only touches already-expired (or already-accepted / revoked)
            # rows. ``revoke_invitation`` is a no-op on non-pending rows,
            # but our goal is to free the ``(org, email) WHERE pending``
            # slot — and by definition an expired row still satisfies
            # that predicate. Mark revoked so the index is freed.
            if stale.revoked_at is None and stale.accepted_at is None:
                storage.revoke_invitation(stale.id)
    try:
        invitation = storage.create_invitation(
            organization_id=scope.organization_id,  # type: ignore[arg-type]
            email=email_clean,
            role=role_clean,
            token=token,
            expires_at=compute_expires_at(DEFAULT_INVITATION_TTL_SECONDS),
            invited_by_user_id=current_user["id"],
        )
    except Exception:
        # Most likely cause: the partial unique index on
        # ``(organization_id, email) WHERE pending`` fired because a
        # pending invite already exists. Surface a friendly message
        # rather than a 500.
        return render(
            "admin/members.html",
            _members_page_context(
                request,
                current_user,
                storage,
                scope,
                org,
                flash_error=(
                    f"A pending invitation already exists for {email_clean}. "
                    "Revoke it first if you want to reissue."
                ),
            ),
        )

    audit_record(
        storage,
        action="admin.member.invite",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="invitation",
        entity_id=str(invitation.id),
        metadata={"email": email_clean, "role": role_clean},
    )
    return render(
        "admin/members.html",
        _members_page_context(
            request,
            current_user,
            storage,
            scope,
            org,
            flash=(
                f"Invitation created for {email_clean} ({role_clean}). "
                "Copy the link below and send it to them."
            ),
            magic_link=_invite_link_for(request, token),
        ),
    )


@router.post("/members/invitations/{invitation_id}/revoke", response_class=HTMLResponse)
async def members_invitation_revoke(
    request: Request,
    invitation_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Revoke a pending invitation."""
    _require_org(scope, storage)
    invitation = storage.get_invitation(invitation_id)
    if invitation is None or invitation.organization_id != scope.organization_id:
        # Cross-tenant or missing — 404 without leaking which.
        raise HTTPException(status_code=404, detail="Invitation not found.")
    storage.revoke_invitation(invitation_id)
    # Log regardless of whether the storage write changed state —
    # idempotent revoke attempts are still worth recording as actor
    # intent.
    audit_record(
        storage,
        action="admin.member.invitation_revoke",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="invitation",
        entity_id=str(invitation_id),
    )
    return redirect_htmx(request, "/admin/members")


@router.post("/members/{user_id}/role", response_class=HTMLResponse)
async def members_role_change(
    request: Request,
    user_id: int = Path(..., ge=1),
    role: str = Form(..., max_length=32),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Change a member's role. Guards: last-owner protection."""
    org = _require_org(scope, storage)
    # require_admin_scope guarantees is_org, so organization_id is not None.
    assert scope.organization_id is not None
    org_id: int = scope.organization_id

    try:
        new_role = validate_role(role)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Guard: actor can't grant a role above their own. Prevents admin→
    # owner self-promotion AND admin-promotes-peer-to-owner.
    if not _can_grant_role(scope.membership_role, new_role):
        return render(
            "admin/members.html",
            _members_page_context(
                request,
                current_user,
                storage,
                scope,
                org,
                flash_error=(
                    f"You don't have permission to grant role {new_role!r}. "
                    f"Only an owner can promote to owner."
                ),
            ),
        )

    membership = storage.get_membership(org_id, user_id)
    if membership is None or not membership.is_active:
        raise HTTPException(status_code=404, detail="Membership not found.")

    if new_role == membership.role:
        # No-op. Skip audit for a non-change.
        return redirect_htmx(request, "/admin/members")

    # Guard: demoting the sole active owner would orphan the org.
    if membership.role == "owner" and new_role != "owner":
        owner_count = _count_active_members_with_role(storage, org_id, "owner")
        if owner_count <= 1:
            return render(
                "admin/members.html",
                _members_page_context(
                    request,
                    current_user,
                    storage,
                    scope,
                    org,
                    flash_error=(
                        "Can't demote the sole owner. Promote another member to owner first."
                    ),
                ),
            )

    # Guard: admins can't demote themselves below admin unless another
    # admin/owner exists (otherwise they lose their own console access).
    if user_id == current_user["id"] and not has_role_at_least(new_role, "admin"):
        higher_count = sum(
            1
            for m in storage.list_memberships_for_org(org_id)
            if m.is_active and m.user_id != user_id and has_role_at_least(m.role, "admin")
        )
        if higher_count == 0:
            return render(
                "admin/members.html",
                _members_page_context(
                    request,
                    current_user,
                    storage,
                    scope,
                    org,
                    flash_error=(
                        "Can't demote yourself below admin while you're the "
                        "only admin/owner. Promote someone else first."
                    ),
                ),
            )

    ok = storage.update_membership_role(org_id, user_id, new_role)
    if not ok:
        # TOCTOU: membership vanished between guard and write.
        raise HTTPException(status_code=404, detail="Membership not found.")
    audit_record(
        storage,
        action="admin.member.role_change",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="membership",
        entity_id=str(user_id),
        metadata={"from": membership.role, "to": new_role},
    )
    return redirect_htmx(request, "/admin/members")


@router.post("/members/{user_id}/remove", response_class=HTMLResponse)
async def members_remove(
    request: Request,
    user_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Soft-delete a member. Guards: last-owner, last-admin."""
    org = _require_org(scope, storage)
    assert scope.organization_id is not None
    org_id: int = scope.organization_id
    membership = storage.get_membership(org_id, user_id)
    if membership is None or not membership.is_active:
        raise HTTPException(status_code=404, detail="Membership not found.")

    # Guard: removing the sole owner orphans the org.
    if membership.role == "owner":
        owner_count = _count_active_members_with_role(storage, org_id, "owner")
        if owner_count <= 1:
            return render(
                "admin/members.html",
                _members_page_context(
                    request,
                    current_user,
                    storage,
                    scope,
                    org,
                    flash_error=(
                        "Can't remove the sole owner. Promote another member to owner first."
                    ),
                ),
            )

    # Guard: admin removing themselves when they're the sole admin+
    # member — they'd lose console access.
    if user_id == current_user["id"]:
        higher_count = sum(
            1
            for m in storage.list_memberships_for_org(org_id)
            if m.is_active and m.user_id != user_id and has_role_at_least(m.role, "admin")
        )
        if higher_count == 0:
            return render(
                "admin/members.html",
                _members_page_context(
                    request,
                    current_user,
                    storage,
                    scope,
                    org,
                    flash_error=(
                        "Can't remove yourself while you're the only "
                        "admin/owner. Promote someone else first."
                    ),
                ),
            )

    ok = storage.soft_delete_membership(org_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Membership not found.")
    audit_record(
        storage,
        action="admin.member.remove",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="membership",
        entity_id=str(user_id),
        metadata={"role": membership.role},
    )
    return redirect_htmx(request, "/admin/members")
