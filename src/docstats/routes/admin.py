"""Admin console — Phase 6.

Role-gated org administration. Every route here requires:

- An authenticated user (``require_user`` via :func:`require_admin_scope`).
- An active org membership (``scope.is_org`` True).
- A membership role at or above ``admin`` (``has_role_at_least(role, "admin")``).

Solo users and sub-admin org members get a 403. The route body never executes
for them — the dependency raises before the handler runs.

This file ships Phase 6.A (foundation + ``GET /admin`` overview) and Phase
6.B (specialty-rules editor). Subsequent slices land the other admin
surfaces (payer rules, org settings, audit viewer, members) as additional
routes on the same router.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, Response

from docstats.auth import require_user
from docstats.domain.audit import record as audit_record
from docstats.domain.orgs import Organization, has_role_at_least
from docstats.domain.reference import SpecialtyRule
from docstats.domain.rules import REQUIRED_FIELD_CHECKS
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


def _redirect_after_save(request: Request, dest: str) -> Response:
    """Return ``HX-Redirect`` (200) for htmx callers, else a 303 redirect.

    CLAUDE.md records that htmx doesn't follow 3xx redirects correctly, so
    every exit path of a mutating admin handler must go through this helper
    — ad-hoc ``Response(status_code=303, ...)`` on any branch (including
    TOCTOU fallbacks) silently breaks htmx-initiated submissions.
    """
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})


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
        audit_action = "admin.specialty_rule.create_override"
    else:
        storage.update_specialty_rule(
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
    return _redirect_after_save(request, "/admin/specialty-rules")


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
        return _redirect_after_save(request, "/admin/specialty-rules")

    deleted = storage.delete_specialty_rule(override.id)
    if not deleted:
        # Row vanished between our read and the delete; treat as already
        # reverted. Don't emit an audit event for a no-op.
        return _redirect_after_save(request, "/admin/specialty-rules")

    audit_record(
        storage,
        action="admin.specialty_rule.revert_override",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=scope.organization_id,
        entity_type="specialty_rule",
        entity_id=specialty_code,
    )
    return _redirect_after_save(request, "/admin/specialty-rules")
