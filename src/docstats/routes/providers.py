"""Provider detail, enrichment, save/unsave, notes, appointment, and single-provider export routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from docstats.auth import get_current_user, require_user
from docstats.client import NPPESClient, NPPESError
from docstats.formatting import referral_export
from docstats.routes._common import MAPBOX_TOKEN, get_client, render, saved_count
from docstats.storage import get_db_path, get_storage
from docstats.storage_base import StorageBase

router = APIRouter(prefix="/provider", tags=["providers"])


def _render_appt(request: Request, npi: str, appt_address: str | None, appt_suite: str | None):
    return render("_appt_address.html", {
        "request": request, "npi": npi,
        "appt_address": appt_address, "appt_suite": appt_suite,
        "mapbox_token": MAPBOX_TOKEN,
    })


@router.get("/{npi}/export/text")
async def export_text(
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    user_id = current_user["id"] if current_user else None
    saved = storage.get_provider(npi, user_id)
    if saved:
        result = saved.to_npi_result()
        appt_address = saved.appt_address
        appt_suite = saved.appt_suite
    else:
        try:
            result = await client.async_lookup(npi)
        except NPPESError as e:
            return PlainTextResponse(content=f"Error: {e}", status_code=500)
        if result is None:
            return PlainTextResponse(content=f"No provider found for NPI {npi}.", status_code=404)
        appt_address = None
        appt_suite = None

    text = referral_export(result, appt_address=appt_address, appt_suite=appt_suite)
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": f"attachment; filename=referral_{npi}.txt"},
    )


@router.get("/{npi}/export", response_class=HTMLResponse)
async def export_view(
    request: Request,
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    user_id = current_user["id"] if current_user else None
    saved = storage.get_provider(npi, user_id)
    if saved:
        result = saved.to_npi_result()
    else:
        try:
            result = await client.async_lookup(npi)
        except NPPESError as e:
            return HTMLResponse(content=f"<p>Error: {e}</p>", status_code=500)
        if result is None:
            return HTMLResponse(content=f"<p>No provider found for NPI {npi}.</p>", status_code=404)

    appt_address = saved.appt_address if saved else None
    appt_suite = saved.appt_suite if saved else None
    export_text = referral_export(result, appt_address=appt_address, appt_suite=appt_suite)

    return render("export.html", {
        "request": request,
        "active_page": "saved",
        "result": result,
        "export_text": export_text,
        "appt_address": appt_address,
        "appt_suite": appt_suite,
        "saved_count": saved_count(storage, user_id),
        "user": current_user,
    })


@router.get("/{npi}/enrichment", response_class=HTMLResponse)
async def provider_enrichment(
    request: Request,
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
):
    """Fetch enrichment data for a provider (htmx lazy-load partial)."""
    from docstats.enrichment import EnrichmentCache, enrich_provider

    cache = EnrichmentCache(get_db_path())
    try:
        data = await enrich_provider(npi, cache)
    finally:
        cache.close()

    user_id = current_user["id"] if current_user else None
    if user_id and data.sources_checked:
        enrichment_json = data.model_dump_json()
        storage.update_enrichment(npi, enrichment_json, user_id)

    return render("_enrichment.html", {
        "request": request,
        "enrichment": data,
        "npi": npi,
    })


@router.post("/{npi}/save", response_class=HTMLResponse)
async def save_provider(
    request: Request,
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Save a provider — returns button partial for htmx swap."""
    btn_target = request.headers.get("hx-target", "#save-btn").lstrip("#")

    if current_user is None:
        return render("_auth_gate.html", {"request": request, "btn_target": btn_target})

    user_id = current_user["id"]
    saved = storage.get_provider(npi, user_id)
    if saved:
        return render("_save_button.html", {
            "request": request,
            "is_saved": True,
            "npi": npi,
            "btn_target": btn_target,
        })

    try:
        result = await client.async_lookup(npi)
    except NPPESError:
        result = None

    if result:
        storage.save_provider(result, user_id)
        return render("_save_button.html", {
            "request": request,
            "is_saved": True,
            "npi": npi,
            "btn_target": btn_target,
        })

    return HTMLResponse(
        content='<span style="color: #c62828;">Could not look up this provider. Try again.</span>'
    )


@router.delete("/{npi}/save", response_class=HTMLResponse)
async def remove_provider(
    request: Request,
    npi: str,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.delete_provider(npi, user_id)

    hx_target = request.headers.get("hx-target", "")
    if hx_target.startswith("#saved-row-"):
        return HTMLResponse(content="")

    btn_target = hx_target.lstrip("#") if hx_target else "save-btn"
    return render("_save_button.html", {
        "request": request,
        "is_saved": False,
        "npi": npi,
        "btn_target": btn_target,
    })


@router.post("/{npi}/appt-address", response_class=HTMLResponse)
async def set_appt_address(
    request: Request,
    npi: str,
    address: str = Form(""),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    address = address.strip()
    if address:
        found = storage.set_appt_address(npi, address, user_id)
        if not found:
            return HTMLResponse(
                '<span class="appt-error">Provider must be saved before adding an appointment address.</span>'
            )
    provider = storage.get_provider(npi, user_id)
    return _render_appt(request, npi, provider.appt_address if provider else None, provider.appt_suite if provider else None)


@router.put("/{npi}/appt-suite", response_class=HTMLResponse)
async def update_appt_suite(
    request: Request,
    npi: str,
    suite: str = Form(""),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    suite = suite.strip()
    storage.set_appt_suite(npi, suite or None, user_id)
    provider = storage.get_provider(npi, user_id)
    return _render_appt(request, npi, provider.appt_address if provider else None, provider.appt_suite if provider else None)


@router.delete("/{npi}/appt-address", response_class=HTMLResponse)
async def clear_appt_address(
    request: Request,
    npi: str,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.clear_appt_address(npi, user_id)
    return _render_appt(request, npi, None, None)


@router.put("/{npi}/notes", response_class=HTMLResponse)
async def update_notes(
    request: Request,
    npi: str,
    notes: str = Form(""),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    text = notes.strip() or None
    storage.update_notes(npi, text, user_id)
    return render("_notes.html", {
        "request": request,
        "npi": npi,
        "saved_notes": text,
        "is_saved": True,
    })


@router.get("/{npi}", response_class=HTMLResponse)
async def provider_detail(
    request: Request,
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    user_id = current_user["id"] if current_user else None
    saved = storage.get_provider(npi, user_id)
    saved_notes = None

    if saved:
        result = saved.to_npi_result()
        saved_notes = saved.notes
    else:
        try:
            result = await client.async_lookup(npi)
        except NPPESError as e:
            return render("detail.html", {
                "request": request,
                "active_page": "search",
                "result": None,
                "error": str(e),
                "is_saved": False,
                "saved_notes": None,
                "saved_count": saved_count(storage, user_id),
                "user": current_user,
            })
        if result is None:
            return HTMLResponse(
                content=f"<main class='container'><p>No provider found for NPI {npi}.</p>"
                        f"<a href='/'>Back to Search</a></main>",
                status_code=404,
            )

    return render("detail.html", {
        "request": request,
        "active_page": "search",
        "result": result,
        "is_saved": saved is not None,
        "npi": npi,
        "saved_notes": saved_notes,
        "saved_count": saved_count(storage, user_id),
        "user": current_user,
    })
