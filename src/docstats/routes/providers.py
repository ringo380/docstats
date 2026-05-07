"""Provider detail, enrichment, save/unsave, notes, appointment, and single-provider export routes."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from docstats.auth import get_current_user, require_user
from docstats.client import NPPESClient, NPPESError
from docstats.exports import render_provider_request_letter
from docstats.formatting import provider_request_letter_text
from docstats.routes._common import MAPBOX_TOKEN, get_client, render, saved_count
from docstats.routes.exports import _resolve_signature_image_url
from docstats.storage import get_db_path, get_storage
from docstats.storage_base import StorageBase
from docstats.storage_files.base import StorageFileBackend
from docstats.storage_files.factory import get_file_backend
from docstats.validators import require_valid_npi

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/provider", tags=["providers"])


def _summary_addr(provider):
    """Pull the NPPES location address out of a SavedProvider for chip display."""
    if provider is None:
        return None
    try:
        return provider.to_npi_result().location_address
    except Exception:  # raw_json corruption is non-fatal — chip just hides it
        logger.exception("Failed to rehydrate NPIResult for appt summary npi=%s", provider.npi)
        return None


def _render_appt_summary(request: Request, npi: str, provider) -> Response:
    """Render the rolodex summary card for a saved provider."""
    return render(
        "_appt_summary.html",
        {
            "request": request,
            "npi": npi,
            "provider": provider,
            "addr": _summary_addr(provider),
        },
    )


def _compute_appt_selection(provider, result) -> dict:
    """Map the saved selection back onto the rendered NPPES address list.

    Returns a context dict with three keys:
      - selected_kind:    None | 'practice' | 'custom' | 'televisit'
      - selected_index:   index in result.addresses to badge as selected, or None
      - custom_unmatched: True only when kind=='custom' and no NPPES row matched

    Match is case-insensitive whitespace-trimmed on address_1 — that's the
    field set_visit_details persists when the snapshot path runs, so any row
    chosen via "Use this" round-trips. A user who instead typed a one-off
    address in the wizard will produce custom_unmatched=True so the section
    can render an explicit "Custom" card at the top.
    """
    out: dict[str, object] = {
        "selected_kind": None,
        "selected_index": None,
        "custom_unmatched": False,
    }
    if provider is None or result is None:
        return out
    vlt = provider.visit_location_type
    if vlt is None:
        return out
    if vlt == "televisit":
        out["selected_kind"] = "televisit"
        return out
    addrs = result.addresses or []
    if vlt == "practice":
        out["selected_kind"] = "practice"
        for i, a in enumerate(addrs):
            if a.address_purpose == "LOCATION":
                out["selected_index"] = i
                break
        return out
    if vlt == "custom":
        out["selected_kind"] = "custom"
        target = (provider.appt_address or "").strip().lower()
        if target:
            for i, a in enumerate(addrs):
                if (a.address_1 or "").strip().lower() == target:
                    out["selected_index"] = i
                    return out
        out["custom_unmatched"] = True
    return out


def _render_appt_addresses_section(
    request: Request, *, npi: str, result, provider, is_saved: bool
) -> Response:
    """Render the appointment-location picker section in detail.html.

    Reused by both the detail GET handler and the POST select handler — the
    select route's hx-target is the section's outer div, so this returns a
    single partial that htmx swaps in place.
    """
    selection = _compute_appt_selection(provider, result)
    return render(
        "_appt_addresses_section.html",
        {
            "request": request,
            "npi": npi,
            "result": result,
            "provider": provider,
            "is_saved": is_saved,
            **selection,
        },
    )


def _wizard_context(
    request: Request,
    npi: str,
    *,
    step: int,
    provider,
    visit_location_type: str | None = None,
    appt_address: str | None = None,
    appt_suite: str | None = None,
    appt_phone: str | None = None,
    appt_fax: str | None = None,
    error: str | None = None,
) -> dict:
    """Shared context builder for the wizard modal across step renders."""
    return {
        "request": request,
        "npi": npi,
        "step": step,
        "provider": provider,
        "addr": _summary_addr(provider),
        "mapbox_token": MAPBOX_TOKEN,
        "visit_location_type": visit_location_type,
        "appt_address": appt_address,
        "appt_suite": appt_suite,
        "appt_phone": appt_phone,
        "appt_fax": appt_fax,
        "error": error,
    }


@router.get("/{npi}/export/text")
async def export_text(
    npi: str = Depends(require_valid_npi),
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
        appt_phone = saved.appt_phone
        appt_fax = saved.appt_fax
        is_televisit = saved.is_televisit
        visit_location_type = saved.visit_location_type
    else:
        try:
            fetched = await client.async_lookup(npi)
        except NPPESError as e:
            return PlainTextResponse(content=f"Error: {e}", status_code=500)
        if fetched is None:
            return PlainTextResponse(content=f"No provider found for NPI {npi}.", status_code=404)
        result = fetched
        appt_address = None
        appt_suite = None
        appt_phone = None
        appt_fax = None
        is_televisit = False
        visit_location_type = None

    text = provider_request_letter_text(
        result,
        current_user=current_user,
        appt_address=appt_address,
        appt_suite=appt_suite,
        appt_phone=appt_phone,
        appt_fax=appt_fax,
        is_televisit=is_televisit,
        visit_location_type=visit_location_type,
    )
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": f"attachment; filename=referral-request-{npi}.txt"},
    )


@router.get("/{npi}/export", response_class=HTMLResponse)
async def export_view(
    request: Request,
    npi: str = Depends(require_valid_npi),
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
            fetched = await client.async_lookup(npi)
        except NPPESError as e:
            return HTMLResponse(content=f"<p>Error: {e}</p>", status_code=500)
        if fetched is None:
            return HTMLResponse(content=f"<p>No provider found for NPI {npi}.</p>", status_code=404)
        result = fetched

    appt_address = saved.appt_address if saved else None
    appt_suite = saved.appt_suite if saved else None
    appt_phone = saved.appt_phone if saved else None
    appt_fax = saved.appt_fax if saved else None
    is_televisit = saved.is_televisit if saved else False
    visit_location_type = saved.visit_location_type if saved else None
    export_text = provider_request_letter_text(
        result,
        current_user=current_user,
        appt_address=appt_address,
        appt_suite=appt_suite,
        appt_phone=appt_phone,
        appt_fax=appt_fax,
        is_televisit=is_televisit,
        visit_location_type=visit_location_type,
    )

    return render(
        "export.html",
        {
            "request": request,
            "active_page": "rolodex",
            "result": result,
            "export_text": export_text,
            "appt_address": appt_address,
            "appt_suite": appt_suite,
            "appt_phone": appt_phone,
            "appt_fax": appt_fax,
            "is_televisit": is_televisit,
            "visit_location_type": visit_location_type,
            "pcp_name": (current_user.get("pcp_display_name") if current_user else None),
            "saved_count": saved_count(storage, user_id),
            "user": current_user,
        },
    )


@router.get("/{npi}/export.pdf")
async def export_pdf(
    npi: str = Depends(require_valid_npi),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
    file_backend: StorageFileBackend = Depends(get_file_backend),
) -> Response:
    """WeasyPrint-rendered patient-to-PCP referral request letter."""
    user_id = current_user["id"]
    saved = storage.get_provider(npi, user_id)
    if saved:
        result = saved.to_npi_result()
        appt_address = saved.appt_address
        appt_suite = saved.appt_suite
        appt_phone = saved.appt_phone
        appt_fax = saved.appt_fax
        is_televisit = saved.is_televisit
        visit_location_type = saved.visit_location_type
    else:
        try:
            fetched = await client.async_lookup(npi)
        except NPPESError as e:
            raise HTTPException(status_code=502, detail=f"NPPES lookup failed: {e}")
        if fetched is None:
            raise HTTPException(status_code=404, detail=f"No provider found for NPI {npi}.")
        result = fetched
        appt_address = None
        appt_suite = None
        appt_phone = None
        appt_fax = None
        is_televisit = False
        visit_location_type = None

    signature_image_url = await _resolve_signature_image_url(file_backend, current_user)
    pcp_name = current_user.get("pcp_display_name")

    loop = asyncio.get_running_loop()
    try:
        pdf_bytes = await loop.run_in_executor(
            None,
            lambda: render_provider_request_letter(
                result=result,
                current_user=current_user,
                appt_address=appt_address,
                appt_suite=appt_suite,
                appt_phone=appt_phone,
                appt_fax=appt_fax,
                is_televisit=is_televisit,
                visit_location_type=visit_location_type,
                pcp_name=pcp_name,
                signature_image_url=signature_image_url,
            ),
        )
    except Exception:
        logger.exception("Provider request PDF render failed for npi=%s", npi)
        raise HTTPException(status_code=500, detail="Failed to render PDF.")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="provider-request-{npi}.pdf"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{npi}/enrichment", response_class=HTMLResponse)
async def provider_enrichment(
    request: Request,
    npi: str = Depends(require_valid_npi),
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

    return render(
        "_enrichment.html",
        {
            "request": request,
            "enrichment": data,
            "npi": npi,
        },
    )


@router.post("/{npi}/save", response_class=HTMLResponse)
async def save_provider(
    request: Request,
    npi: str = Depends(require_valid_npi),
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
        return render(
            "_save_button.html",
            {
                "request": request,
                "is_saved": True,
                "npi": npi,
                "btn_target": btn_target,
            },
        )

    try:
        result = await client.async_lookup(npi)
    except NPPESError:
        result = None

    if result:
        storage.save_provider(result, user_id)
        # Re-read the persisted row — save_provider returns a freshly built
        # SavedProvider that doesn't reflect ON CONFLICT-preserved columns
        # like visit_location_type. The route already short-circuits on
        # re-saves at the top, so this lookup is only here to confirm the
        # write landed and to read back the canonical state.
        provider = storage.get_provider(npi, user_id)
        button = render(
            "_save_button.html",
            {
                "request": request,
                "is_saved": True,
                "npi": npi,
                "btn_target": btn_target,
            },
        )
        # On first-save (no visit details yet), open the wizard automatically
        # via an OOB swap into #modal-root. Re-saving an existing row shouldn't
        # interrupt the user, so keep the wizard closed when details exist.
        button_html = button.body.decode("utf-8")  # type: ignore[union-attr]
        if provider is not None and provider.visit_location_type is None:
            modal = render(
                "_appt_wizard.html",
                _wizard_context(request, npi, step=1, provider=provider),
            )
            modal_html = modal.body.decode("utf-8")  # type: ignore[union-attr]
            # OOB swap: wrap the modal in a #modal-root replacement so the
            # base.html container picks it up no matter where Save was clicked.
            oob = f'<div id="modal-root" hx-swap-oob="true">{modal_html}</div>'
            return HTMLResponse(content=button_html + oob)
        return HTMLResponse(content=button_html)

    return HTMLResponse(
        content='<span style="color: #c62828;">Could not look up this provider. Try again.</span>'
    )


@router.delete("/{npi}/save", response_class=HTMLResponse)
async def remove_provider(
    request: Request,
    npi: str = Depends(require_valid_npi),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.delete_provider(npi, user_id)

    hx_target = request.headers.get("hx-target", "")
    if hx_target.startswith("#saved-row-"):
        return HTMLResponse(content="")

    btn_target = hx_target.lstrip("#") if hx_target else "save-btn"
    return render(
        "_save_button.html",
        {
            "request": request,
            "is_saved": False,
            "npi": npi,
            "btn_target": btn_target,
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Appointment-address wizard (replaces the prior set_appt_* /
# clear_appt_address / toggle_televisit / update_appt_contact routes).
# Single mutation path, three steps; each step posts back to the same
# endpoint with a hidden ``step`` field.
# ─────────────────────────────────────────────────────────────────────


_VALID_VISIT_TYPES = {"practice", "televisit", "custom"}


@router.get("/{npi}/appt-wizard", response_class=HTMLResponse)
async def appt_wizard_open(
    request: Request,
    npi: str = Depends(require_valid_npi),
    start: str = "",
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """Open the wizard at step 1 with current values prefilled.

    Triggered both by the search-page Save flow (via OOB swap from the save
    response) and by the Edit pencil on the rolodex summary card.

    ``?start=address`` jumps straight to step 2 with visit_location_type
    pre-set to 'custom' — used by the "+ Add a different address" tile in
    the detail-page address picker so the user skips the Where radio.
    """
    user_id = current_user["id"]
    provider = storage.get_provider(npi, user_id)
    if provider is None:
        return HTMLResponse(
            '<div class="modal-backdrop"><div class="modal-card">'
            '<p class="wizard-error">Save this provider first.</p>'
            '<button class="btn btn-secondary" hx-delete="/provider/'
            + npi
            + '/appt-wizard" hx-target="#modal-root" hx-swap="innerHTML">Close</button>'
            "</div></div>"
        )
    if start == "address":
        # Direct-to-step-2 entry: pre-select 'custom' so the wizard's existing
        # carry-through of visit_location_type lands the user on the address
        # input. Don't prefill appt_address from the saved row — picking "add
        # a different address" implies the user wants to enter a new one.
        return render(
            "_appt_wizard.html",
            _wizard_context(
                request,
                npi,
                step=2,
                provider=provider,
                visit_location_type="custom",
                appt_phone=provider.appt_phone,
                appt_fax=provider.appt_fax,
            ),
        )
    return render(
        "_appt_wizard.html",
        _wizard_context(
            request,
            npi,
            step=1,
            provider=provider,
            visit_location_type=provider.visit_location_type,
            appt_address=provider.appt_address,
            appt_suite=provider.appt_suite,
            appt_phone=provider.appt_phone,
            appt_fax=provider.appt_fax,
        ),
    )


@router.delete("/{npi}/appt-wizard", response_class=HTMLResponse)
async def appt_wizard_close(
    npi: str = Depends(require_valid_npi),
    current_user: dict = Depends(require_user),
):
    """Cancel button — empty out #modal-root."""
    return HTMLResponse(content="")


@router.post("/{npi}/appt-wizard", response_class=HTMLResponse)
async def appt_wizard_submit(
    request: Request,
    npi: str = Depends(require_valid_npi),
    step: str = Form("1", max_length=20),
    visit_location_type: str = Form("", max_length=20),
    appt_address: str = Form("", max_length=300),
    appt_suite: str = Form("", max_length=100),
    appt_phone: str = Form("", max_length=40),
    appt_fax: str = Form("", max_length=40),
    appt_phone_suggestion: str = Form("", max_length=40),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """Drive a single transition through the 3-step wizard.

    The hidden ``step`` field tells us which step's form was submitted.
    Special values: ``back``/``back-to-1`` walk backwards without writing.
    """
    user_id = current_user["id"]
    provider = storage.get_provider(npi, user_id)
    if provider is None:
        return HTMLResponse(content="")

    # Carry-through normalization
    vlt = visit_location_type.strip().lower() or None
    appt_address = appt_address.strip()
    appt_suite = appt_suite.strip()
    appt_phone = appt_phone.strip()
    appt_fax = appt_fax.strip()
    # Mapbox POI suggestions seed the office phone if the user hasn't set one.
    if not appt_phone and appt_phone_suggestion.strip():
        appt_phone = appt_phone_suggestion.strip()

    def _open(step_num: int, *, error: str | None = None) -> Response:
        return render(
            "_appt_wizard.html",
            _wizard_context(
                request,
                npi,
                step=step_num,
                provider=provider,
                visit_location_type=vlt,
                appt_address=appt_address or None,
                appt_suite=appt_suite or None,
                appt_phone=appt_phone or None,
                appt_fax=appt_fax or None,
                error=error,
            ),
        )

    # Back navigation — no writes.
    if step == "back-to-1":
        return _open(1)
    if step == "back":
        # From step 3 → step 2 only when the user is on the custom branch;
        # otherwise step 1 (telehealth/practice never visited step 2).
        return _open(2 if vlt == "custom" else 1)

    if step == "1":
        if vlt not in _VALID_VISIT_TYPES:
            return _open(1, error="Pick how you visit this provider.")
        # custom needs an address — go to step 2; everything else jumps to step 3.
        return _open(2 if vlt == "custom" else 3)

    if step == "2":
        if vlt != "custom":
            # Defensive: shouldn't reach step 2 unless custom; bounce back.
            return _open(1, error="Pick how you visit this provider.")
        if not appt_address:
            return _open(2, error="Enter an address or pick a suggestion.")
        return _open(3)

    if step == "3":
        if vlt not in _VALID_VISIT_TYPES:
            return _open(1, error="Pick how you visit this provider.")
        if vlt == "custom" and not appt_address:
            return _open(2, error="Enter an address or pick a suggestion.")

        # Practice / televisit don't store a custom address — clear them so
        # toggling between visit types doesn't leave stale data behind.
        write_address = appt_address or None if vlt == "custom" else None
        write_suite = appt_suite or None if vlt == "custom" else None
        storage.set_visit_details(
            npi,
            user_id,
            visit_location_type=vlt,
            appt_address=write_address,
            appt_suite=write_suite,
            appt_phone=appt_phone or None,
            appt_fax=appt_fax or None,
        )

        # Re-fetch so OOB swaps reflect the canonical row.
        refreshed = storage.get_provider(npi, user_id)
        summary = render(
            "_appt_summary.html",
            {
                "request": request,
                "npi": npi,
                "provider": refreshed,
                "addr": _summary_addr(refreshed),
            },
        )
        # Wizard form has hx-target="#modal-root" + hx-swap="innerHTML", so
        # the primary response wipes the modal. The summary card refresh
        # rides along OOB; the target #appt-{npi} only exists on the rolodex
        # page, so on /search and the detail page the OOB swap is a silent
        # no-op there (intended).
        summary_html = summary.body.decode("utf-8")  # type: ignore[union-attr]
        oob_summary = summary_html.replace(
            f'id="appt-{npi}"', f'id="appt-{npi}" hx-swap-oob="true"', 1
        )
        # Also OOB-swap the new addresses section, which lives on the detail
        # page only. Silent no-op on rolodex/search where #appt-addresses-
        # section isn't in the DOM.
        oob_section = ""
        try:
            result = refreshed.to_npi_result() if refreshed else None
        except Exception:  # raw_json corruption — section just doesn't refresh
            result = None
        if result is not None and refreshed is not None:
            section = _render_appt_addresses_section(
                request, npi=npi, result=result, provider=refreshed, is_saved=True
            )
            section_html = section.body.decode("utf-8")  # type: ignore[union-attr]
            oob_section = section_html.replace(
                'id="appt-addresses-section"',
                'id="appt-addresses-section" hx-swap-oob="true"',
                1,
            )
        return HTMLResponse(content="" + oob_summary + oob_section)

    return _open(1, error="Unexpected wizard state. Start over.")


@router.post("/{npi}/appt-location/select", response_class=HTMLResponse)
async def appt_location_select(
    request: Request,
    npi: str = Depends(require_valid_npi),
    kind: str = Form(..., max_length=20),
    nppes_index: str = Form("", max_length=10),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """One-click selection of an appointment location from the address picker.

    Three pathways:
      - kind='practice'           — selects the LOCATION-purpose NPPES row.
        appt_* columns set NULL (referral letters render the practice
        address from NPPES at use-time).
      - kind='custom_from_nppes'  — snapshots a chosen non-LOCATION NPPES
        row (typically MAILING) into appt_address/appt_suite/appt_phone/
        appt_fax with visit_location_type='custom'. Captures the row at
        select-time so PDFs render the snapshot, not whatever NPPES returns
        later. Requires nppes_index.
      - kind='televisit'          — sets visit_location_type='televisit',
        all appt_* NULL.

    Returns the rerendered _appt_addresses_section partial; the form's
    hx-target swaps it in place.
    """
    user_id = current_user["id"]
    provider = storage.get_provider(npi, user_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Save this provider first")

    if kind not in {"practice", "custom_from_nppes", "televisit"}:
        raise HTTPException(status_code=400, detail="Invalid kind")

    try:
        result = provider.to_npi_result()
    except Exception:
        logger.exception("Failed to rehydrate NPIResult npi=%s", npi)
        raise HTTPException(status_code=500, detail="Provider data is corrupt") from None

    if kind == "televisit":
        storage.set_visit_details(npi, user_id, visit_location_type="televisit")
    elif kind == "practice":
        storage.set_visit_details(npi, user_id, visit_location_type="practice")
    else:  # custom_from_nppes
        try:
            idx = int(nppes_index)
        except ValueError:
            raise HTTPException(status_code=400, detail="nppes_index required") from None
        addrs = result.addresses or []
        if idx < 0 or idx >= len(addrs):
            raise HTTPException(status_code=400, detail="nppes_index out of range")
        addr = addrs[idx]
        storage.set_visit_details(
            npi,
            user_id,
            visit_location_type="custom",
            appt_address=addr.address_1 or None,
            appt_suite=addr.address_2 or None,
            appt_phone=addr.formatted_phone,
            appt_fax=addr.formatted_fax,
        )

    refreshed = storage.get_provider(npi, user_id)
    return _render_appt_addresses_section(
        request, npi=npi, result=result, provider=refreshed, is_saved=True
    )


@router.put("/{npi}/notes", response_class=HTMLResponse)
async def update_notes(
    request: Request,
    npi: str = Depends(require_valid_npi),
    notes: str = Form("", max_length=2000),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    text = notes.strip() or None
    storage.update_notes(npi, text, user_id)
    return render(
        "_notes.html",
        {
            "request": request,
            "npi": npi,
            "saved_notes": text,
            "is_saved": True,
        },
    )


@router.get("/{npi}", response_class=HTMLResponse)
async def provider_detail(
    request: Request,
    npi: str = Depends(require_valid_npi),
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
            fetched = await client.async_lookup(npi)
        except NPPESError as e:
            return render(
                "detail.html",
                {
                    "request": request,
                    "active_page": "search",
                    "result": None,
                    "error": str(e),
                    "is_saved": False,
                    "saved_notes": None,
                    "saved_count": saved_count(storage, user_id),
                    "user": current_user,
                },
            )
        if fetched is None:
            return HTMLResponse(
                content=f"<main class='container'><p>No provider found for NPI {npi}.</p>"
                f"<a href='/'>Back to Search</a></main>",
                status_code=404,
            )
        result = fetched

    selection = _compute_appt_selection(saved, result)
    return render(
        "detail.html",
        {
            "request": request,
            "active_page": "search",
            "result": result,
            "is_saved": saved is not None,
            "npi": npi,
            "saved_notes": saved_notes,
            "provider": saved,
            "saved_count": saved_count(storage, user_id),
            "user": current_user,
            **selection,
        },
    )
