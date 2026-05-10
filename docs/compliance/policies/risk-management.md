# Risk Management Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

Define how security and privacy risks are identified, assessed, treated, and tracked. Maps to HIPAA §164.308(a)(1)(ii)(A) (Risk Analysis) and §164.308(a)(1)(ii)(B) (Risk Management).

## Scope

All systems and processes handling PHI or supporting the production application.

## Risk-assessment methodology

Annual formal risk assessment. Per-asset analysis using:

1. **Threat identification**: what could go wrong? Drawn from HIPAA Security Rule, OWASP Top 10, vendor advisories, and incident history.
2. **Vulnerability identification**: where are we exposed? Drawn from CI scans (Trivy, gitleaks), code review findings, audit results.
3. **Likelihood**: low / medium / high. Calibrated on industry incident data + our own history.
4. **Impact**: low / medium / high. Measured in (a) count of records exposed, (b) regulatory penalty exposure, (c) reputational harm, (d) cost to remediate.
5. **Risk = Likelihood × Impact**.
6. **Treatment**: mitigate / accept / transfer / avoid. Document rationale.

Output: `docs/compliance/risk-assessment-YYYY.md` (annual).

## Continuous risk activities

Between annual assessments:

- **CI gates**: Trivy + gitleaks + log-redaction + tests run on every PR. Findings triaged within 7 days for HIGH/CRITICAL.
- **Vendor watch**: subscribe to security advisories from Supabase, Railway, Resend, Documo, Cloudmersive, Anthropic, all EHR vendors. Triage within 7 days.
- **Quarterly access review** (per `access-control.md`).
- **Quarterly DR drill** (per `business-continuity.md`).
- **Incident retrospectives** feed into the next annual assessment.

## Risk register

Currently maintained inline in `docs/compliance/risk-assessment-2026.md`. Each entry has:

- Risk ID (sequential).
- Description.
- Asset(s) affected.
- Threat / vulnerability.
- Likelihood, impact, calculated risk.
- Treatment decision + rationale.
- Owner + due date (for mitigations).
- Status.

## Treatment thresholds

- **HIGH risk**: must be mitigated or formally accepted by the founder within 90 days.
- **MEDIUM risk**: must be mitigated or formally accepted within 12 months.
- **LOW risk**: tracked but no required action.

"Formally accepted" means the founder has signed a written acknowledgement that the residual risk is understood and accepted as a business decision. Examples of acceptable accepted risks: SOC 2 audit deferral pre-revenue; persistent admin role pre-headcount.

## Escalation

Risks that exceed acceptance thresholds require:

1. Stop introducing new exposure (e.g., halt new customer onboarding).
2. Engage outside expertise if internal capacity insufficient.
3. Reassess monthly until reduced below threshold.

## Documentation

All risk-management decisions are written down. Verbal-only decisions don't count for compliance and won't survive audit.
