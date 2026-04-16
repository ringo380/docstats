"""Lightweight API endpoints: ZIP lookup, name suggestions, taxonomy list."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from docstats.client import NPPESClient, NPPESError
from docstats.normalize import format_name
from docstats.routes._common import get_client, render
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.taxonomies import TAXONOMY_DESCRIPTIONS

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/zip/{code}")
async def zip_lookup(
    code: str = Path(..., min_length=3, max_length=10),
    storage: StorageBase = Depends(get_storage),
):
    result = storage.lookup_zip(code)
    if result:
        return JSONResponse({"city": result["city"], "state": result["state"]})
    return JSONResponse({"city": None, "state": None})


_SUGGEST_FIELDS = {"last_name", "first_name", "organization_name"}


@router.get("/suggest/names", response_class=HTMLResponse)
async def suggest_names(
    request: Request,
    q: str = Query("", max_length=200),
    field: str = Query("last_name", max_length=32),
    client: NPPESClient = Depends(get_client),
):
    q = q.strip()
    if len(q) < 2 or field not in _SUGGEST_FIELDS:
        return HTMLResponse("")

    try:
        response = await client.async_search(**{field: q}, limit=50)
    except NPPESError:
        return HTMLResponse("")

    seen: set[str] = set()
    suggestions: list[dict[str, str | dict[str, str]]] = []
    q_lower = q.lower()
    for r in response.results:
        basic = r.parsed_basic()
        if field == "organization_name" and r.is_organization:
            value = format_name(basic.organization_name)
            extra = {}
        elif field in ("last_name", "first_name") and r.is_individual:
            value = format_name(getattr(basic, field))
            extra = {
                "name": format_name(basic.last_name),
                "first": format_name(basic.first_name),
            }
        else:
            continue

        if not value.lower().startswith(q_lower):
            continue

        display = r.display_name
        if display in seen:
            continue
        seen.add(display)

        sublabel = r.primary_specialty
        addr = r.location_address
        if addr:
            sublabel += f" — {format_name(addr.city)}, {addr.state}"

        suggestions.append({
            "value": value,
            "label": display,
            "sublabel": sublabel,
            "extra": extra,
        })
        if len(suggestions) >= 8:
            break

    return render("_suggestions.html", {"request": request, "suggestions": suggestions})


@router.get("/taxonomies")
async def taxonomy_list():
    return JSONResponse(
        content=TAXONOMY_DESCRIPTIONS,
        headers={"Cache-Control": "max-age=86400"},
    )
