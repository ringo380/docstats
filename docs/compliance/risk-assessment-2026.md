# Annual Risk Assessment — 2026

**Conducted**: 2026-05-10
**Conducted by**: Ryan Robson (founder)
**Scope**: All systems and processes handling PHI for referme.help, as of 2026-05-10
**Methodology**: per `policies/risk-management.md`
**Maps to**: HIPAA §164.308(a)(1)(ii)(A) — Security Management Process / Risk Analysis

## Executive summary

Posture is **adequate for pilot deployments** and below the threshold for enterprise hospital RFPs. Top three exposures:

1. Single-operator org (founder concentration risk).
2. No SOC 2 / external attestation yet.
3. BAAs in progress, not all signed.

All three are tracked with explicit treatment plans below. None block pilot work.

## Risk register

### R-001: Founder concentration risk

- **Asset**: Operational continuity of the entire service.
- **Threat**: Founder incapacity, illness, or unavailability for >72 hours.
- **Vulnerability**: No second operator with credentials or runbook familiarity.
- **Likelihood**: Medium (statistical baseline for any individual over 12 months).
- **Impact**: High (service degrades to "nobody home"; customer support stops; security incidents go unhandled).
- **Risk**: HIGH.
- **Treatment**: Identify a successor with credential vault access by 2026-Q3. Document full runbook (this binder is the start). Consider a small retainer with a fractional ops contractor.
- **Owner**: Founder.
- **Due**: 2026-09-30.
- **Status**: Open.

### R-002: No SOC 2 attestation

- **Asset**: Sales pipeline / enterprise prospects.
- **Threat**: Lost deals due to procurement compliance requirements.
- **Vulnerability**: No SOC 2 Type II report; no Type I either.
- **Likelihood**: High (most enterprise hospital systems require SOC 2).
- **Impact**: Medium (lost revenue; not a security exposure per se).
- **Risk**: MEDIUM (it's a business risk, not a security risk; prioritized lower than R-001 and R-003).
- **Treatment**: Accept until first enterprise deal funds the engagement. Until then, lean on the compliance one-pager + this binder.
- **Owner**: Founder.
- **Due**: Re-evaluate quarterly.
- **Status**: Accepted.

### R-003: BAAs not all signed — paid BAAs deferred until revenue

- **Asset**: Legal posture for PHI processing.
- **Threat**: HIPAA enforcement action; customer breach-of-contract claims.
- **Vulnerability**: Per `baa-register.md`, several vendors gate BAA execution to a paid plan we have not funded (Supabase Team ≈ $7–10k/year; Resend Business; Cloudmersive enterprise). Per the explicit pre-revenue funding policy, those are 💸 (deferred until revenue).
- **Likelihood**: Low pre-revenue (no real PHI flowing); becomes High the moment a paying customer onboards if upgrades haven't been funded simultaneously.
- **Impact**: High.
- **Risk**: HIGH if PHI flows before BAAs are signed; LOW while we strictly enforce the no-real-PHI gate below.
- **Treatment**: **Partial mitigation + accepted residual.** Pursue all free / form-based / already-bundled BAAs to closure (Anthropic, Documo, Availity Trading Partner, Railway ticket). Paid-tier-gated BAAs (Supabase, Resend, Cloudmersive) explicitly deferred until first paying customer funds the upgrade. Hard gate: **no real patient PHI in production until 100% of PHI-touching vendors are ✅** — sandbox and synthetic-data work continues unrestricted. See R-011 for the gate itself.
- **Owner**: Founder.
- **Due**: Re-evaluate at first revenue event.
- **Status**: Partially accepted (deferred), partially open (free-tier BAAs still actively pursued).

### R-004: No external pen test

- **Asset**: Application security posture.
- **Threat**: Undiscovered vulnerability in OAuth flows, scope enforcement, or admin gates exploited by an attacker.
- **Vulnerability**: Application has been self-reviewed but never adversarially tested by an external party.
- **Likelihood**: Medium.
- **Impact**: High (PHI exposure).
- **Risk**: HIGH.
- **Treatment**: Mitigate. Engage Cure53 / Bishop Fox / equivalent for a focused 1-week assessment of OAuth flows + admin console + scope enforcement. Funded on first enterprise deal close. Until then, run automated scans + advisor reviews.
- **Owner**: Founder.
- **Due**: First enterprise deal + 60 days.
- **Status**: Accepted (interim).

### R-005: No cross-region database backup

- **Asset**: Patient data integrity.
- **Threat**: Supabase regional outage with data loss; or accidental destructive operation that PITR can't recover.
- **Vulnerability**: PITR is single-region. No cross-region snapshot pipeline live.
- **Likelihood**: Low (Supabase has not had a destructive regional outage to date).
- **Impact**: High (data loss).
- **Risk**: MEDIUM.
- **Treatment**: Mitigate. Ship `scripts/backup_to_s3.py` (weekly logical dump → S3 us-west-2). Tracked in Phase 15 plan B.4.
- **Owner**: Founder.
- **Due**: 2026-Q3.
- **Status**: Open.

### R-006: Logged-in session theft via XSS

- **Asset**: User accounts.
- **Threat**: Stored or reflected XSS bypasses session protections.
- **Vulnerability**: We use Jinja autoescape (default on); no Content-Security-Policy header set yet.
- **Likelihood**: Low (autoescape catches the common cases).
- **Impact**: High per affected user.
- **Risk**: MEDIUM.
- **Treatment**: Mitigate. Add CSP header to security middleware (next sprint). Audit all `| safe` filter uses in templates. Add a CI test for `| safe` callsites with allowlist.
- **Owner**: Founder.
- **Due**: 2026-Q3.
- **Status**: Open.

### R-007: Login brute force

- **Asset**: User accounts.
- **Threat**: Credential stuffing or password brute force at the login endpoint.
- **Vulnerability**: No application-layer rate limiting on login attempts; only Railway edge.
- **Likelihood**: Medium (generic threat).
- **Impact**: Medium (per-account; depends on bcrypt cost defending the stolen hash, which is high).
- **Risk**: MEDIUM.
- **Treatment**: Mitigate. Add per-IP and per-account login throttling. Track in Phase 15 follow-up.
- **Owner**: Founder.
- **Due**: 2026-Q3.
- **Status**: Open.

### R-008: PHI accidentally logged

- **Asset**: All PHI flowing through the application.
- **Threat**: A future code change interpolates a PHI field into a logger call, landing in Railway log retention.
- **Vulnerability**: Easy mistake to make in code review.
- **Likelihood**: Medium (across many PRs).
- **Impact**: Medium (depends on log retention scope).
- **Risk**: MEDIUM.
- **Treatment**: Mitigated. AST CI gate (`tests/test_no_phi_in_logs.py`) prevents the most common pattern (f-strings in logger calls). Residual: positional args could still carry PHI. Layer a runtime redactor before scaling.
- **Owner**: Founder.
- **Due**: Layer a runtime redactor when first auditor or first incident demands it.
- **Status**: Mitigated.

### R-009: Webhook replay attack

- **Asset**: Inbound webhook endpoint (`/api/v2/webhooks/inbound`).
- **Threat**: Captured webhook replayed.
- **Vulnerability**: HMAC alone doesn't prevent replay; timestamp is required.
- **Likelihood**: Low.
- **Impact**: Low (no handlers wired yet).
- **Risk**: LOW.
- **Treatment**: Mitigated. ±5-minute replay window enforced by signature scheme; raw payload stored in `webhook_inbox` for forensics.
- **Owner**: Founder.
- **Due**: N/A.
- **Status**: Mitigated.

### R-011: PHI launch gate — production-PHI block until paid-tier BAAs are funded

- **Asset**: HIPAA-compliant production posture.
- **Threat**: A paying customer (or pilot organization) is onboarded into production and starts pushing real PHI through Supabase / Resend / Cloudmersive **before** those vendors' BAAs are signed, because the upgrades were deferred for cost reasons.
- **Vulnerability**: Per R-003, paid-tier-gated BAAs are deferred until revenue. If onboarding outpaces upgrade execution, the gap is a HIPAA violation on day 1 of revenue.
- **Likelihood**: Medium — pre-revenue today, but the whole point of the deferral is that we expect to need to flip the switch quickly when a deal closes.
- **Impact**: Critical (regulatory + contractual).
- **Risk**: HIGH at first revenue event; LOW today.
- **Treatment**: Mitigate via a hard pre-launch checklist. Before the **first** real-PHI byte enters production:
  1. Supabase Team plan active + HIPAA add-on processed + BAA countersigned and filed.
  2. Resend Business plan active + BAA countersigned (or PHI-channel removed if BAA can't be executed in time).
  3. Cloudmersive enterprise BAA in hand, OR replacement scanner (ClamAV sidecar) in production with a documented test result.
  4. All other 🔄 vendors moved to ✅.
  5. Update `baa-register.md` + this risk row + the compliance one-pager in the same PR that launches the customer.

  The checklist lives in `docs/compliance/policies/business-continuity.md` (new "PHI launch gate" section to be added) and gets re-verified before each new BAA-touching vendor is added.
- **Owner**: Founder.
- **Due**: Before first paying customer's production go-live.
- **Status**: Open (no current customer; gate not yet tripped).

### R-010: Compromised vendor key

- **Asset**: Whatever the key authenticates to (Supabase, Availity, Documo, Resend, EHR client secrets).
- **Threat**: Key leaked via mis-commit, misconfigured CI, or vendor portal compromise.
- **Vulnerability**: Keys in Railway env; if Railway compromise, all keys exposed.
- **Likelihood**: Low.
- **Impact**: Critical (full backend access for the affected vendor).
- **Risk**: HIGH.
- **Treatment**: Mitigate. Rotate keys annually per `encryption.md`. Gitleaks gate prevents commits. Document immediate-rotation runbook (already in `incident-response.md`).
- **Owner**: Founder.
- **Due**: First annual rotation: 2027-Q1.
- **Status**: Open (rotation pending).

## Treatment summary

| Risk | Status | Due |
|---|---|---|
| R-001 Founder concentration | Open | 2026-09-30 |
| R-002 No SOC 2 | Accepted | Re-evaluate quarterly |
| R-003 BAAs incomplete (paid-tier deferred) | Partially accepted | Re-evaluate at first revenue |
| R-004 No pen test | Accepted (interim) | On first enterprise deal |
| R-005 Cross-region backup (Supabase Team can't do it natively → pg_dump → S3 cron) | Open | 2026-Q3 |
| R-006 No CSP header | Mitigated | — |
| R-007 No login throttling | Mitigated | — |
| R-008 PHI in logs | Mitigated | — |
| R-009 Webhook replay | Mitigated | — |
| R-010 Compromised vendor key | Open (rotation pending) | 2027-Q1 |
| R-011 PHI launch gate (paid-BAA must be funded before first paying customer go-live) | Open | Before first paying customer's production go-live |

## Sign-off

Risk assessment reviewed and accepted by:

**Ryan Robson** — Founder, Robworks Software — 2026-05-10
