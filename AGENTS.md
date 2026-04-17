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
- `src/docstats/domain/` is the referral-platform domain layer. Keep it free of FastAPI dependencies where possible; FastAPI deps live in `routes/`
- Audit log: `domain/audit.py` exposes `AuditEvent` model + `record(storage, *, action, request=None, ...)` helper that swallows errors. Action vocabulary is `{entity}.{verb}` (e.g. `user.login`, `provider.save`). Storage methods `record_audit_event` / `list_audit_events`; rows are append-only by contract. Postgres table `docstats_audit_events`; SQLite `audit_events`. FKs use `ON DELETE SET NULL` so audit rows survive user deletion
- Dual-mode scope: `docstats/scope.py` defines the `Scope` frozen dataclass. `routes._common.get_scope` is the FastAPI dep; silently falls back to solo mode if `active_org_id` is stale. Every Patient/Referral row from Phase 1+ must carry exactly one of `scope_user_id` or `scope_organization_id`
- Organizations + memberships: `domain/orgs.py` holds `Organization`, `Membership`, `ROLES` ladder (read_only → staff → clinician → coordinator → admin → owner), `has_role_at_least()`. Slugs use a partial unique index (live rows only). `memberships.(organization_id, user_id)` is a plain UNIQUE — rejoining a soft-deleted member must reactivate the existing row
- Server-side sessions: `domain/sessions.py` `Session` model; storage methods `create_session` (URL-safe 32-byte token id), `get_session`, `touch_session`, `revoke_session`, `list_sessions_for_user`, `purge_expired_sessions`. Cookie carries both `user_id` and `session_id`; `get_current_user` rejects revoked/expired rows. `_begin_session` in `routes/auth.py` rotates on every login (revokes any prior session), degrades to cookie-only on DB write failure. DB lookup failures fail CLOSED (deny access). Sessions cascade on user delete (unlike audit events which SET NULL)
- PHI consent: four columns on `users` (`phi_consent_version`, `phi_consent_at`, `phi_consent_ip`, `phi_consent_user_agent`) separate from `terms_*`. Storage: `record_phi_consent`. `src/docstats/phi.py` exposes `CURRENT_PHI_CONSENT_VERSION` + `require_phi_consent` dep (subclass of `AuthRequiredException`); no route uses it yet — Phase 2 PHI-entry routes will
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
- Appointment location: `appt_address` (Mapbox geocoded or manual) + `appt_suite` (free-text suite/room/office) + `appt_phone` + `appt_fax` (location-specific contact info) stored on `saved_providers`; `_appt_address.html` partial handles all; suite/phone/fax inputs appear after address is set; `DELETE /provider/{npi}/appt-address` clears all four; `PUT /provider/{npi}/appt-suite` edits suite independently; `PUT /provider/{npi}/appt-contact` edits phone+fax together
- Televisit: `is_televisit` boolean on `saved_providers`; `PUT /provider/{npi}/televisit` toggles the flag; when enabled, hides address section and shows a "Televisit" chip; toggling ON clears address/suite/phone/fax
- Appointment address Mapbox search includes `types=poi,address,place,postcode` — POI enables facility/business name lookup; phone from `f.properties.tel` auto-populates `appt_phone` on POI selection
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
- All `/provider/{npi}/...`, `/onboarding/select-pcp/{npi}`, `/profile/pcp/{npi}` routes must use `npi: str = Depends(require_valid_npi)` (from `docstats.validators`) — never raw `npi: str`; keeps malformed input at the boundary (422) and closes header-injection in export filenames. Same for new Form/Query fields: add `max_length=` per the caps in `docs/security-audit.md`
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
- All HTTP clients (NPPES, OIG, CMS, Open Payments) use `request_with_retry()` from `http_retry.py` for exponential backoff retry — NPPES translates `_NPPESRetryExhausted` into user-facing `NPPESError` in `client._friendly_transport_error`
- Retry tests must patch `client._http.request` (not `.get`/`.post`) and `docstats.http_retry.time.sleep` — retry logic is in `http_retry.py`, not in each client module
- HTTP timeout/retry knobs come from env vars (read at call time, not import): `DOCSTATS_HTTP_TIMEOUT` (default 30.0s) for httpx client timeout; `DOCSTATS_HTTP_MAX_RETRIES` (default 3) for retries on 429/5xx/timeout; `DOCSTATS_HTTP_CONCURRENCY` (default 5) for the semaphore size returned by `docstats.concurrency.async_limiter()` for batch callers. New httpx clients should read `get_default_timeout()` at `__init__`; new batch paths should consume `async_limiter()` instead of unbounded `asyncio.gather`
- `NPPESClient.async_lookup_many(npis, *, limiter=None)` is the batch lookup seam — results in input order, capped by the passed semaphore or `async_limiter()`. Pass a shared `asyncio.Semaphore` when batching from a long-running process so the cap is shared across calls
- `request_with_retry()` honors integer-seconds `Retry-After` headers on retryable statuses (≥ 0.5s); HTTP-date form is not supported
- `http_retry.py` `backoff_base` is an *exponent base*, not a constant multiplier — delays are `backoff_base ** attempt`. Default `2.0` → `1s, 2s, 4s`; setting `backoff_base=1.0` silently degrades to `1s, 1s, 1s` (`1**n == 1`). If you change it, update `test_exponential_backoff_delays` and the docstring together
- To smoke-test NPPES retry error messages without a real network failure: `DOCSTATS_HTTP_TIMEOUT=0.001 DOCSTATS_HTTP_MAX_RETRIES=0 docstats web` — forces the "took too long" `NPPESError` path instantly
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
- Adding a column to `saved_providers` requires updates in: (1) `storage_base.py` abstract method (if new public API), (2) `storage.py` migration + `save_provider` INSERT + `_row_to_provider`, (3) `pg_storage.py` `_row_to_provider` + `save_provider` upsert dict + preserve-on-conflict fetch, (4) `models.py` `SavedProvider` field + `export_fields()`, (5) `routes/saved.py` `_CSV_FIELDNAMES` + all relevant route template contexts
- `saved.html` includes `_appt_address.html` via `{% set npi %}{% set appt_address %}{% set appt_suite %}{% set appt_phone %}{% set appt_fax %}{% set is_televisit %}{% include %}` — any new context variable for that partial must be added to the `{% set %}` chain AND to all `render("_appt_address.html", {...})` calls in `routes/providers.py`
- `pg_storage.py` has no auto-migration — new columns must be added to Supabase manually via Management API SQL endpoint before deploying code that references them. Migration SQL files live in `docs/migrations/NNN_name.sql`; apply before merging the code PR that uses the new schema
- When adding a storage method that inserts a row, prefer `ON DELETE SET NULL` over `ON DELETE CASCADE` on audit / log / immutable-history tables — cascading deletes silently destroy evidence. `CASCADE` is fine for purely derived data like `saved_providers` (per-user rolodex)
- Any new keys added to `SavedProvider.export_fields()` must also be added to `_CSV_FIELDNAMES` in `routes/saved.py` — `DictWriter` raises `ValueError` on extra keys by default
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
