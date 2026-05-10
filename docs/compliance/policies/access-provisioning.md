# Access Provisioning Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

Define how access is granted, modified, and removed across all systems holding production data. Maps to HIPAA §164.308(a)(3) and SOC 2 CC6.2.

## Onboarding (new app users)

**Self-service signup** is the primary path:

1. User signs up via email/password or GitHub OAuth.
2. Account starts with `account_type='patient'` by default. PHI consent must be accepted before any PHI route is reachable.
3. Clinicians upgrade via `/auth/upgrade-to-clinician`, providing NPI + license. NPI is verified against NPPES + OIG LEIE; verdict written to `clinician_verification_status`.
4. Clinicians joining an existing org receive an invitation token via email; redemption auto-grants the role specified in the invitation (subject to `_can_grant_role` — granters can only grant ≤ their own role).

**No "ad-hoc" account creation by admins** — every account is owner-of-its-credentials. Org admins can invite, never impersonate.

## Role grant criteria

| Role | Granted to | Granter |
|---|---|---|
| read_only | Auditors, observers | admin or higher |
| staff | Schedulers, intake | admin or higher |
| clinician | Verified clinicians | admin or higher |
| coordinator | Care coordinators | admin or higher |
| admin | Org administrators | owner only |
| owner | Org founder + designees | owner only; sole-owner demotion blocked |

The application enforces granter-≥-grantee at the storage layer (`_can_grant_role`).

## Role modification

- Self-demotion blocked when sole owner or sole admin (would orphan the org).
- Role changes audited as `admin.member.role_change`.
- Demotion takes effect immediately; the next request from that user reflects new role.

## Offboarding (app users)

User-initiated:
- `/profile/delete` revokes all sessions, soft-deletes patients/referrals scoped to the user, hard-deletes user row. `delete_user` returns storage refs for blob cleanup.
- Audit `user.account_deleted` recorded **before** the row is deleted (FK constraint).

Admin-initiated:
- `/admin/members/{user_id}/remove` removes membership but does not delete the user account. The user retains app access but loses access to org-scoped resources.

## Infrastructure access (Railway, Supabase, vendor portals)

Currently the founder is the only operator. Procedures for future headcount:

1. **Granting**: founder creates account at vendor; documents in a private "infra access ledger" (one entry per person per system).
2. **2FA mandatory**: every infra account must have 2FA enabled. Verify on grant.
3. **Quarterly review** alongside the access review (`access-control.md`). For each operator, confirm they still need each system.
4. **Removal**: within 1 business day of departure.

## Service accounts / API keys

- Created via vendor portal; stored only in Railway environment variables.
- Rotated annually (see `encryption.md` key rotation table) or on suspected compromise.
- Never committed to repo; gitleaks CI gate prevents accidental commits.
- Documented in `docs/compliance/baa-register.md` (one row per vendor).

## Audit

- App access events: `audit_events` table, append-only, 7-year retention.
- Infra access events: vendor's own audit log (Railway, Supabase, GitHub). Reviewed quarterly during access review.
