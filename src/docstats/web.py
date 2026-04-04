"""FastAPI web application for docstats."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from docstats.auth import (
    ANON_SEARCH_LIMIT,
    AuthRequiredException,
    get_anon_search_count,
    get_current_user,
    hash_password,
    increment_anon_search_count,
    require_user,
    verify_password,
)
from docstats.cache import ResponseCache
from docstats.client import NPPESClient, NPPESError
from docstats.formatting import referral_export
from docstats.normalize import format_name
from docstats.oauth import (
    GITHUB_ENABLED,
    github_authorize_url,
    github_exchange_code,
    github_get_emails,
    github_get_user,
    primary_github_email,
)
from docstats.parse import build_interpretations, parse_query
from docstats.scoring import SearchQuery, rank_results
from docstats.storage import Storage, get_db_path, get_storage
from docstats.taxonomies import TAXONOMY_DESCRIPTIONS

logger = logging.getLogger(__name__)

app = FastAPI(title="docstats", description="NPI Registry lookup for HMO referrals")

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
MAPBOX_TOKEN = os.environ.get("MAPBOX_PUBLIC_TOKEN", "")

# --- Session middleware (must be added before @app.middleware decorators) ---
_SESSION_SECRET = os.environ.get("SESSION_SECRET_KEY") or secrets.token_hex(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    max_age=604800,  # 7 days
    https_only=os.environ.get("RAILWAY_ENVIRONMENT") == "production",
)


# --- X-Robots-Tag (keep crawlers out) ---
@app.middleware("http")
async def add_robots_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# --- Exception handlers ---

@app.exception_handler(AuthRequiredException)
async def auth_exception_handler(request: Request, exc: AuthRequiredException):
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/auth/login"})
    return RedirectResponse("/auth/login", status_code=303)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return HTMLResponse(content="Internal Server Error", status_code=500)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    """Block all crawlers."""
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

_client: NPPESClient | None = None


def get_client() -> NPPESClient:
    global _client
    if _client is None:
        db_path = get_db_path()
        cache = ResponseCache(db_path)
        _client = NPPESClient(cache=cache)
    return _client


def _render(name: str, context: dict) -> Response:
    """Render a template, compatible with Starlette 0.50+."""
    request = context["request"]
    return templates.TemplateResponse(request, name, context)


def _saved_count(storage: Storage, user_id: int | None) -> int:
    if user_id is None:
        return 0
    return len(storage.list_providers(user_id))


# --- Auth routes ---

@app.get("/auth/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/", status_code=303)
    return _render("login.html", {
        "request": request,
        "active_page": None,
        "saved_count": 0,
        "user": None,
        "error": request.session.pop("flash_error", None),
        "github_enabled": GITHUB_ENABLED,
    })


@app.post("/auth/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    storage: Storage = Depends(get_storage),
):
    email = email.strip().lower()
    if not email or not password:
        return _render("login.html", {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": "Email and password are required.",
            "github_enabled": GITHUB_ENABLED,
        })

    user = storage.get_user_by_email(email)
    if not user or not user.get("password_hash") or not verify_password(password, user["password_hash"]):
        return _render("login.html", {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": "Invalid email or password.",
            "github_enabled": GITHUB_ENABLED,
        })

    request.session["user_id"] = user["id"]
    request.session.pop("anon_searches", None)
    storage.update_last_login(user["id"])
    return RedirectResponse("/", status_code=303)


@app.get("/auth/signup", response_class=HTMLResponse)
async def signup_page(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/", status_code=303)
    return _render("signup.html", {
        "request": request,
        "active_page": None,
        "saved_count": 0,
        "user": None,
        "error": None,
        "github_enabled": GITHUB_ENABLED,
    })


@app.post("/auth/signup", response_class=HTMLResponse)
async def signup_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    storage: Storage = Depends(get_storage),
):
    email = email.strip().lower()

    def _err(msg: str):
        return _render("signup.html", {
            "request": request,
            "active_page": None,
            "saved_count": 0,
            "user": None,
            "error": msg,
            "github_enabled": GITHUB_ENABLED,
        })

    if not email or not password:
        return _err("Email and password are required.")
    if len(password) < 8:
        return _err("Password must be at least 8 characters.")
    if password != confirm_password:
        return _err("Passwords do not match.")
    if storage.get_user_by_email(email):
        return _err("An account with that email already exists.")

    user_id = storage.create_user(email, hash_password(password))
    request.session["user_id"] = user_id
    request.session.pop("anon_searches", None)
    return RedirectResponse("/", status_code=303)


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/auth/github")
async def github_login(request: Request):
    if not GITHUB_ENABLED:
        return RedirectResponse("/auth/login", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["github_state"] = state
    return RedirectResponse(github_authorize_url(state), status_code=303)


@app.get("/auth/github/callback")
async def github_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    storage: Storage = Depends(get_storage),
):
    expected_state = request.session.pop("github_state", None)
    if not code or not state or state != expected_state:
        return RedirectResponse("/auth/login?error=oauth", status_code=303)

    try:
        async with httpx.AsyncClient() as client:
            token_data = await github_exchange_code(code, client)
            access_token = token_data.get("access_token")
            if not access_token:
                return RedirectResponse("/auth/login?error=oauth", status_code=303)

            gh_user = await github_get_user(access_token, client)
            gh_emails = await github_get_emails(access_token, client)
    except Exception:
        logger.exception("GitHub OAuth error")
        return RedirectResponse("/auth/login?error=oauth", status_code=303)

    email = primary_github_email(gh_emails) or gh_user.get("email")
    display_name = gh_user.get("name") or gh_user.get("login")
    user_id = storage.upsert_github_user(
        github_id=str(gh_user["id"]),
        github_login=gh_user["login"],
        email=email,
        display_name=display_name,
    )
    request.session["user_id"] = user_id
    request.session.pop("anon_searches", None)
    return RedirectResponse("/", status_code=303)


# --- Main routes ---

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    current_user: dict | None = Depends(get_current_user),
    storage: Storage = Depends(get_storage),
):
    user_id = current_user["id"] if current_user else None
    anon_remaining = (
        None if current_user
        else max(0, ANON_SEARCH_LIMIT - get_anon_search_count(request))
    )
    return _render("index.html", {
        "request": request,
        "active_page": "search",
        "states": US_STATES,
        "q": {},
        "initial_results": False,
        "mapbox_token": MAPBOX_TOKEN,
        "saved_count": _saved_count(storage, user_id),
        "user": current_user,
        "anon_searches_remaining": anon_remaining,
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
    geo_state: str = Query("", alias="geo_state"),
    limit: int = Query(10, alias="limit"),
    current_user: dict | None = Depends(get_current_user),
    storage: Storage = Depends(get_storage),
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

    user_id = current_user["id"] if current_user else None

    # Anonymous search limit
    if current_user is None:
        count = get_anon_search_count(request)
        if count >= ANON_SEARCH_LIMIT:
            return _render("_results.html", {
                "request": request,
                "error": None,
                "results": None,
                "result_count": 0,
                "interp_desc": None,
                "anon_limit_reached": True,
            })

    def _error(msg: str):
        return _render("_results.html", {
            "request": request,
            "error": msg,
            "results": None,
            "result_count": 0,
            "interp_desc": None,
            "anon_limit_reached": False,
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
                result = client.search(**interp, limit=limit)
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
            return _render("_results.html", {
                "request": request,
                "error": None,
                "results": [],
                "result_count": 0,
                "interp_desc": None,
                "anon_limit_reached": False,
            })

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
            specialty=interp.get("taxonomy_description") or None,
            geo_state=geo_state or None,
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
        )
    ranked = rank_results(response.results, query_obj)

    return _render("_results.html", {
        "request": request,
        "results": ranked,
        "result_count": response.result_count,
        "error": None,
        "interp_desc": interp_desc,
        "anon_limit_reached": False,
    })


@app.get("/api/zip/{code}")
async def zip_lookup(code: str, storage: Storage = Depends(get_storage)):
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

    return _render("_suggestions.html", {"request": request, "suggestions": suggestions})


@app.get("/api/taxonomies")
async def taxonomy_list():
    return JSONResponse(
        content=TAXONOMY_DESCRIPTIONS,
        headers={"Cache-Control": "max-age=86400"},
    )


@app.get("/provider/{npi}", response_class=HTMLResponse)
async def provider_detail(
    request: Request,
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: Storage = Depends(get_storage),
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
            result = client.lookup(npi)
        except NPPESError as e:
            return _render("detail.html", {
                "request": request,
                "active_page": "search",
                "result": None,
                "error": str(e),
                "is_saved": False,
                "saved_notes": None,
                "saved_count": _saved_count(storage, user_id),
                "user": current_user,
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
        "saved_count": _saved_count(storage, user_id),
        "user": current_user,
    })


@app.post("/provider/{npi}/save", response_class=HTMLResponse)
async def save_provider(
    request: Request,
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    """Save a provider — returns button partial for htmx swap."""
    btn_target = request.headers.get("hx-target", "#save-btn").lstrip("#")

    # Anonymous users get an inline auth prompt instead of a redirect
    if current_user is None:
        return _render("_auth_gate.html", {"request": request, "btn_target": btn_target})

    user_id = current_user["id"]
    saved = storage.get_provider(npi, user_id)
    if saved:
        return _render("_save_button.html", {
            "request": request,
            "is_saved": True,
            "npi": npi,
            "btn_target": btn_target,
        })

    try:
        result = client.lookup(npi)
    except NPPESError:
        result = None

    if result:
        storage.save_provider(result, user_id)
        return _render("_save_button.html", {
            "request": request,
            "is_saved": True,
            "npi": npi,
            "btn_target": btn_target,
        })

    return HTMLResponse(
        content='<span style="color: #c62828;">Could not look up this provider. Try again.</span>'
    )


@app.delete("/provider/{npi}/save", response_class=HTMLResponse)
async def remove_provider(
    request: Request,
    npi: str,
    current_user: dict = Depends(require_user),
    storage: Storage = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.delete_provider(npi, user_id)

    hx_target = request.headers.get("hx-target", "")
    if hx_target.startswith("#saved-row-"):
        return HTMLResponse(content="")

    btn_target = hx_target.lstrip("#") if hx_target else "save-btn"
    return _render("_save_button.html", {
        "request": request,
        "is_saved": False,
        "npi": npi,
        "btn_target": btn_target,
    })


@app.get("/saved", response_class=HTMLResponse)
async def saved_list(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: Storage = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    return _render("saved.html", {
        "request": request,
        "active_page": "saved",
        "providers": providers,
        "saved_count": len(providers),
        "mapbox_token": MAPBOX_TOKEN,
        "user": current_user,
    })


@app.post("/provider/{npi}/appt-address", response_class=HTMLResponse)
async def set_appt_address(
    request: Request,
    npi: str,
    address: str = Form(""),
    current_user: dict = Depends(require_user),
    storage: Storage = Depends(get_storage),
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
    current_user: dict = Depends(require_user),
    storage: Storage = Depends(get_storage),
):
    user_id = current_user["id"]
    storage.clear_appt_address(npi, user_id)
    return _render("_appt_address.html", {
        "request": request,
        "npi": npi,
        "appt_address": None,
        "mapbox_token": MAPBOX_TOKEN,
    })


@app.get("/provider/{npi}/export", response_class=HTMLResponse)
async def export_view(
    request: Request,
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    user_id = current_user["id"] if current_user else None
    saved = storage.get_provider(npi, user_id)
    if saved:
        result = saved.to_npi_result()
    else:
        try:
            result = client.lookup(npi)
        except NPPESError as e:
            return HTMLResponse(content=f"<p>Error: {e}</p>", status_code=500)
        if result is None:
            return HTMLResponse(content=f"<p>No provider found for NPI {npi}.</p>", status_code=404)

    appt_address = saved.appt_address if saved else None
    export_text = referral_export(result, appt_address=appt_address)

    return _render("export.html", {
        "request": request,
        "active_page": "saved",
        "result": result,
        "export_text": export_text,
        "appt_address": appt_address,
        "saved_count": _saved_count(storage, user_id),
        "user": current_user,
    })


@app.get("/provider/{npi}/export/text")
async def export_text(
    npi: str,
    current_user: dict | None = Depends(get_current_user),
    storage: Storage = Depends(get_storage),
    client: NPPESClient = Depends(get_client),
):
    user_id = current_user["id"] if current_user else None
    saved = storage.get_provider(npi, user_id)
    if saved:
        result = saved.to_npi_result()
        appt_address = saved.appt_address
    else:
        try:
            result = client.lookup(npi)
        except NPPESError as e:
            return PlainTextResponse(content=f"Error: {e}", status_code=500)
        if result is None:
            return PlainTextResponse(content=f"No provider found for NPI {npi}.", status_code=404)
        appt_address = None

    text = referral_export(result, appt_address=appt_address)
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": f"attachment; filename=referral_{npi}.txt"},
    )


@app.get("/saved/export", response_class=HTMLResponse)
async def export_all(
    request: Request,
    current_user: dict = Depends(require_user),
    storage: Storage = Depends(get_storage),
):
    user_id = current_user["id"]
    providers = storage.list_providers(user_id)
    referrals = []
    for p in providers:
        result = p.to_npi_result()
        text = referral_export(result, appt_address=p.appt_address)
        referrals.append({
            "result": result,
            "export_text": text,
            "appt_address": p.appt_address,
        })
    return _render("export_all.html", {
        "request": request,
        "active_page": "saved",
        "referrals": referrals,
        "saved_count": len(providers),
        "user": current_user,
    })


@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    limit: int = Query(50),
    current_user: dict = Depends(require_user),
    storage: Storage = Depends(get_storage),
):
    user_id = current_user["id"]
    entries = storage.get_history(limit=limit, user_id=user_id)
    return _render("history.html", {
        "request": request,
        "active_page": "history",
        "entries": entries,
        "saved_count": _saved_count(storage, user_id),
        "user": current_user,
    })
