# docstats API v2

**Source of truth**: [`src/docstats/routes/api_v2.py`](../src/docstats/routes/api_v2.py). This doc lags by review cadence — when the two disagree, the code wins.

Stable, versioned read endpoints + a dead-lettered webhook inbox. Shipped in Phase 8. The v2 namespace is the first machine-consumable surface; Phase 12 (SMART-on-FHIR) will add OAuth2, write endpoints, and `\$everything`-style bundles on top of the same `/api/v2` prefix.

All responses carry `X-Docstats-Api-Version: 2` so consumers can assert they're talking to the version they compiled against.

## Endpoint catalog

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/referrals/{id}` | Fetch a referral. Plain JSON by default; FHIR Bundle when `Accept: application/fhir+json`. |
| `GET` | `/api/v2/patients/{id}` | Fetch a patient. Plain JSON by default; bare FHIR `Patient` resource when `Accept: application/fhir+json`. |
| `POST` | `/api/v2/webhooks/inbound` | HMAC-signed dead-letter inbox for future EHR / delivery-vendor integrations. |

Intentionally **not** shipped in Phase 8:

- List endpoints (`GET /api/v2/referrals?status=...`). Response size would be unbounded without a pagination plan. Deferred to Phase 12.
- Write endpoints. `POST` / `PATCH` on referrals and patients go through the web UI; programmatic writes land with Phase 12 SMART-on-FHIR.
- OAuth2 client-credentials. Phase 8 consumers authenticate via a session cookie obtained from the web UI.
- `\$everything`-style patient-centric bundles. Deferred to Phase 12.

## Authentication

Session cookie, obtained by logging in through the web UI. Send the cookie on every API request:

```bash
# One-time: log in interactively, save cookies to disk.
curl -c cookies.jar -b cookies.jar \
     -d "email=coordinator@clinic.example" \
     -d "password=…" \
     https://referme.help/auth/login

# Subsequent API calls reuse the cookie jar.
curl -b cookies.jar \
     -H "Accept: application/fhir+json" \
     https://referme.help/api/v2/referrals/42
```

### Error shapes by auth state

| State | HTTP | Body shape |
|---|---|---|
| No session cookie | `401` | `{"detail": {"code": "authentication_required", "message": "…"}}` |
| Session cookie valid, PHI consent not granted | `403` | `{"detail": {"code": "phi_consent_required", "message": "…"}}` |
| Session cookie valid + consented | `200`/`404`/`409` | See below. |

**Important**: unauthenticated API calls return JSON 401, **not** a 303 redirect to `/auth/login`. The global `AuthRequiredException` handler that redirects browser requests is bypassed for `/api/v2/*` via a dedicated `require_user_api` dependency. Clients that cannot follow redirects (curl default, most HTTP SDKs) Just Work.

## Content negotiation

The `Accept` request header selects the response format:

| `Accept` header contains | Referral response | Patient response |
|---|---|---|
| `application/fhir+json` | FHIR R4 Bundle (`type=document`) | Bare FHIR `Patient` resource (not a Bundle) |
| anything else, including `*/*` or absent | Plain JSON mirroring the pydantic model | Plain JSON mirroring the pydantic model |

### Simplification

We perform a **substring check** for `fhir+json` in the `Accept` header — not RFC 7231 q-value parsing. In practice this handles the two formats every real consumer sends (`application/fhir+json` alone, or as one option in a multi-type Accept header). If you need strict RFC 7231 semantics, open an issue.

`*/*` stays on plain JSON. A regression test pins this; a future contributor rewriting the negotiator with a real parser will break that test first.

### Plain JSON response shape

Direct serialization of the underlying pydantic model via `model_dump(mode="json")` — dates become ISO-8601 strings, nothing else is transformed. Fields include storage columns like `scope_user_id`, `scope_organization_id`, `assigned_to_user_id`, `created_at`, `updated_at`. Consumers should treat any field not documented in the FHIR mapping as potentially unstable across versions.

### FHIR response shape

See [`docs/fhir-mapping.md`](fhir-mapping.md) for the resource-by-resource mapping. Bundles are `type=document` (read semantics). FHIR errors are emitted as `OperationOutcome`:

```json
{
  "resourceType": "OperationOutcome",
  "issue": [
    {
      "severity": "error",
      "code": "not-found",
      "diagnostics": "Referral 999 not found or not visible to this scope."
    }
  ]
}
```

## Error codes

Plain-JSON errors use `{"code": "<token>", "detail": "<human>"}`; fhir+json errors use `OperationOutcome` with a matching `issue[0].code`. HTTP status is shared.

| HTTP | Plain-JSON `code` | FHIR `issue.code` | Trigger |
|---|---|---|---|
| `401` | `authentication_required` | n/a (pre-auth) | No session cookie. |
| `403` | `phi_consent_required` | n/a | Session cookie valid but no current PHI consent. |
| `404` | `not-found` | `not-found` | Entity doesn't exist or isn't visible to the caller's scope. Cross-tenant reads return 404 (not 403) so the API doesn't leak existence. |
| `409` | `incomplete` | `incomplete` | Referral exists but the linked patient row is unavailable (data integrity issue). |

Webhook endpoint error codes are separate — see the Webhooks section.

## Response headers

| Header | Value | Set on |
|---|---|---|
| `X-Docstats-Api-Version` | `2` | Every response (success and error). |
| `Content-Type` | `application/json` or `application/fhir+json` | Negotiated per request. |
| `Cache-Control` | `private, no-store` | Read endpoints — PHI must not cache. |
| `X-Content-Type-Options` | `nosniff` | All responses. |

## Audit trail

Every successful read lands in `audit_events` with action `referral.api_v2.read` or `patient.api_v2.read`. The metadata captures:

```json
{
  "accept_header": "application/fhir+json",
  "content_type": "application/fhir+json",
  "bundle_entries": 9
}
```

Use this to grep consumer behavior — which formats real callers want, how often. `bundle_entries` is always `0` in plain-JSON mode.

## Webhooks

### `POST /api/v2/webhooks/inbound`

Dead-lettered HMAC-signed JSON inbox for future integrations. Phase 8 only persists rows; Phase 9+ will consume them (delivery status callbacks, EHR pushes, etc.).

**Disabled by default.** Set `WEBHOOK_INBOX_SECRET` to activate. Without it, every request returns `503 {"code": "endpoint_disabled"}` — this is administratively distinguishable from "wrong signature" (`401`).

### Required headers

| Header | Purpose |
|---|---|
| `X-Timestamp` | Unix seconds when the payload was signed. |
| `X-Signature` | HMAC-SHA-256 over `<X-Timestamp>.<body>` using the shared secret. Accepts either `sha256=<hex>` prefix or bare hex. |
| `X-Source` (optional) | Caller identity string; persisted as `webhook_inbox.source`. |
| `Content-Type` | `application/json` expected. |

### Validation order

1. Secret present in env? → else `503`.
2. Body ≤ 256 KiB? (Content-Length check first, then actual read) → else `413 payload_too_large`.
3. Both headers present? → else `401 invalid_signature`.
4. Timestamp parses as Unix seconds? → else `401`.
5. Timestamp within ±5 minutes of server clock? → else `401` (replay guard).
6. HMAC matches? (`hmac.compare_digest`) → else `401`.
7. Body decodes as UTF-8 JSON object? → else `400 invalid_payload`.
8. Insert into `webhook_inbox` → `202 {"id": <int>, "status": "received"}`.

### Persisted columns

`webhook_inbox` stores `source`, `payload_json`, `http_headers_json`, `signature`, `status` (default `received`), `received_at`, plus future-use `notes` / `processed_at`. Headers are filtered to a **5-name allowlist** before write:

- `content-type`
- `user-agent`
- `x-signature`
- `x-timestamp`
- `x-source`

Everything else (cookies, `x-forwarded-for`, proxy identifiers, auth headers) is dropped. Raw-header archival is deliberately not supported — audit debt is better than PHI-adjacent leakage into an operational table.

Postgres also enforces `octet_length(payload_json::text) <= 262144` as a CHECK constraint — belt and suspenders for the 256 KiB body cap.

### Python signing recipe

```python
import hmac
import json
import os
import time
from hashlib import sha256

import requests

SECRET = os.environ["WEBHOOK_INBOX_SECRET"]
URL = "https://referme.help/api/v2/webhooks/inbound"

payload = {"event": "delivery_status", "delivery_id": "abc123", "status": "sent"}
body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
ts = str(int(time.time()))
signed = f"{ts}.".encode() + body
sig = "sha256=" + hmac.new(SECRET.encode(), signed, sha256).hexdigest()

resp = requests.post(
    URL,
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": sig,
        "X-Source": "my-integration",
    },
)
resp.raise_for_status()
print(resp.json())  # {"id": 123, "status": "received"}
```

### Curl one-liner

```bash
SECRET="…" && \
BODY='{"event":"ping"}' && \
TS=$(date +%s) && \
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1) && \
curl -X POST https://referme.help/api/v2/webhooks/inbound \
     -H "Content-Type: application/json" \
     -H "X-Timestamp: $TS" \
     -H "X-Signature: sha256=$SIG" \
     -H "X-Source: ops-smoke" \
     -d "$BODY"
```

### Operational notes

- Rows accumulate until purged. A nightly sweep is planned for Phase 9 when real delivery traffic lands; until then, expect the table to grow slowly.
- The 256 KiB cap is well above any current real-world webhook (Documo / Sfax / Resend are all ≤ 8 KiB typical) but well below platform limits.
- A replay window of ±5 minutes is tight enough to defeat naive replay attacks against an unauthenticated inbox URL, wide enough to accommodate typical NTP drift between a vendor's infrastructure and Railway's.

## Versioning contract

- `v2` is stable for the lifetime of this doc. Breaking changes go to `v3`.
- New fields added to JSON / FHIR responses are **not** breaking. Consumers should tolerate unknown fields.
- New error codes are **not** breaking. Consumers should treat unknown `code` values as generic failures at the corresponding HTTP status.
- Header additions are **not** breaking.
- Removing or changing the semantics of a documented field **is** breaking and will be gated on `v3`.
