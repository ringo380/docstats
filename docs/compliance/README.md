# Compliance documentation

Living binder of policies, control evidence, and vendor BAAs for referme.help (operated by Robworks Software).

This directory exists so that:

1. A clinic-side compliance officer evaluating us can read a coherent, written posture.
2. A future SOC 2 / HITRUST auditor has a single place to pull evidence from.
3. We have a record of decisions made (and dates) so controls don't silently drift.

Reviewed annually by the founder until headcount > 1, then by a designated security officer.

## Layout

```
docs/compliance/
├── README.md                       this file
├── baa-register.md                 every vendor + BAA status + scope
├── encryption.md                   encryption posture (at-rest, in-transit, app-layer)
├── dr-runbook.md                   disaster recovery procedure
├── dr-drills.md                    log of executed DR drills
├── risk-assessment-2026.md         annual HHS OCR §164.308(a)(1) risk assessment
├── policies/
│   ├── access-control.md
│   ├── access-provisioning.md
│   ├── acceptable-use.md
│   ├── business-continuity.md
│   ├── change-management.md
│   ├── data-classification.md
│   ├── encryption.md               (consolidated technical detail)
│   ├── incident-response.md
│   ├── risk-management.md
│   └── vendor-risk.md
└── vendor-outreach/
    ├── baa-request-template.md
    └── compliance-posture-onepager.md
```

## Source materials

Policy structure adapted from the [JupiterOne security-policy-templates](https://github.com/JupiterOne/security-policy-templates) (open-source SOC 2 / HIPAA library, MIT licensed). Trimmed to what a solo-founder, pre-headcount org actually does — no aspirational controls.

## Status: lean-profile pre-revenue

Per the active Phase 15 plan (`.claude/plans/2026-05-09_phase-15-hipaa-baa-soc2.md`), this binder represents the **lean** execution profile: real BAAs, real policies, real engineering controls — no SOC 2 audit, no Vanta, no third-party validation. Sufficient for closing small clinic pilots; insufficient for enterprise hospital RFPs that hard-require SOC 2 Type II. Switch profiles once revenue or a specific deal funds the audit work.
