# Access Control Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

Define how identities are authenticated, what roles exist, and how PHI access is gated. Maps to HIPAA §164.312(a) and SOC 2 CC6.

## Scope

All systems handling production data: the FastAPI application (`src/docstats/`), the Supabase Postgres database, Supabase Storage, the Railway hosting console, and connected third-party tools (Availity, Documo, EHR vendor portals).

## Authentication

- **Application users** authenticate via email + bcrypt password OR GitHub OAuth. Session tokens are server-side (DB-backed `sessions` table), 32-byte urlsafe-random, rotated on every login/signup/OAuth, hard-revoked on account deletion.
- **Admin console operators** are application users with an org membership role of `admin` or `owner`. The `is_org_admin` flag is recomputed per-request from storage; stale dict flags are not trusted after role mutations.
- **Infrastructure operators** (currently the founder only) authenticate to Railway and Supabase via the vendor's own SSO + 2FA. 2FA is mandatory.
- **Service accounts** (Supabase service key, Availity client credentials, EHR client secrets) authenticate machine-to-machine. Keys live only in Railway environment variables.

## Authorization

The application enforces three orthogonal gates on every PHI-touching route:

1. **Authentication** — `require_user` dependency raises if no valid session.
2. **PHI consent** — `require_phi_consent` dependency raises if the user hasn't accepted the current PHI consent version (`CURRENT_PHI_CONSENT_VERSION` in `src/docstats/phi.py`).
3. **Scope** — every PHI-owning row carries either `scope_user_id` or `scope_organization_id`; queries are filtered via `scope_sql_clause` (SQLite) or `_apply_scope` (Postgres). Anonymous callers raise `ScopeRequired`.

For org-scoped resources, the membership role hierarchy is `read_only < staff < clinician < coordinator < admin < owner`. Role checks use `has_role_at_least()`.

## Roles

| Role | Can read PHI | Can mutate PHI | Can transition referral status | Can administer org |
|---|---|---|---|---|
| read_only | yes | no | no | no |
| staff | yes | yes | yes (subject to STATUS_TRANSITIONS) | no |
| clinician | yes | yes | yes | no |
| coordinator | yes | yes | yes | no |
| admin | yes | yes | yes | yes |
| owner | yes | yes | yes | yes (cannot be demoted by another admin) |

## Just-in-time admin elevation

Not currently implemented. Documented gap. Adding when a SOC 2 auditor requests it or when org headcount > 5 makes persistent elevation a real risk. Until then, persistent admin role is acceptable for the founder + small clinic pilots.

## Quarterly access review

Performed by the org owner. Procedure:

1. Pull current memberships: `GET /admin/members` per org.
2. For each member, confirm they still need their role. Demote or remove as appropriate.
3. Pull last 90 days of audit events for any actor with `admin` or `owner` role; spot-check 5 events per actor.
4. Record the review in `docs/compliance/access-reviews/YYYY-QN.md`.

## Session lifecycle

- Sessions expire 7 days from creation (`max_age=604800` on the cookie + DB-side `expires_at`).
- `last_seen_at` is touched at most every 5 minutes to limit DB write churn.
- Sessions revoke on logout, account deletion, or explicit admin revoke.

## Failed authentication

The application does not currently rate-limit login attempts at the application layer; Railway's edge layer absorbs trivial brute-force. **Open gap**: add per-IP login throttling before scaling beyond pilot.

## Departing personnel

When (not if) headcount reaches > 1:

1. Disable Railway access within 1 business day.
2. Disable Supabase access within 1 business day.
3. Revoke any vendor portal logins they had.
4. Demote and then remove their app membership.
5. Rotate any shared secrets they handled.
6. Confirm in writing within 7 days.
