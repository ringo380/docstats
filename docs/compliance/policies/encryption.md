# Encryption Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

State the cryptographic posture for protecting data in transit, at rest, and at the application layer. Maps to HIPAA §164.312(a)(2)(iv) and §164.312(e)(2)(ii) (technical safeguards — encryption).

This policy is the normative source. Operational detail and current state live in `docs/compliance/encryption.md`.

## Standards

### Cryptographic algorithms

| Use | Required minimum | Currently used |
|---|---|---|
| TLS | TLS 1.2 with strong cipher suites; TLS 1.3 preferred | TLS 1.3 (Railway edge) |
| Symmetric data encryption | AES-128-GCM minimum; AES-256-GCM preferred | AES-256 (Supabase disk; Fernet for app-layer) |
| Hashing (passwords) | bcrypt cost ≥10, argon2id, or scrypt | bcrypt cost 12 (passlib default) |
| Hashing (data integrity) | SHA-256 minimum | SHA-256 (HMAC for webhooks) |
| Random | OS-provided CSPRNG | `secrets` module (Python) |

### Prohibited

- MD5, SHA-1 for security purposes (collision risk; SHA-1 OK for non-security checksums only).
- DES, 3DES.
- RC4.
- TLS 1.0, TLS 1.1.
- Custom / homegrown cryptographic primitives. No exceptions.

## Requirements

### In transit

- All public-facing traffic must use TLS 1.2+.
- HSTS must be set with `max-age ≥ 31536000` (1 year), `includeSubDomains`, and `preload`.
- Internal vendor calls (Supabase, EHR FHIR endpoints, payment processors, etc.) must use TLS with cert validation.
- Webhooks must be authenticated (HMAC-signed inbound, vendor-specific signing for outbound callbacks).

### At rest

- Production database: managed encryption-at-rest enabled (Supabase default).
- Production object storage: managed encryption-at-rest enabled (Supabase default).
- Backup snapshots: encrypted at rest in destination (S3 SSE).
- Local dev databases: developer's responsibility; full-disk encryption required on the device (per `acceptable-use.md`).

### Application layer

- EHR OAuth tokens: encrypted with Fernet (`EHR_TOKEN_KEY`); decrypt fail-closed if env unset.
- Session cookies: signed with `SESSION_SECRET_KEY`; never contain PHI.
- Future high-sensitivity columns (e.g., SSN if added): envelope encryption via KMS. Document the gap until needed.

### Key management

- Keys stored in Railway environment variables, never in repo.
- Keys rotated per the schedule in `docs/compliance/encryption.md`.
- Old keys retained during rotation windows for re-encryption migrations.
- Compromised keys rotated immediately + incident response per `incident-response.md`.

## Verification

- HSTS header presence: CI test `tests/test_security_headers.py`.
- TLS posture: external scan (e.g., `nmap --script ssl-enum-ciphers`) annually.
- No PHI in logs: CI gate `tests/test_no_phi_in_logs.py`.
- No credentials in repo: CI gate (gitleaks).
