# Business Continuity & Disaster Recovery Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual; drill quarterly

## Purpose

Define how referme.help survives incidents that take production offline or destroy data. Maps to HIPAA §164.308(a)(7) (Contingency Plan).

## Scope

Production application stack: Railway (compute), Supabase (Postgres + Storage), Cloudflare/Namecheap (DNS), and supporting vendor services per `baa-register.md`.

## Recovery objectives

| Metric | Target | Notes |
|---|---|---|
| RTO (Recovery Time Objective) | 4 hours for full service; 1 hour for read-only | The product is not life-safety; clinics have fax + EHR fallbacks. |
| RPO (Recovery Point Objective) | 1 hour | Supabase point-in-time recovery (PITR) on Team tier. |

These are commitments to ourselves; customer SLAs (when first paying customer signs) may be tighter.

## Backup posture

### Database

- **Primary**: Supabase point-in-time recovery enabled (Team plan default; verify on plan upgrade).
- **Secondary**: weekly logical backup via `scripts/backup_to_s3.py` (TODO — not yet shipped) → S3 in `us-west-2` (separate region from Supabase prod which is typically `us-east-1`).
- **Retention**: PITR for 7 days (Supabase default at Team), weekly snapshots for 90 days, monthly snapshots for 7 years.

### Object storage (attachments)

- Supabase Storage; covered by Supabase backup.
- **Gap**: cross-region copy of attachment blobs not yet implemented. Plan: nightly sync to S3. TODO.

### Source code

- GitHub primary. Local working copies on developer laptops are de-facto secondary.
- Releases tagged at deploy time (TODO — currently we don't tag).

### Configuration

- Railway environment variables exported quarterly to `~/Documents/robworks/infra-snapshots/YYYY-QN-railway-vars.txt.gpg` (gpg-encrypted).
- DNS records exported via Namecheap export quarterly.

## Single points of failure

- **Founder**: solo operator. If incapacitated, the business stops responding. Mitigations:
  - Critical credentials shared with a designated successor (TBD; identify before first paying customer).
  - This policy binder is enough for a successor to operate the systems.
- **Supabase**: outage takes the app down. No active multi-vendor failover. Documented gap.
- **Railway**: outage takes the app down. Migration target (Fly.io) identified but not pre-deployed.
- **Cloudflare/Namecheap**: DNS outage takes the domain down. Industry-standard reliability; accept the risk.

## Disaster scenarios

### Scenario 1 — Supabase regional outage

Symptoms: app returns 5xx on all DB-touching routes.

Response:
1. Confirm via Supabase status page.
2. Communicate to customers (status page TBD; for now, in-app banner).
3. Wait. No active failover.
4. Post-mortem afterward.

### Scenario 2 — Railway outage

Symptoms: app entirely unreachable.

Response:
1. Confirm via Railway status page.
2. Wait, OR if outage is prolonged (>4 hours), execute Fly.io failover:
   - Build container locally: `fly launch --image <image>` (preconfigured `fly.toml` TODO).
   - Set env vars from local snapshot.
   - Update DNS at Namecheap to point at Fly.io app.
   - DNS propagation 5–30 minutes.
3. Restore to Railway when service resumes; deploy from main; cut DNS back.

### Scenario 3 — Database corruption / accidental delete

Symptoms: app returns wrong data; users report missing records; mass-delete observed in audit log.

Response:
1. **Stop writes immediately**: take app offline (`railway down`).
2. Identify the time of corruption.
3. Restore from PITR to a Supabase branch.
4. Compare branch vs. prod; identify lost data.
5. Coordinate with affected customers — they may need to re-enter data lost between PITR point and incident.
6. Promote branch when verified clean.
7. Resume service.

### Scenario 4 — Compromised production credentials

See `incident-response.md` runbook for credential compromise.

### Scenario 5 — Founder unavailable for >24 hours

Successor (TBD) executes from this policy binder. They have:

- Credential access (1Password vault shared item).
- This policy binder.
- Ability to communicate with customers from `support@referme.help`.
- Authority to engage outside counsel and cyber insurance.

## Drills

- **Quarterly**: at least one disaster scenario tabletop or live drill.
- **Live drill** for Scenario 3 (database restore) annually — restore to Supabase branch, verify data integrity, do not promote.
- **Tabletop** for Scenarios 1, 2, 4, 5 quarterly.

Drill log: `docs/compliance/dr-drills.md` (created on first drill).

## Communications during incidents

- **Customers**: status page TBD; for now, in-app banner + per-customer email.
- **Sub-processors**: per BAA terms.
- **Regulators**: per HIPAA Breach Notification Rule timelines (see `incident-response.md`).

## Annual test

The full BCP/DR plan is tested end-to-end at least annually. Test results documented in `docs/compliance/dr-drills.md` with deficiencies tracked to closure.

## PHI launch gate (cross-references R-011)

The platform is engineered to handle PHI but, per the pre-revenue funding policy in `docs/compliance/baa-register.md`, a subset of sub-processor BAAs are deferred until first paying customer funds the plan upgrades. **Real patient PHI must not enter production until every item below is checked:**

1. **Supabase**: Team plan active (~$599/mo) + HIPAA add-on processed + BAA countersigned and filed in `~/Documents/robworks/baa/`. Project's HIPAA Security Advisor checklist green (SSL enforcement, network restrictions, MFA on all accounts, PITR on the project carrying PHI).
2. **Resend**: Business plan active + BAA countersigned, OR the email channel is removed from the PHI path entirely (and the signed-link discipline remains in place for non-PHI account email).
3. **Cloudmersive enterprise BAA in hand**, OR replacement scanner (self-hosted ClamAV in a Railway sidecar) deployed to production with a documented test result and `VIRUS_SCAN_REQUIRED=1` still enforced fail-closed.
4. **Anthropic** BAA on file if Claude API is on the PHI path (it is not today; defer until Phase 16 ships).
5. **Documo**, **Availity** (Trading Partner Management), **Redox** (production tier), **Railway**: each at ✅ in `docs/compliance/baa-register.md`.
6. **DataMotion** (or whichever HISP we contract) BAA bundled with the signed HISP contract if Direct Trust is on the PHI path.

The first paying customer's contract MUST NOT close at "we'll be ready next week" — the upgrade-and-sign sequence above is week-long minimum (Supabase HIPAA add-on processing time alone is days). Build the timeline into the deal.

When the gate is crossed for the first time, update `baa-register.md`, `risk-assessment-YYYY.md` (R-011 status), and this section with the date and the customer name (one line; we're tracking the event, not the customer roster). Subsequent customers don't re-trigger the gate, but new BAA-touching vendors do.
