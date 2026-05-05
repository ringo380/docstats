"""Rolodex routes — personal provider list + bulk export (Phase 2.E rename).

Originally shipped as "My Referrals" at ``/saved`` (Phase 0, the pre-referral-
platform CRM). Phase 2.E moved the surface to ``/rolodex`` to free up the
"Referrals" name for the referral workspace (``/referrals``). The old
``/saved`` paths 301-redirect to ``/rolodex`` — see ``web.py`` for the
redirect router. External bookmarks / export CSVs linked by users therefore
still resolve.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from docstats.auth import require_user
from docstats.exports import concat_pdfs, render_provider_request_letter
from docstats.formatting import provider_request_letter_text
from docstats.routes._common import MAPBOX_TOKEN, render
from docstats.routes.exports import _resolve_signature_image_url
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.storage_files.base import StorageFileBackend
from docstats.storage_files.factory import get_file_backend

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rolodex", tags=["rolodex"])

_CSV_FIELDNAMES = [
    "NPI",
    "Name",
    "Entity Type",
    "Specialty",
    "Phone",
    "Fax",
    "Address",
    "City",
    "State",
    "ZIP",
    "Notes",
    "Appointment Address",
    "Appointment Suite",
    "Appointment Phone",
    "Appointment Fax",
    "Televisit",
    "Saved At",
    "OIG Excluded",
    "Medicare Enrolled",
    "Industry Payments ($)",
]


@router.get("", response_class=HTMLResponse)
async def rolodex_list(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    return render(
        "saved.html",
        {
            "request": request,
            "active_page": "rolodex",
            "providers": providers,
            "saved_count": len(providers),
            "mapbox_token": MAPBOX_TOKEN,
            "user": current_user,
        },
    )


@router.get("/export/csv")
async def export_all_csv(
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDNAMES)
    writer.writeheader()
    for p in providers:
        writer.writerow(p.export_fields())
    filename = f"rolodex_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/json")
async def export_all_json(
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    data = [p.export_fields() for p in providers]
    filename = f"rolodex_{date.today().isoformat()}.json"
    return Response(
        content=json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export", response_class=HTMLResponse)
async def export_all(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    referrals = []
    pcp_name = current_user.get("pcp_display_name")
    for p in providers:
        result = p.to_npi_result()
        text = provider_request_letter_text(
            result,
            current_user=current_user,
            appt_address=p.appt_address,
            appt_suite=p.appt_suite,
            appt_phone=p.appt_phone,
            appt_fax=p.appt_fax,
            is_televisit=p.is_televisit,
        )
        referrals.append(
            {
                "result": result,
                "export_text": text,
                "appt_address": p.appt_address,
                "appt_suite": p.appt_suite,
                "appt_phone": p.appt_phone,
                "appt_fax": p.appt_fax,
                "is_televisit": p.is_televisit,
                "pcp_name": pcp_name,
            }
        )
    return render(
        "export_all.html",
        {
            "request": request,
            "active_page": "rolodex",
            "referrals": referrals,
            "saved_count": len(providers),
            "user": current_user,
            "pcp_name": pcp_name,
        },
    )


@router.get("/export.pdf")
async def export_all_pdf(
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
    file_backend: StorageFileBackend = Depends(get_file_backend),
) -> Response:
    """Concatenated PDF: one provider-request letter per saved provider."""
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    if not providers:
        raise HTTPException(status_code=404, detail="No saved providers to export.")

    signature_image_url = await _resolve_signature_image_url(file_backend, current_user)
    pcp_name = current_user.get("pcp_display_name")

    loop = asyncio.get_running_loop()

    def _render_all() -> bytes:
        parts: list[bytes] = []
        for p in providers:
            parts.append(
                render_provider_request_letter(
                    result=p.to_npi_result(),
                    current_user=current_user,
                    appt_address=p.appt_address,
                    appt_suite=p.appt_suite,
                    appt_phone=p.appt_phone,
                    appt_fax=p.appt_fax,
                    is_televisit=p.is_televisit,
                    pcp_name=pcp_name,
                    signature_image_url=signature_image_url,
                )
            )
        return concat_pdfs(parts)

    try:
        pdf_bytes = await loop.run_in_executor(None, _render_all)
    except Exception:
        logger.exception("Bulk rolodex PDF render failed for user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to render PDF.")

    filename = f"rolodex-letters-{date.today().isoformat()}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
