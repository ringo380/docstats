# Vendor Risk Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

Define how third-party services (sub-processors) are evaluated, contracted with, monitored, and removed. Maps to HIPAA §164.308(b)(1) (Business Associate Contracts) and SOC 2 CC9.2.

## Scope

Any service that processes, transmits, stores, or could incidentally encounter PHI on behalf of referme.help. Authoritative list: `docs/compliance/baa-register.md`.

## Vendor evaluation criteria

Before connecting any vendor that could touch PHI:

1. **BAA available?** If no, the vendor is disqualified for PHI paths. Document the decision and pick another vendor.
2. **Compliance posture**: SOC 2 Type II report or equivalent (HITRUST, ISO 27001) preferred. Pre-revenue tolerance: a vendor with a credible self-attested HIPAA compliance posture + BAA is acceptable for pilots.
3. **Data residency**: US-only for production. Vendors that route data through other regions need explicit acknowledgement (e.g., Cloudflare's edge is global by design — but DNS records are not PHI).
4. **Sub-processor list**: vendor publishes their own sub-processors? Review.
5. **Breach history**: search for recent breaches; recent serious incidents disqualify unless there's evidence of meaningful remediation.
6. **Trial / sandbox path**: prefer vendors with sandbox tiers so we can integrate without committing PHI.
7. **Exit cost**: how do we get our data out if we leave?

## Tiering

| Tier | Definition | Examples |
|---|---|---|
| Critical | Loss of vendor halts the product or breaches PHI | Supabase, Railway, Documo, EHR vendors |
| Important | Loss degrades a feature but product continues | Resend, Cloudmersive, Availity |
| Convenience | Loss has no production impact | GitHub (source hosting), Namecheap (DNS — DNS portable), Mapbox |

Critical vendors require: BAA + documented exit plan + monitored uptime SLA.

## BAA execution

1. Request BAA from vendor (template: `vendor-outreach/baa-request-template.md`).
2. Review BAA terms before signing — particularly around:
   - Sub-processor disclosure obligations.
   - Breach notification timeline (we expect ≤24 hours; HIPAA-required is 60 days but pilot customers will demand faster).
   - Data return / deletion on termination.
   - Liability caps (push back on aggressive caps for critical vendors).
3. Sign + countersign. Store countersigned PDF locally (`~/Documents/robworks/baa/`).
4. Update `baa-register.md` with effective date.

## Ongoing monitoring

- **Quarterly**: review `baa-register.md` for stale entries.
- **Annually**: re-confirm each vendor's compliance posture (request fresh SOC 2 report if applicable).
- **On vendor incident**: if a sub-processor reports a security incident affecting our service, treat as P1 incident per `incident-response.md`. Determine whether PHI was in scope.
- **On vendor pricing/plan change**: re-confirm BAA still applies at the new tier (some vendors gate BAAs to specific plans).

## Sub-processor changes

If a critical vendor adds a new sub-processor that touches our data, we must:

1. Be notified per the BAA.
2. Evaluate the sub-processor against this policy.
3. Have a meaningful right to object (typically 30 days notice).

Vendor BAAs without sub-processor disclosure clauses are a red flag — push back.

## Termination

When ending a vendor relationship:

1. Migrate data per the vendor's exit procedure.
2. Confirm data deletion in writing (per BAA termination clauses, this is required within a vendor-specified window — typically 30 days).
3. Rotate any shared secrets.
4. Remove the vendor from `baa-register.md`.
5. Remove SDK / dependency / configuration from the codebase.

## Open vendor decisions

- **Cyber insurance carrier**: not currently engaged. Engage before first paying customer. Coverage targets: PHI breach response costs, regulatory fines, business interruption.
- **Outside legal counsel for HIPAA**: not currently engaged. Engage before first paying customer.
- **Compliance automation platform** (Vanta / Drata / Secureframe): deferred per Phase 15 lean profile. Re-evaluate when SOC 2 audit becomes a deal blocker.
