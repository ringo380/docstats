"""Search routes: NPPES search, name suggestions, taxonomy list, ZIP lookup."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from docstats.auth import (
    ANON_SEARCH_LIMIT,
    get_anon_search_count,
    get_current_user,
    increment_anon_search_count,
)
from docstats.client import NPPESClient, NPPESError
from docstats.parse import build_interpretations, parse_query
from docstats.routes._common import get_client, render
from docstats.scoring import SearchQuery, rank_results
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

router = APIRouter(tags=["search"])


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    query: str = Query("", alias="query", max_length=200),
    name: str = Query("", alias="name", max_length=100),
    first: str = Query("", alias="first", max_length=100),
    middle: str = Query("", alias="middle", max_length=100),
    org: str = Query("", alias="org", max_length=200),
    specialty: str = Query("", alias="specialty", max_length=200),
    city: str = Query("", alias="city", max_length=100),
    state: str = Query("", alias="state", max_length=2),
    zip: str = Query("", alias="zip", max_length=10),
    type: str = Query("", alias="type", max_length=10),
    geo_state: str = Query("", alias="geo_state", max_length=2),
    geo_lat: str = Query("", alias="geo_lat", max_length=20),
    geo_lon: str = Query("", alias="geo_lon", max_length=20),
    limit: int = Query(10, alias="limit", ge=1, le=100),
    context: str = Query("", alias="context", max_length=20),
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
