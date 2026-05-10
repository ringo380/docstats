# Data Classification Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

Define data sensitivity tiers and the handling requirements for each. Maps to HIPAA §164.502 and SOC 2 CC6.1.

## Tiers

### Tier 1 — Public

Data intentionally exposed to the world.

- **Examples**: marketing pages on referme.help, blog posts, public NPI registry data (NPPES is public domain), CSV templates, OpenAPI spec.
- **Handling**: no restrictions. May be cached, copied, indexed.

### Tier 2 — Internal

Data not intended for public consumption but not sensitive.

- **Examples**: aggregate analytics counts, error rates, deploy logs that don't contain user data, this policy binder.
- **Handling**: store on company-controlled systems; need-to-know access; OK to share within the organization.

### Tier 3 — Confidential

Sensitive business data not regulated as PHI.

- **Examples**: vendor BAA terms, contract drafts, financial statements, credentials, security incident reports (with PHI redacted), source code containing security logic.
- **Handling**: encrypt at rest + in transit; access on need-to-know; do not commit credentials to repo (gitleaks gate); audit access via vendor logs.

### Tier 4 — PHI / regulated

Protected Health Information as defined by HIPAA, plus equivalent categories under state law.

- **Examples**: patient names, DOB, MRN, addresses, phone numbers, diagnoses, medications, allergies, encounter notes, insurance plan details, EHR identifiers (FHIR resource IDs), referral content, attachments, DICOM images, lab results.
- **Handling**: see "PHI handling rules" below.

## PHI handling rules

These are normative. Violations are security incidents per `incident-response.md`.

### Storage

- **Authoritative store**: Supabase Postgres + Supabase Storage, both at Team tier with BAA.
- **Allowed copies**:
  - Local SQLite for dev (developer laptop with full-disk encryption — developer's responsibility).
  - Per-request cache in application memory, lifetime ≤ request.
  - Audit log entries (subject to redaction — never log full PHI fields).
- **Disallowed copies**: any cache, queue, log, or sink not enumerated above. No "I'll just temporarily put this in a Google Doc."

### Transit

- TLS 1.2+ everywhere (see `encryption.md`).
- HMAC-signed webhooks for inter-system PHI-bearing callbacks.
- Direct Trust HISP for PHI-bearing email (never plain email body).
- Fax via Documo for legacy recipients.

### In application code

- **Never put PHI in URLs** (path, query string). URLs land in browser history, server access logs, Referer headers, and CDN logs.
- **Never put PHI in session cookies** — Starlette `SessionMiddleware` is signed but not encrypted. Re-fetch from DB on each render.
- **Never put PHI in logs** — `tests/test_no_phi_in_logs.py` AST-gates this. Use lazy `%s` formatting so a future redaction layer can hook the args.
- **Never put PHI in error responses to API consumers** — return generic error codes; log internally.
- **Never put PHI in third-party analytics** — GA4 is loaded, but we do not pass PHI to it.

### In communications

- **Never put PHI in transactional email bodies** — use signed-link pattern (link into the app; auth gates the actual content).
- **Never put PHI in outbound webhooks unless the receiver is a BAA-covered sub-processor**.
- **Never put PHI in customer support tickets** unless the support system has a BAA (currently it does not).

### In code / commits

- **Never commit PHI to the repo** — even in test fixtures. Use synthetic data (faker, made-up names).
- gitleaks CI gate scans for credentials but not PHI patterns; the discipline is human.
- `.gitignore` excludes `.claude/`, `CLAUDE.md`, `memory/` to keep working artifacts out.

### In screenshots / mockups / demos

- All screenshots used in marketing, docs, or demos must use synthetic data.
- Never screenshot real customer data, even for internal use, unless explicitly anonymized.

## Retention

| Tier | Retention | Disposal |
|---|---|---|
| Public | Indefinite | None required |
| Internal | 3 years rolling | Standard delete |
| Confidential | 7 years from creation | Standard delete + remove backups |
| PHI | Per HIPAA + customer BAA (default 7 years from last activity); per-org override via `attachment_retention_days` (30–10950) | Cryptographic erasure (delete row + remove from backups within 30 days) |

Audit log retention: 7 years (HIPAA Security Rule §164.312(b) implication).
