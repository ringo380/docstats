"""Profile page and PCP management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from docstats.auth import require_user
from docstats.client import NPPESClient, NPPESError
from docstats.routes._common import MAPBOX_TOKEN, get_client, render, saved_count
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.validators import require_valid_npi

router = APIRouter(tags=["profile"])


@router.get("/profile", response_class=HTMLResponse)
async def profile(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    user_id = current_user["id"]
    pcp_provider = None
    pcp_npi = current_user.get("pcp_npi")
    if pcp_npi:
        try:
            pcp_provider = await client.async_lookup(pcp_npi)
        except NPPESError:
            pass
    return render("profile.html", {
        "request": request,
        "active_page": "profile",
        "saved_count": saved_count(storage, user_id),
        "user": current_user,
        "pcp_provider": pcp_provider,
        "mapbox_token": MAPBOX_TOKEN,
    })


@router.post("/profile/pcp/{npi}", response_class=HTMLResponse)
async def profile_set_pcp(
    request: Request,
    npi: str = Depends(require_valid_npi),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.set_user_pcp(user_id, npi)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/profile"
    return resp


@router.delete("/profile/pcp", response_class=HTMLResponse)
async def profile_clear_pcp(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    storage.clear_user_pcp(current_user["id"])
    return render("_pcp_section.html", {
        "request": request,
        "pcp_provider": None,
        "mapbox_token": MAPBOX_TOKEN,
    })
