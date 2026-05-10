# Encryption posture

**Last reviewed**: 2026-05-10
**Owner**: Founder

Authoritative description of how data is protected at rest, in transit, and at the application layer. Cited from `policies/encryption.md`.

## In transit

- All public traffic terminates TLS 1.2+ at Railway's edge. TLS 1.0/1.1 disabled.
- HSTS header `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload` set on every https response (see `src/docstats/web.py::add_security_headers`).
- HTTP→HTTPS redirect handled at Railway's edge.
- Internal vendor calls (Supabase REST, Availity, Cloudmersive, Resend, Documo, EHR FHIR endpoints) all use https with cert validation enabled (`httpx` defaults).
- Webhooks: HMAC-SHA-256 signing on inbound (`/api/v2/webhooks/inbound`), Svix or Documo signing on outbound delivery callbacks, validated before payload is parsed.

## At rest

- **Supabase Postgres**: AES-256 disk-level encryption (Supabase managed; covered by their BAA at Team tier).
- **Supabase Storage** (attachments bucket): AES-256 server-side encryption (Supabase managed). Bucket private, signed-URL access only (15-minute TTL).
- **SQLite local dev DB** (`~/.local/share/docstats/docstats.db`): no application-layer encryption. Local-only, not used in production. PHI on a developer laptop is the developer's responsibility (full-disk encryption required).

## Application layer

- **EHR OAuth tokens** (`ehr_connections.access_token_enc`, `refresh_token_enc`): Fernet-encrypted with `EHR_TOKEN_KEY` (32-byte urlsafe-base64). Decryption fail-closed if env unset. See `src/docstats/ehr/crypto.py`.
- **Session cookies**: signed with `SESSION_SECRET_KEY` via `itsdangerous.TimestampSigner`. Signed but not encrypted — never put PHI in the session.
- **Passwords**: bcrypt via passlib (work factor 12 default). Never logged.
- **API tokens / webhook secrets**: stored in environment variables, never in DB or repo. Rotated on suspected compromise.

## Not yet implemented (deferred until forced by audit or threat)

- **Envelope encryption for high-sensitivity columns** (e.g., a future `patients.ssn`). Plan: KMS via Supabase Vault. Defer until first such column is needed or first auditor asks. Document the gap rather than ship an unused control.
- **Customer-managed encryption keys (CMK)**: not on roadmap. Enterprise-only feature.

## Key rotation

| Key | Rotation cadence | Procedure |
|---|---|---|
| `EHR_TOKEN_KEY` | Every 24 months, or on suspected compromise | Generate new key, decrypt-then-reencrypt all `ehr_connections` rows in a migration, update Railway env var, deploy. Old key kept available during migration window. |
| `SESSION_SECRET_KEY` | Every 24 months, or on suspected compromise | Set new value; existing sessions invalidate (users must re-login). One-line change. |
| `WEBHOOK_INBOX_SECRET` | Every 12 months | Coordinate with senders before rotation. |
| Vendor API keys (Supabase service key, Availity, Resend, Documo, Cloudmersive, EHR client secrets) | Every 12 months, or on personnel change | Rotate at vendor, update Railway env vars, redeploy. |

## Verification

- HSTS header presence: `tests/test_security_headers.py::test_hsts_emitted_when_forwarded_proto_is_https`.
- TLS 1.2+ only: `nmap --script ssl-enum-ciphers -p 443 referme.help` annually.
- Token redaction in logs: `epic._redact()` + repo-wide AST gate (`tests/test_no_phi_in_logs.py`).
