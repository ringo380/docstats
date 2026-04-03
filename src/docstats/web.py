"""FastAPI web application for docstats."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from docstats.cache import ResponseCache
from docstats.client import NPPESClient, NPPESError
from docstats.parse import build_interpretations, parse_query
from docstats.formatting import referral_export
from docstats.normalize import format_name
from docstats.scoring import SearchQuery, rank_results
from docstats.storage import Storage, get_db_path
from docstats.taxonomies import TAXONOMY_DESCRIPTIONS

logger = logging.getLogger(__name__)

app = FastAPI(title="docstats", description="NPI Registry lookup for HMO referrals")

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _render(name: str, context: dict) -> Response:
    """Render a template, compatible with Starlette 0.50+."""
    request = context["request"]
    return templates.TemplateResponse(request, name, context)

# --- Pre-launch protections ---

BASIC_AUTH_USER = os.environ.get("DOCSTATS_AUTH_USER", "")
BASIC_AUTH_PASS = os.environ.get("DOCSTATS_AUTH_PASS", "")
MAPBOX_TOKEN = os.environ.get("MAPBOX_PUBLIC_TOKEN", "")


class PreLaunchMiddleware(BaseHTTPMiddleware):
    """Block search engines and optionally require basic auth."""

    async def dispatch(self, request: Request, call_next):
        # Basic auth gate (only when credentials are configured)
        if BASIC_AUTH_USER and BASIC_AUTH_PASS:
            import base64

            auth = request.headers.get("authorization", "")
            if not auth.startswith("Basic "):
                return StarletteResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="docstats"'},
                )
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
                user, passwd = decoded.split(":", 1)
            except Exception:
                return StarletteResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="docstats"'},
                )
            if not (secrets.compare_digest(user, BASIC_AUTH_USER)
                    and secrets.compare_digest(passwd, BASIC_AUTH_PASS)):
                return StarletteResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="docstats"'},
                )

        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response


app.add_middleware(PreLaunchMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return HTMLResponse(content="Internal Server Error", status_code=500)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    """Block all crawlers while in development."""
    return "User-agent: *\nDisallow: /\n"


# US state codes for the search form dropdown
US_STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"), ("DC", "District of Columbia"),
]

# --- Dependency injection ---

_storage: Storage | None = None
_client: NPPESClient | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage


def get_client() -> NPPESClient:
    global _client
    if _client is None:
        db_path = get_db_path()
        cache = ResponseCache(db_path)
        _client = NPPESClient(cache=cache)
    return _client


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page with search form."""
    return _render("index.html", {
        "request": request,
        "active_page": "search",
        "states": US_STATES,
        "q": {},
        "initial_results": False,
        "mapbox_token": MAPBOX_TOKEN,
    })


@app.get("/search", response_class=HTMLResponse)
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
    limit: int = Query(10, alias="limit"),
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Search providers -- returns partial HTML for htmx."""
    query = query.strip()
    name = name.strip()
    first = first.strip()
    middle = middle.strip()
    org = org.strip()
    specialty = specialty.strip()
    city = city.strip()
    state = state.strip()
    zip = zip.strip()

    def _error(msg: str):
        return _render("_results.html", {
            "request": request,
            "error": msg,
            "results": None,
            "result_count": 0,
            "interp_desc": None,
        })

    response = None
    interp_desc: str | None = None

    if query:
        # Smart search bar path: try interpretations in sequence
        parsed = parse_query(query)
        interpretations = build_interpretations(parsed)
        if not interpretations:
            return _error("Please enter a provider name or specialty to search.")

        last_error: Exception | None = None
        for interp in interpretations:
            try:
                result = client.search(**interp, limit=limit)
                if result.result_count > 0:
                    response = result
                    # Build human-readable "Searched as:" description
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
            storage.log_search({"query": query}, 0)
            return _render("_results.html", {
                "request": request,
                "error": None,
                "results": [],
                "result_count": 0,
                "interp_desc": None,
            })

        log_params: dict[str, str] = {"query": query}
        if interp_desc:
            log_params["_interp"] = interp_desc
        storage.log_search(log_params, response.result_count)

    else:
        # Legacy structured-field path (history re-run, old form)
        if not any([name, first, org, specialty, city, zip]):
            return _error("Please fill in at least one search field.")
        if (name or first) and org:
            return _error(
                "Cannot search by individual name and organization name at the same time."
            )
        try:
            response = client.search(
                last_name=name or None,
                first_name=first or None,
                organization_name=org or None,
                taxonomy_description=specialty or None,
                city=city or None,
                state=state or None,
                postal_code=zip or None,
                enumeration_type=type or None,
                limit=limit,
            )
        except NPPESError as e:
            return _error(str(e))

        params: dict[str, str] = {}
        for k, v in [("name", name), ("first", first), ("org", org),
                     ("specialty", specialty), ("state", state),
                     ("city", city), ("zip", zip), ("type", type)]:
            if v:
                params[k] = v
        storage.log_search(params, response.result_count)

    query_obj = SearchQuery(
        last_name=name or None,
        first_name=first or None,
        middle_name=middle or None,
        organization_name=org or None,
        specialty=specialty or None,
        city=city or None,
        state=state or None,
        postal_code=zip or None,
    )
    ranked = rank_results(response.results, query_obj)

    return _render("_results.html", {
        "request": request,
        "results": ranked,
        "result_count": response.result_count,
        "error": None,
        "interp_desc": interp_desc,
    })


@app.get("/api/zip/{code}")
async def zip_lookup(
    code: str,
    storage: Storage = Depends(get_storage),
):
    """Return city/state for a ZIP code (used by frontend autofill)."""
    result = storage.lookup_zip(code)
    if result:
        return JSONResponse({"city": result["city"], "state": result["state"]})
    return JSONResponse({"city": None, "state": None})


_SUGGEST_FIELDS = {"last_name", "first_name", "organization_name"}


@app.get("/api/suggest/names", response_class=HTMLResponse)
async def suggest_names(
    request: Request,
    q: str = Query(""),
    field: str = Query("last_name"),
    client: NPPESClient = Depends(get_client),
):
    """Return name suggestions as HTML partial for typeahead."""
    q = q.strip()
    if len(q) < 2 or field not in _SUGGEST_FIELDS:
        return HTMLResponse("")

    try:
        response = client.search(**{field: q}, limit=50)
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
            # Pre-fill both name fields when user selects a person
            extra = {
                "name": format_name(basic.last_name),
                "first": format_name(basic.first_name),
            }
        else:
            continue

        # NPPES matches against former/other names too — only show
        # suggestions where the current name matches the typed prefix
        if not value.lower().startswith(q_lower):
            continue

        # Deduplicate by full display name (not just the field value)
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

    return _render("_suggestions.html", {
        "request": request,
        "suggestions": suggestions,
    })


@app.get("/api/taxonomies")
async def taxonomy_list():
    """Return full taxonomy description list for client-side specialty autocomplete."""
    return JSONResponse(
        content=TAXONOMY_DESCRIPTIONS,
        headers={"Cache-Control": "max-age=86400"},
    )


@app.get("/provider/{npi}", response_class=HTMLResponse)
async def provider_detail(
    request: Request,
    npi: str,
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Show full provider detail."""
    saved = storage.get_provider(npi)
    saved_notes = None

    if saved:
        result = saved.to_npi_result()
        saved_notes = saved.notes
    else:
        try:
            result = client.lookup(npi)
        except NPPESError as e:
            return _render("detail.html", {
                "request": request,
                "active_page": "search",
                "result": None,
                "error": str(e),
                "is_saved": False,
                "saved_notes": None,
            })
        if result is None:
            return HTMLResponse(
                content=f"<main class='container'><p>No provider found for NPI {npi}.</p>"
                        f"<a href='/'>Back to Search</a></main>",
                status_code=404,
            )

    return _render("detail.html", {
        "request": request,
        "active_page": "search",
        "result": result,
        "is_saved": saved is not None,
        "npi": npi,
        "saved_notes": saved_notes,
    })


@app.post("/provider/{npi}/save", response_class=HTMLResponse)
async def save_provider(
    request: Request,
    npi: str,
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Save a provider -- returns button partial for htmx swap."""
    saved = storage.get_provider(npi)
    if saved:
        return _render("_save_button.html", {
            "request": request,
            "is_saved": True,
            "npi": npi,
        })

    try:
        result = client.lookup(npi)
    except NPPESError:
        result = None

    if result:
        storage.save_provider(result)
        return _render("_save_button.html", {
            "request": request,
            "is_saved": True,
            "npi": npi,
        })

    # Lookup failed -- don't claim it was saved
    return HTMLResponse(
        content='<span style="color: #c62828;">Could not look up this provider. Try again.</span>'
    )


@app.delete("/provider/{npi}/save", response_class=HTMLResponse)
async def remove_provider(
    request: Request,
    npi: str,
    storage: Storage = Depends(get_storage),
):
    """Remove a saved provider -- returns button partial for htmx swap."""
    storage.delete_provider(npi)

    hx_target = request.headers.get("hx-target", "")
    if hx_target.startswith("#saved-row-"):
        return HTMLResponse(content="")

    return _render("_save_button.html", {
        "request": request,
        "is_saved": False,
        "npi": npi,
    })


@app.get("/saved", response_class=HTMLResponse)
async def saved_list(
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """List saved providers."""
    providers = storage.list_providers()
    return _render("saved.html", {
        "request": request,
        "active_page": "saved",
        "providers": providers,
    })


@app.get("/provider/{npi}/export", response_class=HTMLResponse)
async def export_view(
    request: Request,
    npi: str,
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Show referral export page."""
    saved = storage.get_provider(npi)
    if saved:
        result = saved.to_npi_result()
    else:
        try:
            result = client.lookup(npi)
        except NPPESError as e:
            return HTMLResponse(content=f"<p>Error: {e}</p>", status_code=500)
        if result is None:
            return HTMLResponse(content=f"<p>No provider found for NPI {npi}.</p>", status_code=404)

    export_text = referral_export(result)

    return _render("export.html", {
        "request": request,
        "active_page": "search",
        "result": result,
        "export_text": export_text,
    })


@app.get("/provider/{npi}/export/text")
async def export_text(
    npi: str,
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Download referral summary as plain text."""
    saved = storage.get_provider(npi)
    if saved:
        result = saved.to_npi_result()
    else:
        try:
            result = client.lookup(npi)
        except NPPESError as e:
            return PlainTextResponse(content=f"Error: {e}", status_code=500)
        if result is None:
            return PlainTextResponse(content=f"No provider found for NPI {npi}.", status_code=404)

    text = referral_export(result)
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": f"attachment; filename=referral_{npi}.txt"},
    )


@app.post("/provider/{npi}/appt-address", response_class=HTMLResponse)
async def set_appt_address(
    request: Request,
    npi: str,
    address: str = Form(""),
    storage: Storage = Depends(get_storage),
):
    """Save appointment address for a provider — returns address chip partial."""
    address = address.strip()
    if address:
        storage.set_appt_address(npi, address)
    provider = storage.get_provider(npi)
    return _render("_appt_address.html", {
        "request": request,
        "npi": npi,
        "appt_address": provider.appt_address if provider else None,
        "mapbox_token": MAPBOX_TOKEN,
    })


@app.delete("/provider/{npi}/appt-address", response_class=HTMLResponse)
async def clear_appt_address(
    request: Request,
    npi: str,
    storage: Storage = Depends(get_storage),
):
    """Clear appointment address for a provider — returns empty input partial."""
    storage.clear_appt_address(npi)
    return _render("_appt_address.html", {
        "request": request,
        "npi": npi,
        "appt_address": None,
        "mapbox_token": MAPBOX_TOKEN,
    })


@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    limit: int = Query(50),
    storage: Storage = Depends(get_storage),
):
    """Show search history."""
    entries = storage.get_history(limit=limit)
    return _render("history.html", {
        "request": request,
        "active_page": "history",
        "entries": entries,
    })
