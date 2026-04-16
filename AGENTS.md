# docstats

<!-- This file mirrors CLAUDE.md for agents that read AGENTS.md instead (Codex, Gemini, etc.).
     Keep in sync when updating CLAUDE.md. -->

## Build & Test
- `pip install -e .` — install CLI
- `pip install -e ".[web]"` — install CLI + web UI
- `pip install -e ".[dev]"` — install with test deps
- `pytest` — run all tests
- `ruff check .` — run lint checks
- `docstats web` — start web UI at http://127.0.0.1:8000

## Architecture
- `src/docstats/` layout — one module per concern, each under 300 lines; web routes split into `routes/` subpackage
- Core modules (client, models, storage, cache, normalize, scoring) have zero dependency on Rich/Typer/FastAPI
- Web layer imports core modules directly — no wrappers or adapters
- Dual-backend storage: Supabase Postgres in production (when `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are set), SQLite locally (default)
- SQLite DB at `~/.local/share/docstats/docstats.db` (local dev/CLI only)
- Supabase tables use `docstats_` prefix (e.g. `docstats_users`) to coexist with robworks-software in shared project (ref: `uhnymifvdauzlmaogjfj`)
- `get_storage()` returns `PostgresStorage` or `Storage` (SQLite) — both inherit from `StorageBase` ABC in `storage_base.py`; consumers should type-hint `StorageBase`
- `storage_base.py` contains `StorageBase` ABC + shared helpers (`normalize_email`, `fuzzy_score`); `storage.py` contains `Storage(StorageBase)` (SQLite) and `get_storage()`; `pg_storage.py` contains `PostgresStorage(StorageBase)` (Supabase)
- Web routes split into `routes/` subpackage: `auth.py`, `onboarding.py`, `profile.py`, `providers.py`, `saved.py`, `search.py`, `api.py`; shared helpers in `routes/_common.py` (`render`, `saved_count`, `get_client`, `MAPBOX_TOKEN`, `US_STATES`); `web.py` is the app shell (middleware, exception handlers, router includes, home + history routes)
- Typeahead: name/org fields use htmx (server-side NPPES queries), specialty uses client-side filtering from static taxonomy list
- Location autocomplete uses Mapbox Geocoding API (client-side fetch, 300ms debounce) — reads `MAPBOX_PUBLIC_TOKEN` env var; gracefully absent if unset
- `initAutocomplete(inputEl, listEl)` in `index.html` is reusable for any field; `data-value=""` + `data-extra='{"field":"val"}'` pattern populates sibling fields on selection without htmx
- Web errors return HTTP 200 with error HTML partial (not 4xx/5xx) — htmx swaps normally; `htmx:responseError` in `base.html` handles true network/server failures
- Individual search form uses progressive disclosure: smart search bar shown by default; specialty and location are optional filters revealed via pill buttons
- Auth: `auth.py` (bcrypt password hashing, `get_current_user`/`require_user` FastAPI deps, anon search counter), `oauth.py` (GitHub OAuth helpers); sessions via Starlette `SessionMiddleware` with signed cookie
- All provider/history data is per-user: `saved_providers` PK is `(user_id, npi)`, `search_history` has nullable `user_id`; `get_provider(npi, None)` always returns None (anonymous)
- `get_storage()` is defined in `storage.py` (not `web.py`); web.py imports and re-exports it for backward compatibility
- Anonymous users get 3 free searches (tracked in session cookie); save attempts return `_auth_gate.html` inline partial instead of a redirect
- Smart search: `parse.py` module (`parse_query()`, `build_interpretations()`) parses free-text query into ranked NPPES API fallback interpretations; winning `interp` dict populates `SearchQuery` for `rank_results()`
- Onboarding: 4-step wizard at `/onboarding` (name → DOB → PCP selection → terms acceptance); `_onboarding_step()` computes resume position from user record; step transitions via `HX-Trigger: stepComplete` + JS listener; PCP step is skippable (tracked in session `pcp_skipped` flag)
- User profile fields: `first_name`, `last_name`, `middle_name`, `date_of_birth` on users table; `update_user_profile()` method on both storage backends; also sets `display_name` to "First Last" during onboarding
- Terms acceptance: `terms_accepted_at` (UTC), `terms_version`, `terms_ip` (proxy-aware via X-Forwarded-For), `terms_user_agent` stored per-user; `record_terms_acceptance()` method; `terms_accepted_at` is the onboarding completion gate (replaces old `pcp_npi` gate)
- Profile dropdown: navbar shows user initials avatar (inline SVG) + name + chevron; dropdown with Profile/Sign Out; click-outside closes; CSS class `.profile-menu.open` toggles visibility
- `appt_address` stored per `SavedProvider` in SQLite; rendered as an editable chip in the saved list; appended to referral export
- Favicon is a data-URI SVG in `base.html` `<head>`; static files (CSS, JS) served from `src/docstats/static/` via FastAPI `StaticFiles` mount at `/static`
- CSS in `static/style.css` (external, browser-cacheable); JS in `static/app.js` (htmx error handler, profile menu, note editor); `base.html` is layout + nav only (~69 lines)
- `base.html` provides `{% block head_extra %}` for page-specific CSS/JS in `<head>`
- Dark theme via CSS custom properties (`--bg`, `--bg-card`, `--green`, `--blue`, `--text`, `--text-muted`, `--text-dim`, `--border`) in `static/style.css`; no external CSS framework

## NPPES API
- Endpoint: `https://npiregistry.cms.hhs.gov/api/?version=2.1`
- Cannot combine NPI-1 fields (first_name, last_name) with NPI-2 fields (organization_name)
- No middle_name search param — use for post-retrieval ranking only
- Returns names UPPERCASE, phones unformatted, postal codes raw (9 digits)
- API has no auth, max 1200 results per query
- Name searches match against `other_names` (former/maiden names) too — results sorted alphabetically by current name, so common names (Smith, Patel) return mostly former-name matches first; prefix-filter results by current name when relevance matters
- `taxonomies.py` has 883 NUCC taxonomy descriptions for client-side specialty autocomplete

## Gotchas
- SQLite connections need `check_same_thread=False` for FastAPI/uvicorn
- SQLite WAL mode (`PRAGMA journal_mode=WAL`) required for concurrent CLI+web access
- SQLite FK enforcement is off by default — `PRAGMA foreign_keys = ON` must be set per-connection (after WAL pragma in `Storage.__init__`)
- `INSERT OR REPLACE` triggers FK cascade deletes on the replaced row — use `INSERT ... ON CONFLICT(col) DO UPDATE SET ...` upsert to preserve child rows
- Same-second SQLite timestamps make `ORDER BY created_at DESC` non-deterministic in tests — always include `id DESC` as a tiebreaker
- Route ordering via `app.include_router()` in `web.py` matters: `saved_router` must be included before `providers_router` so `/saved/export/csv` matches before `/provider/{npi}`; within each router, specific routes (e.g., `/{npi}/export`) must be declared before parameterized routes
- htmx `HX-Target` header includes the `#` prefix from CSS selectors
- Pydantic `basic` field is a dict (not typed) because NPI-1 and NPI-2 have incompatible schemas — use `parsed_basic()` method
- Starlette 0.50+ changed `TemplateResponse` signature — use `render()` from `routes/_common.py` instead of calling `templates.TemplateResponse()` directly
- When adding new storage methods, add the abstract method to `StorageBase` first — both `Storage` and `PostgresStorage` inherit from it and must implement all abstract methods
- `normalize_email()` and `fuzzy_score()` live in `storage_base.py` — use these instead of inline `email.strip().lower()` or reimplementing fuzzy matching
- All three enrichment API clients use `request_with_retry()` from `http_retry.py` for exponential backoff retry
- Enrichment client retry tests must patch `client._http.request` (not `.post`) and `docstats.http_retry.time.sleep` — retry logic is in `http_retry.py`, not in each client module
- New routes go in the appropriate `routes/*.py` file, not in `web.py` — only app-level middleware, exception handlers, and router includes belong in `web.py`
- Don't use `pip freeze` on this machine for generating `requirements.txt` — global env has 500+ unrelated packages
- `httpx.TimeoutException` is a subclass of `RequestError` — catch it first or it's dead code
- Templates must guard against `None` models — routes pass `result=None` on `NPPESError`
- `scoring.py` result ranking is integrated into both the Web UI (via `routes/search.py`) and the CLI (via `services.py` `search_providers`)
- `querySelectorAll('input')` does not match `<select>` elements — when clearing a form section, reset selects explicitly (e.g. `el.value = ''`) in addition to iterating inputs
- Use `clearSuggestions(id)` to clear suggestion lists in `index.html` — do not set `.innerHTML = ''` directly
- To trigger `initAutocomplete`'s `activeIdx` reset for a non-htmx list, dispatch `new Event('htmx:afterSwap')` on the list element after populating it
- JS that references elements inside `{% if ... %}` blocks must null-guard or be inside the same conditional — the element won't exist when the condition is false
- Mapbox Geocoding: for `postcode`-type features, the ZIP is in `f.text` not `f.context` — add `if (!zip && place_type is postcode) zip = f.text` alongside the `place`-type city fallback
- Mapbox tokens: `pk.` = public (safe for client-side JS), `sk.` = secret (server-side only)
- To safely inject a Python template variable into JS, use `{{ var | tojson }}` — handles escaping
- CSS `:has()` requires `@supports selector(:has(*))` guard — without it, hiding inputs styled only via `:has(input:checked)` leaves no visual feedback on Firefox <121; wrap both the `display:none` and the `:has()` rule together inside the `@supports` block
- CSS utility classes used in multiple templates must be defined standalone (e.g. `.back-link { ... }`) not only as descendant selectors (e.g. `.action-bar .back-link`) — descendants work only inside that specific parent; silently no-ops elsewhere
- `hx-swap="outerHTML"` on a button inside a named `<div id="...">` destroys that container ID — subsequent htmx clicks target a missing element; use `hx-swap="innerHTML"` on the container instead
- `_save_button.html` uses `btn_target` variable (container ID without `#`) so it works from multiple call sites; routes pass it via `request.headers.get("hx-target", "#save-btn").lstrip("#")`
- History re-run links navigate to `/?query=...`; `index.html` has a `DOMContentLoaded` handler that reads `?query=` from the URL and auto-triggers `htmx.trigger(form, 'submit')` — required for re-run to land on results
- In the smart-search path (`query` param), `rank_results()` must receive a `SearchQuery` built from the winning `interp` dict (`first_name`, `last_name`, `organization_name`, `taxonomy_description`), not the empty structured-form fields
- `require_user` dependency raises `AuthRequiredException`; the exception handler in `web.py` returns `HX-Redirect` header (200) for HTMX requests, and `303` redirect for normal requests — HTMX doesn't follow 3xx redirects correctly
- Anonymous saves hit `POST /provider/{npi}/save` which returns `_auth_gate.html` (not a redirect) so HTMX can swap it inline; never use `require_user` on this route
- `SESSION_SECRET_KEY` not set → random key generated at startup (dev-only fallback); sessions won't survive server restarts without it set in env
- `saved_providers` migration: `_migrate_saved_providers()` checks `PRAGMA table_info` for `user_id`; if absent, drops and recreates with composite PK — existing data is lost (acceptable on Railway due to ephemeral filesystem)
- All full-page routes must pass `user=current_user` in template context for `base.html` nav to render correctly
- Test auth override: `app.dependency_overrides[get_current_user] = lambda: fake_user_dict` — `require_user` inherits this automatically since it depends on `get_current_user`
- `passlib[bcrypt]` is incompatible with `bcrypt>=4.0.0` — pin `bcrypt>=3.2.0,<4.0.0` in both `requirements.txt` and `pyproject.toml` web extras; bcrypt 4.x raises `ValueError` on passwords >72 bytes instead of silently truncating
- `python-multipart` must be explicit in `requirements.txt` — FastAPI requires it for any form POST route; Railpack won't install it as a transitive dep
- CSS input styles in `base.html` must enumerate `input[type="email"]` and `input[type="password"]` explicitly — they don't inherit from `input[type="text"]` rules
- Railway build environment uses Python 3.13 — passlib prints a `crypt` deprecation warning on startup; harmless but expected
- Onboarding gate checks `terms_accepted_at` (DB column), not `pcp_npi` — GitHub OAuth bypass must also check `terms_accepted_at` to avoid skipping terms acceptance
- `_onboarding_step()` accepts `pcp_skipped` kwarg from session — if PCP was skipped, step 3 is bypassed on resume so user lands on step 4 (terms) instead of looping back to PCP
- `date_of_birth` must be validated server-side (`date.fromisoformat()` + future-date check) — the HTML `max` attribute is client-side only

## Deployment (Railway)
- Hosted at https://docstats-production.up.railway.app
- Railway uses **Railpack** (not Nixpacks) — `nixpacks.toml` is ignored
- Config: `railway.toml` for build/start commands, `requirements.txt` for deps
- Railpack doesn't install pyproject.toml optional extras — `requirements.txt` must include all web deps explicitly
- Pre-launch protections: HTTP Basic Auth removed; robots.txt and X-Robots-Tag header remain
- Required Railway env vars: `SESSION_SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- `SUPABASE_URL` = `https://uhnymifvdauzlmaogjfj.supabase.co` (robworks-software project)
- `SUPABASE_SERVICE_KEY` = service_role JWT for robworks-software (set on Railway, also in `~/.zshrc`)
- Optional Railway env vars: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` (GitHub OAuth App; callback URL: `https://referme.help/auth/github/callback`)
- `MAPBOX_PUBLIC_TOKEN` — Railway env var for address autocomplete (use `pk.` public token, not `sk.` secret)
- Data persists across deploys via Supabase Postgres (SQLite ephemeral filesystem issue is resolved)
- Deploy: `railway up --detach --service docstats`

## Code Style
- Python 3.12+, type hints throughout
- Pydantic v2 for all data models
- `normalize.py` handles all API data cleanup (name casing, phone formatting, postal codes)
- No AI attribution in commits, PRs, or code
- Template JS uses `var` (not `const`/`let`) and vanilla DOM APIs — no build step, no modules
- Prefer `textContent`/`createElement` over `innerHTML` for dynamic content
