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
- Flat `src/docstats/` layout — one module per concern, each under 300 lines
- Core modules (client, models, storage, cache, normalize, scoring) have zero dependency on Rich/Typer/FastAPI
- Web layer imports core modules directly — no wrappers or adapters
- SQLite for everything: saved providers, search history, response cache, ZIP codes
- Single DB at `~/.local/share/docstats/docstats.db`
- Typeahead: name/org fields use htmx (server-side NPPES queries), specialty uses client-side filtering from static taxonomy list
- Location autocomplete uses Mapbox Geocoding API (client-side fetch, 300ms debounce) — reads `MAPBOX_PUBLIC_TOKEN` env var; gracefully absent if unset
- `initAutocomplete(inputEl, listEl)` in `index.html` is reusable for any field; `data-value=""` + `data-extra='{"field":"val"}'` pattern populates sibling fields on selection without htmx
- Web errors return HTTP 200 with error HTML partial (not 4xx/5xx) — htmx swaps normally; `htmx:responseError` in `base.html` handles true network/server failures
- Individual search form uses progressive disclosure: smart search bar shown by default; specialty and location are optional filters revealed via pill buttons
- Smart search: `parse.py` module (`parse_query()`, `build_interpretations()`) parses free-text query into ranked NPPES API fallback interpretations; winning `interp` dict populates `SearchQuery` for `rank_results()`
- `appt_address` stored per `SavedProvider` in SQLite; rendered as an editable chip in the saved list; appended to referral export
- Dark theme CSS in `base.html`: CSS custom properties (`--bg`, `--bg-card`, `--green`, `--blue`, `--text`, `--text-muted`, `--text-dim`, `--border`); no external CSS framework
- CSS utility classes in `base.html`: `.action-bar` (flex row for primary actions), `.back-link` (muted nav link), `.empty-state` (centered muted placeholder), `.badge`, `.badge-ind`, `.badge-org`, `.badge-active`, `.badge-inactive`, `.btn`, `.btn-primary`, `.btn-secondary`, `.btn-blue`, `.btn-sm`, `.result-card`, `.detail-section`, `.detail-table`

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
- Route ordering in `web.py` matters: specific routes (e.g., `/referral-lists/{id}/export`) must be declared before parameterized routes (`/referral-lists/{id}`) or FastAPI matches the wrong one
- htmx `HX-Target` header includes the `#` prefix from CSS selectors
- Pydantic `basic` field is a dict (not typed) because NPI-1 and NPI-2 have incompatible schemas — use `parsed_basic()` method
- Starlette 0.50+ changed `TemplateResponse` signature — use `_render()` helper in `web.py` instead of calling `templates.TemplateResponse()` directly
- Don't use `pip freeze` on this machine for generating `requirements.txt` — global env has 500+ unrelated packages
- `httpx.TimeoutException` is a subclass of `RequestError` — catch it first or it's dead code
- Templates must guard against `None` models — routes pass `result=None` on `NPPESError`
- `scoring.py` result ranking is currently only integrated into the Web UI (`web.py`), not the CLI (`cli.py`)
- `querySelectorAll('input')` does not match `<select>` elements — when clearing a form section, reset selects explicitly (e.g. `el.value = ''`) in addition to iterating inputs
- Use `clearSuggestions(id)` to clear suggestion lists in `index.html` — do not set `.innerHTML = ''` directly
- To trigger `initAutocomplete`'s `activeIdx` reset for a non-htmx list, dispatch `new Event('htmx:afterSwap')` on the list element after populating it
- JS that references elements inside `{% if ... %}` blocks must null-guard or be inside the same conditional — the element won't exist when the condition is false
- Mapbox Geocoding: for `postcode`-type features, the ZIP is in `f.text` not `f.context` — add `if (!zip && place_type is postcode) zip = f.text` alongside the `place`-type city fallback
- Mapbox tokens: `pk.` = public (safe for client-side JS), `sk.` = secret (server-side only)
- To safely inject a Python template variable into JS, use `{{ var | tojson }}` — handles escaping
- CSS `:has()` requires `@supports selector(:has(*))` guard — without it, hiding inputs styled only via `:has(input:checked)` leaves no visual feedback on Firefox <121; wrap both the `display:none` and the `:has()` rule together inside the `@supports` block
- CSS utility classes used in multiple templates must be defined standalone (e.g. `.back-link { ... }`) not only as descendant selectors (e.g. `.action-bar .back-link`) — descendants work only inside that specific parent; silently no-ops elsewhere

## Deployment (Railway)
- Hosted at https://docstats-production.up.railway.app
- Railway uses **Railpack** (not Nixpacks) — `nixpacks.toml` is ignored
- Config: `railway.toml` for build/start commands, `requirements.txt` for deps
- Railpack doesn't install pyproject.toml optional extras — `requirements.txt` must include all web deps explicitly
- Pre-launch protections active (issue #57 tracks removal): basic auth, robots.txt, X-Robots-Tag header, meta noindex
- Auth creds set as Railway env vars: `DOCSTATS_AUTH_USER`, `DOCSTATS_AUTH_PASS`
- `MAPBOX_PUBLIC_TOKEN` — Railway env var for address autocomplete (use `pk.` public token, not `sk.` secret)
- SQLite data doesn't persist across redeploys (ephemeral filesystem)
- Deploy: `railway up --detach --service docstats`

## Code Style
- Python 3.12+, type hints throughout
- Pydantic v2 for all data models
- `normalize.py` handles all API data cleanup (name casing, phone formatting, postal codes)
- No AI attribution in commits, PRs, or code
- Template JS uses `var` (not `const`/`let`) and vanilla DOM APIs — no build step, no modules
- Prefer `textContent`/`createElement` over `innerHTML` for dynamic content
