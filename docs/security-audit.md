# Security Audit: Input Sanitization

**Date**: 2026-04-16
**Scope**: Issue [#51](https://github.com/ringo380/docstats/issues/51)
**Milestone**: v0.2.0 — Foundation
**Auditor**: Internal review against OWASP ASVS Level 2 input-handling controls

This audit covers every category requested by #51: SQL injection, XSS,
validation, and path traversal — plus an incidental review of session cookie
posture and open-redirect risk.

---

## Summary

| Category | Result | Action |
|---|---|---|
| SQL injection (SQLite + PostgREST) | **PASS** | None |
| XSS (Jinja2 autoescape, script interpolation) | **PASS** | None |
| NPI format validation at route boundary | FIX | Shared `require_valid_npi` dependency |
| Search / form param length caps | FIX | `max_length=` on every `Query` / `Form` |
| Password `max_length` + bcrypt 72-byte clarity | FIX | Reject >72 bytes at the Form boundary |
| Email format validation | FIX | `validate_email()` in signup |
| Path traversal — exports, caches | LOW | Closed via NPI validation |
| Session cookie `httponly` / `samesite` | LOW | Made explicit |
| Open redirect | **PASS** | None |

---

## 1. SQL injection — PASS

Every SQLite call uses `?` placeholders. No f-string SQL on user input.

- `src/docstats/storage.py:225–530` — user CRUD, provider save/update/delete, search.
- `src/docstats/storage.py:326` — `UPDATE users SET {set_clause}` is safe: the
  set-clause is built from a whitelisted dict keyed by the function's typed
  kwargs (`first_name`, `last_name`, `middle_name`, `date_of_birth`,
  `display_name`); user values flow through `?` placeholders only.
- `src/docstats/storage.py:183, 216` — `ALTER TABLE ... ADD COLUMN {col}` is
  schema-only and uses a hardcoded list.
- `src/docstats/storage.py:422–433` — LIKE pattern search uses
  `_escape_like(query)` + `ESCAPE '\\'`.
- `src/docstats/cache.py:53–91`, `src/docstats/enrichment.py:100–134` —
  parameterized throughout.
- `src/docstats/pg_storage.py` — supabase-py `.eq()`, `.update()`, `.upsert()`
  pass user input as values (safe). `.or_()` is deliberately avoided for user
  input — provider search fetches all per-user rows and filters in Python.

**Verdict:** No SQL injection vectors. No code change required.

---

## 2. XSS — PASS

- `src/docstats/routes/_common.py:17` — `Jinja2Templates` is created without
  override, so autoescape is ON for `.html`.
- No `|safe`, `|e(false)`, or `{% autoescape false %}` anywhere in
  `src/docstats/templates/`.
- All `<script>` variable interpolations use `| tojson`:
  - `_appt_address.html:70`
  - `_search_js.html:3`
  - `saved.html:61`
- No `innerHTML =` assignments in `src/docstats/static/app.js` or template
  inline scripts. DOM is built with `createElement` / `textContent`.
- All `hx-*` URLs use NPI (numeric, now strictly validated) path params only.

**Verdict:** No XSS vectors. No code change required.

---

## 3. NPI format validation — FIX

**Risk:** 16+ routes under `/provider/{npi}/...`, plus
`/onboarding/select-pcp/{npi}` and `/profile/pcp/{npi}`, accepted `npi: str`
and passed it to storage / NPPES without format checks. `client.lookup()`
validates internally, but not every code path goes through it, and error
responses leaked as generic 500s instead of 422s.

**Fix:** new `src/docstats/validators.py` with:

- `NPI_PATTERN = re.compile(r"^\d{10}$")`
- `validate_npi(npi) -> str` — raises `ValidationError` on malformed input.
- `require_valid_npi` FastAPI dependency — raises HTTP 422 with a friendly
  message when the path segment is not 10 digits.

Applied to every `/{npi}/...` route in `routes/providers.py`,
`routes/onboarding.py`, and `routes/profile.py`.

**Collateral benefit:** closes the theoretical header-injection gap in
`Content-Disposition: attachment; filename=referral_{npi}.txt` — `\r\n` or
`/` in NPI is now a 422 before the header is ever written.

---

## 4. Length caps on user input — FIX

All `Query(...)` / `Form(...)` declarations now have `max_length=` constraints:

| Field | Cap | File |
|---|---|---|
| search query, name, org, specialty | 100–200 | `routes/search.py` |
| city | 100 | `routes/search.py` |
| state, geo_state | 2 | `routes/search.py` |
| zip | 10 | `routes/search.py` |
| geo_lat / geo_lon | 20 | `routes/search.py` |
| limit | `1–100` (int bounds) | `routes/search.py` |
| address | 300 | `routes/providers.py` |
| phone / fax | 40 | `routes/providers.py` |
| suite | 100 | `routes/providers.py` |
| notes | 2000 | `routes/providers.py` |
| first/last/middle name | 100 | `routes/onboarding.py` |
| date_of_birth | 10 | `routes/onboarding.py` |
| terms_version | 32 | `routes/onboarding.py` |
| email | 254 (RFC 5321) | `routes/auth.py` |
| password | 72 (bcrypt limit) | `routes/auth.py` |
| suggest `q` | 200 | `routes/api.py` |
| suggest `field` | 32 | `routes/api.py` |
| ZIP path param | 3–10 | `routes/api.py` |

---

## 5. Password truncation + email format — FIX

**Password:** `max_length=72` on the signup/login/confirm Form fields.
Oversized input is rejected with 422 at the boundary, preventing silent
bcrypt truncation.

**Email:** `validate_email()` in `routes/auth.py:signup_post` before the
storage lookup. `storage_base.normalize_email()` stays as a thin
lowercase+strip helper (callers that need validation use `validate_email`).

---

## 6. Path traversal — LOW / closed

- `src/docstats/oig_client.py:31,50-51` — cache dir is
  `Path.home() / ".local/share/docstats/leie"`, hardcoded.
- `src/docstats/enrichment.py:69-72` — SQLite path from `get_db_path()`,
  hardcoded.
- `src/docstats/routes/saved.py:59,75` — export filenames are date-based
  (`referrals_2026-04-16.csv`), no user input.
- `src/docstats/routes/providers.py:83` — per-provider export filename
  includes NPI, now strictly validated as 10 digits.
- `src/docstats/web.py:39` — static files served via FastAPI `StaticFiles`
  mount (safe).

**Verdict:** No user input reaches a filesystem path. The only NPI-in-header
case is now closed by #3.

---

## 7. Session cookies — LOW / made explicit

`src/docstats/web.py:43–49` now sets `same_site="lax"` explicitly on
`SessionMiddleware`. Starlette's defaults are already `HttpOnly=True` and
`SameSite=lax`, but making them explicit is self-documenting and guards
against a future default change. `Secure` flag is still conditional on
`RAILWAY_ENVIRONMENT == "production"` so localhost dev over HTTP keeps
working.

---

## 8. Open redirect — PASS

Grepped all route modules: every `RedirectResponse` target is a literal
string. No `?next=` or user-controlled redirect parameter. OAuth callback
errors route to `/auth/login?error=oauth` (static).

---

## Out of Scope

- **Rate limiting** — tracked in issue #48.
- **Dependency vulnerability scanning** — tracked in issue #52.
- **CSRF tokens** — Starlette default `SameSite=Lax` on the session cookie
  is sufficient for current same-origin form flows; revisit if cross-origin
  embeds or JSON APIs are added.
- **Field-level encryption** for notes / addresses — product decision, not a
  validation concern.

---

## Verification

```
pytest tests/test_validators.py tests/test_security.py -v
pytest                             # full suite, no regressions
ruff check .
mypy src/docstats/
```

Manual smoke checks documented in `.claude/plans/51-parsed-rossum.md`
(section "Verification"). Key flows: malformed NPI → 422; oversize query
→ 422; invalid email at signup → friendly error; 100-char password →
422; inspect session cookie for `HttpOnly` + `SameSite=Lax`.
