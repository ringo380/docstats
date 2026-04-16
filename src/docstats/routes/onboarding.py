"""Onboarding wizard routes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from docstats.auth import require_user
from docstats.routes._common import MAPBOX_TOKEN, render, saved_count
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _onboarding_step(user: dict, *, pcp_skipped: bool = False) -> int:
    """Determine which onboarding step a user should be on."""
    if not (user.get("first_name") and user.get("last_name")):
        return 1
    if not user.get("date_of_birth"):
        return 2
    if not user.get("pcp_npi") and not pcp_skipped:
        return 3
    return 4


@router.get("", response_class=HTMLResponse)
async def onboarding(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    if current_user.get("terms_accepted_at") or request.session.get("onboarding_done"):
        return RedirectResponse("/", status_code=303)
    user_id = current_user["id"]
    return render("onboarding.html", {
        "request": request,
        "active_page": None,
        "saved_count": saved_count(storage, user_id),
        "mapbox_token": MAPBOX_TOKEN,
        "user": current_user,
        "initial_step": _onboarding_step(current_user, pcp_skipped=request.session.get("pcp_skipped", False)),
        "today": date.today().isoformat(),
    })


@router.post("/save-name")
async def onboarding_save_name(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    middle_name: str = Form(""),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    fn = first_name.strip()
    ln = last_name.strip()
    mn = middle_name.strip() or None
    storage.update_user_profile(
        current_user["id"],
        first_name=fn,
        last_name=ln,
        middle_name=mn,
        display_name=f"{fn} {ln}",
    )
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "stepComplete"
    return resp


@router.post("/save-dob")
async def onboarding_save_dob(
    request: Request,
    date_of_birth: str = Form(...),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    try:
        parsed = date.fromisoformat(date_of_birth)
    except ValueError:
        return Response("Invalid date format.", status_code=200)
    if parsed > date.today():
        return Response("Date of birth cannot be in the future.", status_code=200)
    storage.update_user_profile(current_user["id"], date_of_birth=date_of_birth)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "stepComplete"
    return resp


@router.post("/select-pcp/{npi}", response_class=HTMLResponse)
async def onboarding_select_pcp(
    npi: str,
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.set_user_pcp(user_id, npi)
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "stepComplete"
    return resp


@router.get("/skip-pcp")
async def onboarding_skip_pcp(
    request: Request,
    current_user: dict = Depends(require_user),
):
    request.session["pcp_skipped"] = True
    resp = Response(status_code=200)
    resp.headers["HX-Trigger"] = "stepComplete"
    return resp


@router.post("/accept-terms")
async def onboarding_accept_terms(
    request: Request,
    terms_version: str = Form(...),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    ua = request.headers.get("user-agent", "unknown")
    storage.record_terms_acceptance(
        current_user["id"],
        terms_version=terms_version,
        ip_address=ip,
        user_agent=ua,
    )
    request.session["onboarding_done"] = True
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/"
    return resp


@router.get("/skip")
async def onboarding_skip(
    request: Request,
    current_user: dict = Depends(require_user),
):
    request.session["onboarding_done"] = True
    return RedirectResponse("/", status_code=303)
