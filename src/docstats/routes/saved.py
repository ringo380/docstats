"""Saved providers list and bulk export routes."""

from __future__ import annotations

import csv
import io
import json
from datetime import date

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from docstats.auth import require_user
from docstats.formatting import referral_export
from docstats.routes._common import MAPBOX_TOKEN, render
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

router = APIRouter(tags=["saved"])

_CSV_FIELDNAMES = [
    "NPI", "Name", "Entity Type", "Specialty", "Phone", "Fax",
    "Address", "City", "State", "ZIP", "Notes", "Appointment Address", "Appointment Suite",
    "Appointment Phone", "Appointment Fax", "Televisit", "Saved At",
    "OIG Excluded", "Medicare Enrolled", "Industry Payments ($)",
]


@router.get("/saved", response_class=HTMLResponse)
async def saved_list(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    return render("saved.html", {
        "request": request,
        "active_page": "saved",
        "providers": providers,
        "saved_count": len(providers),
        "mapbox_token": MAPBOX_TOKEN,
        "user": current_user,
    })


@router.get("/saved/export/csv")
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
    filename = f"referrals_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/saved/export/json")
async def export_all_json(
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    data = [p.export_fields() for p in providers]
    filename = f"referrals_{date.today().isoformat()}.json"
    return Response(
        content=json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/saved/export", response_class=HTMLResponse)
async def export_all(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    referrals = []
    for p in providers:
        result = p.to_npi_result()
        text = referral_export(result, appt_address=p.appt_address, appt_suite=p.appt_suite)
        referrals.append({
            "result": result,
            "export_text": text,
            "appt_address": p.appt_address,
            "appt_suite": p.appt_suite,
        })
    return render("export_all.html", {
        "request": request,
        "active_page": "saved",
        "referrals": referrals,
        "saved_count": len(providers),
        "user": current_user,
    })
