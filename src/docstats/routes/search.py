"""Search routes: NPPES search, name suggestions, taxonomy list, ZIP lookup."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from docstats.auth import (
    ANON_SEARCH_LIMIT,
    get_anon_search_count,
    get_current_user,
    increment_anon_search_count,
)
from docstats.client import NPPESClient, NPPESError
from docstats.normalize import format_name
from docstats.parse import build_interpretations, parse_query
from docstats.routes._common import get_client, render
from docstats.scoring import SearchQuery, rank_results
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.taxonomies import TAXONOMY_DESCRIPTIONS

router = APIRouter(tags=["search"])


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    query: str = Query("", alias="query"),
    name: str = Query("", alias="name"),
    first: str = Query("", alias="first"),
    middle: str = Query("", alias="middle"),
    org: str = Query("", alias="org"),
    specialty: str = Query("", alias="specialty"),
    city: str = Query("", alias="city"),
    state: str = Query("", alias="state"),
    zip: str = Query("", alias="zip"),
    type: str = Query("", alias="type"),
    geo_state: str = Query("", alias="geo_state"),
    geo_lat: str = Query("", alias="geo_lat"),
    geo_lon: str = Query("", alias="geo_lon"),
    limit: int = Query(10, alias="limit"),
    context: str = Query("", alias="context"),
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Search providers — returns partial HTML for htmx."""
    query = query.strip()
    name = name.strip()
    first = first.strip()
    middle = middle.strip()
    org = org.strip()
    specialty = specialty.strip()
    city = city.strip()
    state = state.strip()
    zip = zip.strip()
    geo_state = geo_state.strip()
    parsed_geo_lat: float | None = None
    parsed_geo_lon: float | None = None
    try:
        if geo_lat and geo_lon:
            parsed_geo_lat = round(float(geo_lat), 2)
            parsed_geo_lon = round(float(geo_lon), 2)
    except ValueError:
        pass

    user_id = current_user["id"] if current_user else None

    def _error(msg: str):
        if context in ("onboarding", "profile"):
            return render("_pcp_results.html", {
                "request": request,
                "error": msg,
                "results": None,
                "result_count": 0,
                "interp_desc": None,
                "pcp_action_url": "/onboarding/select-pcp" if context == "onboarding" else "/profile/pcp",
            })
        return render("_results.html", {
            "request": request,
            "error": msg,
            "results": None,
            "result_count": 0,
            "interp_desc": None,
            "anon_limit_reached": False,
        })

    # Anonymous search limit
    if current_user is None:
        count = get_anon_search_count(request)
        if count >= ANON_SEARCH_LIMIT:
            return render("_results.html", {
                "request": request,
                "error": None,
                "results": None,
                "result_count": 0,
                "interp_desc": None,
                "anon_limit_reached": True,
            })

    response = None
    interp_desc: str | None = None

    if query:
        parsed = parse_query(query)
        interpretations = build_interpretations(parsed)
        if not interpretations:
            return _error("Please enter a provider name or specialty to search.")

        last_error: Exception | None = None
        interp: dict = {}
        for interp in interpretations:
            try:
                search_kwargs = dict(interp)
                if specialty:
                    search_kwargs["taxonomy_description"] = specialty
                if city:
                    search_kwargs["city"] = city
                if state:
                    search_kwargs["state"] = state
                elif geo_state:
                    search_kwargs["state"] = geo_state
                if zip:
                    search_kwargs["postal_code"] = zip
                has_location = city or state or zip
                geo_limit = limit * 5 if (parsed_geo_lat is not None and not has_location) else limit
                result = await client.async_search(**search_kwargs, limit=geo_limit)
                if result.result_count == 0 and (has_location or geo_state):
                    fallback_kwargs = dict(interp)
                    if specialty:
                        fallback_kwargs["taxonomy_description"] = specialty
                    result = await client.async_search(**fallback_kwargs, limit=geo_limit)
                if result.result_count > 0:
                    response = result
                    parts = []
                    fn = interp.get("first_name", "")
                    ln = interp.get("last_name", "")
                    org_name = interp.get("organization_name", "")
                    tax = interp.get("taxonomy_description", "")
                    if fn and ln:
                        parts.append(f"{fn} {ln}")
                    elif ln:
                        parts.append(ln)
                    elif org_name:
                        parts.append(org_name)
                    if tax:
                        parts.append(tax)
                    interp_desc = " · ".join(parts)
                    break
            except NPPESError as e:
                last_error = e

        if response is None:
            if last_error:
                return _error(str(last_error))
            storage.log_search({"query": query}, 0, user_id=user_id)
            return _error("No results found. Try broadening your search.")

        log_params: dict[str, str] = {"query": query}
        if interp_desc:
            log_params["_interp"] = interp_desc
        storage.log_search(log_params, response.result_count, user_id=user_id)

    else:
        if not any([name, first, org, specialty, city, zip]):
            return _error("Please fill in at least one search field.")
        if (name or first) and org:
            return _error(
                "Cannot search by individual name and organization name at the same time."
            )
        struct_has_location = city or state or zip
        struct_geo_limit = limit * 5 if (parsed_geo_lat is not None and not struct_has_location) else limit
        try:
            response = await client.async_search(
                last_name=name or None,
                first_name=first or None,
                organization_name=org or None,
                taxonomy_description=specialty or None,
                city=city or None,
                state=state or None,
                postal_code=zip or None,
                enumeration_type=type or None,
                limit=struct_geo_limit,
            )
        except NPPESError as e:
            return _error(str(e))

        params: dict[str, str] = {}
        for k, v in [("name", name), ("first", first), ("org", org),
                     ("specialty", specialty), ("state", state),
                     ("city", city), ("zip", zip), ("type", type)]:
            if v:
                params[k] = v
        storage.log_search(params, response.result_count, user_id=user_id)

    # Increment anonymous search counter after a successful search
    if current_user is None:
        increment_anon_search_count(request)

    if query:
        query_obj = SearchQuery(
            last_name=interp.get("last_name") or None,
            first_name=interp.get("first_name") or None,
            middle_name=parsed.middle_name or None,
            organization_name=interp.get("organization_name") or None,
            specialty=interp.get("taxonomy_description") or specialty or None,
            geo_state=geo_state or None,
            geo_lat=parsed_geo_lat,
            geo_lon=parsed_geo_lon,
        )
    else:
        query_obj = SearchQuery(
            last_name=name or None,
            first_name=first or None,
            middle_name=middle or None,
            organization_name=org or None,
            specialty=specialty or None,
            city=city or None,
            state=state or None,
            postal_code=zip or None,
            geo_state=geo_state or None,
            geo_lat=parsed_geo_lat,
            geo_lon=parsed_geo_lon,
        )
    ranked = rank_results(response.results, query_obj)[:limit]

    if context in ("onboarding", "profile"):
        pcp_action_url = "/onboarding/select-pcp" if context == "onboarding" else "/profile/pcp"
        return render("_pcp_results.html", {
            "request": request,
            "results": ranked,
            "result_count": response.result_count,
            "error": None,
            "interp_desc": interp_desc,
            "pcp_action_url": pcp_action_url,
        })

    return render("_results.html", {
        "request": request,
        "results": ranked,
        "result_count": response.result_count,
        "error": None,
        "interp_desc": interp_desc,
        "anon_limit_reached": False,
    })


@router.get("/api/zip/{code}")
async def zip_lookup(code: str, storage: StorageBase = Depends(get_storage)):
    result = storage.lookup_zip(code)
    if result:
        return JSONResponse({"city": result["city"], "state": result["state"]})
    return JSONResponse({"city": None, "state": None})


_SUGGEST_FIELDS = {"last_name", "first_name", "organization_name"}


@router.get("/api/suggest/names", response_class=HTMLResponse)
async def suggest_names(
    request: Request,
    q: str = Query(""),
    field: str = Query("last_name"),
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


@router.get("/api/taxonomies")
async def taxonomy_list():
    return JSONResponse(
        content=TAXONOMY_DESCRIPTIONS,
        headers={"Cache-Control": "max-age=86400"},
    )
