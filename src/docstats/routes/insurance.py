"""Patient-facing insurance plan management + family sharing (#159).

Adults can enter their own insurance plans under ``/profile/insurance`` and
optionally mark a plan as ``shared_with_family``. Family members (linked via
active ``family_links``) then see the plan in their referral wizard as a
read-only option; selecting it clones the plan into the picker's scope so the
resulting referral has a clean scope-owned ``payer_plan_id`` reference.

Org-scoped insurance plans stay back-office only — sharing is a
patient-account concept.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from docstats.auth import require_user
from docstats.domain import audit
from docstats.domain.reference import PLAN_TYPE_VALUES
from docstats.routes._common import render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase


router = APIRouter()


def _require_patient(current_user: dict) -> None:
    if current_user.get("account_type") != "patient":
        raise HTTPException(status_code=403, detail="Patient account required.")


def _solo_scope(current_user: dict) -> Scope:
    return Scope(user_id=current_user["id"])


def _ctx(
    request: Request,
    current_user: dict,
    storage: StorageBase,
    *,
    errors: list[str] | None = None,
    values: dict | None = None,
) -> dict:
    user_id = current_user["id"]
    scope = _solo_scope(current_user)
    plans = storage.list_insurance_plans(scope)
    shared = storage.list_shared_family_plans(user_id)
    # Hydrate holder name for shared plans.
    holder_names: dict[int, str] = {}
    for plan in shared:
        holder_id = plan.scope_user_id
        if holder_id is None or holder_id in holder_names:
            continue
        holder = storage.get_user_by_id(holder_id) or {}
        name = (
            f"{holder.get('first_name', '')} {holder.get('last_name', '')}".strip()
            or holder.get("email", "")
            or "Family member"
        )
        holder_names[holder_id] = name
    return {
        "request": request,
        "active_page": "profile",
        "user": current_user,
        "saved_count": saved_count(storage, user_id),
        "plans": plans,
        "shared_plans": shared,
        "holder_names": holder_names,
        "plan_type_values": PLAN_TYPE_VALUES,
        "errors": errors,
        "values": values or {},
    }


@router.get("/profile/insurance", response_class=HTMLResponse)
async def insurance_index(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    _require_patient(current_user)
    return render("insurance_plans.html", _ctx(request, current_user, storage))


@router.post("/profile/insurance", response_class=HTMLResponse)
async def insurance_create(
    request: Request,
    payer_name: str = Form(..., max_length=200),
    plan_name: str | None = Form(None, max_length=200),
    plan_type: str = Form("other", max_length=32),
    notes: str | None = Form(None, max_length=1000),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    _require_patient(current_user)
    scope = _solo_scope(current_user)
    payer_clean = payer_name.strip()
    plan_clean = (plan_name or "").strip() or None
    notes_clean = (notes or "").strip() or None
    errors: list[str] = []
    if not payer_clean:
        errors.append("Payer name is required.")
    if "|" in payer_clean:
        errors.append("Payer name must not contain '|'.")
    if plan_type not in PLAN_TYPE_VALUES:
        errors.append("Invalid plan type.")
    if errors:
        return render(
            "insurance_plans.html",
            _ctx(
                request,
                current_user,
                storage,
                errors=errors,
                values={
                    "payer_name": payer_clean,
                    "plan_name": plan_clean,
                    "plan_type": plan_type,
                    "notes": notes_clean,
                },
            ),
        )
    plan = storage.create_insurance_plan(
        scope,
        payer_name=payer_clean,
        plan_name=plan_clean,
        plan_type=plan_type,
        notes=notes_clean,
    )
    audit.record(
        storage,
        action="insurance_plan.created",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=current_user["id"],
        entity_type="insurance_plan",
        entity_id=str(plan.id),
    )
    return RedirectResponse("/profile/insurance?created=1", status_code=303)


@router.post("/profile/insurance/{plan_id}/share", response_class=HTMLResponse)
async def insurance_set_share(
    plan_id: int,
    request: Request,
    shared: str = Form("0", max_length=1),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    _require_patient(current_user)
    scope = _solo_scope(current_user)
    want_shared = shared == "1"
    plan = storage.set_insurance_plan_share(scope, plan_id, shared=want_shared)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    audit.record(
        storage,
        action="insurance_plan.shared" if want_shared else "insurance_plan.unshared",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=current_user["id"],
        entity_type="insurance_plan",
        entity_id=str(plan.id),
    )
    if request.headers.get("HX-Request"):
        return Response(
            status_code=200,
            headers={"HX-Redirect": "/profile/insurance"},
        )
    return RedirectResponse("/profile/insurance", status_code=303)


@router.post("/profile/insurance/{plan_id}/delete", response_class=HTMLResponse)
async def insurance_delete(
    plan_id: int,
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    _require_patient(current_user)
    scope = _solo_scope(current_user)
    deleted = storage.soft_delete_insurance_plan(scope, plan_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Plan not found.")
    audit.record(
        storage,
        action="insurance_plan.deleted",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=current_user["id"],
        entity_type="insurance_plan",
        entity_id=str(plan_id),
    )
    return RedirectResponse("/profile/insurance", status_code=303)
