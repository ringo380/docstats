# Business Associate Agreement (BAA) register

Every vendor that processes, transmits, or stores PHI on behalf of referme.help must have a signed BAA on file. This document is the authoritative index. Update on every vendor add/change/rotate.

**Last reviewed**: 2026-05-10 by Ryan Robson (founder)
**Next review**: 2026-11-10

## Status legend

- ✅ Signed — countersigned PDF on file in `~/Documents/robworks/baa/`
- 🔄 In progress — request sent, awaiting countersig
- 💸 Cost-gated, deferred until revenue — vendor BAA requires a paid plan upgrade we cannot fund pre-revenue. Re-engage when first paying customer signs.
- ⛔ Not eligible — vendor doesn't offer BAA at our tier (must upgrade or replace)
- 🚫 Not applicable — vendor doesn't touch PHI

## Funding policy

**Pre-revenue, the only BAAs we pursue are those that are free, form-based, or bundled into a paid plan we already use.** Any BAA gated to a plan upgrade we'd have to pay for is marked 💸 and deferred until first paying customer closes. Real PHI cannot flow in production until every PHI-touching vendor in this register is ✅.

## Infrastructure & hosting

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Supabase | Postgres + Storage | 💸 | — | Confirmed 2026-05-11 by AJ (Growth team): BAA requires **Team plan at $599/mo** + HIPAA add-on (separate quote required) + PITR add-on (project-level). Realistic TCO ≈ $7–10k/year. BAA covers Postgres + Storage in a single agreement (no addendum for the `attachments` bucket). Cross-region snapshot backups are Enterprise-only — kills R-005 mitigation on Team. **Deferred until first paying customer funds the Team upgrade.** Reply thread alive (ticket SU-373598 was auto-closed; reopen by replying when ready). |
| Railway | App hosting | 🔄 | — | BAA case-by-case. Ticket open. Migration target if declined: Fly.io Pro. |
| Cloudflare | DNS only | 🚫 | — | DNS records do not contain PHI. |
| Namecheap | Domain registrar | 🚫 | — | No PHI. |

## Email & messaging

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Resend | Transactional email | 💸 | — | 2026-05-11 reply from Brian (Customer Success) answered sub-processor list (resend.com/legal/subprocessors) + breach notification (72h via email) but **did not address the BAA itself**. BAA is gated to the Business plan ($20/mo upgrade from Pro). **Deferred until first paying customer.** Mitigation while deferred: keep the "no PHI in email bodies, signed-link pattern only" discipline so a transient BAA gap doesn't expose PHI through this channel. |
| Documo | Fax | 💸 | — | 2026-05-12 clarification: no Documo signup yet — earlier register entry incorrectly assumed an existing paid account. Documo's free tier doesn't include BAA, and we have no paid plan to bundle one into, so this falls under the "free-or-included only" funding policy. **Production fax delivery is not active.** Re-engage when first paying customer funds the Documo signup + BAA. Existing ticket #137511 will close on its own — we won't be replying with an account number. |
| DataMotion | Direct Trust HISP | 🔄 | — | BAA bundles with HISP contract. Not yet contracted. |

## Clinical integrations

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Availity | X12 270/271/278 clearinghouse | 🔄 | — | 2026-05-10 email to support@availity.com bounced (address no longer in service). 2026-05-11: discovered existing Developer Portal account already in place with the `referme-phase11` app on the Demo Plan. Submitted Contact Sales form at availity.com/contact-sales requesting Standard Plan contracting + BAA template from Trading Partner Management. Also registered a parallel Essentials Billing Service account that day; awaiting routing decision from Availity. |
| Redox | EHR aggregator | 🔄 | — | BAA bundles with Redox subscription. Sandbox-only currently; production needs new contract. |
| Epic | App Orchard / Showroom | ⛔ | — | Sandbox listing today; production requires Epic-side approval + BAA. Defer until first customer site requests Epic integration. |
| Cerner / Oracle Health | SMART app | ⛔ | — | Sandbox only. Same as Epic. |
| eClinicalWorks | SMART app | ⛔ | — | Sandbox only. Same as Epic. |

## Tooling

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Cloudmersive | Virus scan API | 💸 | — | Request sent 2026-05-10. No reply yet. BAA gated to enterprise tier (unknown cost; likely $$$). **Deferred until first paying customer** unless reply quotes a tier we can fund. **Pre-funding mitigation: migrate to self-hosted ClamAV in a Railway sidecar** — same fail-closed posture, $0 cost, no BAA needed. Schedule this swap before any real-PHI go-live regardless of Cloudmersive's reply. |
| Anthropic | Claude API | 🔄 | — | 2026-05-10: Fin AI Agent routed to Anthropic Privacy Team (conversation 215474247994248); awaiting human reply. Anthropic publishes a BAA-request form (not gated to enterprise contract for the request itself). Keep pursuing — if BAA is form-based and free, qualifies under the funding policy. Not yet on PHI path; targeting BAA-on-file before Phase 16 (AI drafting) ships. |
| GitHub | Source hosting | 🚫 | — | No PHI in repo. CLAUDE.md explicitly excludes patient data from commits. |
| Sentry | Error tracking | 🚫 | — | Not currently in use. If added, must be Business tier with BAA — or self-hosted. |

## Workflow

1. **New vendor**: do not connect anything that could touch PHI until BAA status is at least 🔄. Update this register on the same PR as the vendor wiring.
2. **BAA signed**: move row to ✅, fill Effective date, file countersigned PDF locally.
3. **Annual review** (every November): re-confirm all ✅ rows are still valid; expired BAAs go back to 🔄.
4. **Vendor termination**: when removing a vendor, also remove the row from this register and request data deletion confirmation under the BAA's termination clauses.

## Outreach log

Append-only — every BAA-related send recorded here for audit trail.

| Date | Vendor | To | Subject | Status |
|---|---|---|---|---|
| 2026-05-10 | Resend | support@resend.com | Business Associate Agreement (BAA) request — Robworks Software / referme.help | Awaiting reply |
| 2026-05-10 | Cloudmersive | support@cloudmersive.com | Business Associate Agreement (BAA) request — Robworks Software / referme.help | Awaiting reply |
| 2026-05-10 | Anthropic | support@anthropic.com | Business Associate Agreement (BAA) request for Claude API — Robworks Software / referme.help | Awaiting reply |
| 2026-05-10 | Supabase | support@supabase.io | Business Associate Agreement (BAA) request — Robworks Software / referme.help (project uhnymifvdauzlmaogjfj) | Awaiting reply |
| 2026-05-10 | Documo | support@documo.com | Business Associate Agreement (BAA) confirmation — Robworks Software / referme.help | **Closed** — vendor asked for account number; we don't have a Documo signup (free tier doesn't include BAA, no paid plan to bundle into). Ticket #137511 will close on its own; defer until first paying customer funds the signup. |
| 2026-05-10 | Availity | support@availity.com | Business Associate Agreement (BAA) confirmation — Robworks Software / referme.help | **Bounced** — address no longer in service. Switching to Essentials portal Support Case. |
| 2026-05-11 | Availity | availity.com/contact-sales (Marketo form → Trading Partner Management) | Standard Plan contracting + BAA execution — referme.help (existing Demo Plan app `referme-phase11`) | Submitted. Awaiting routing to Trading Partner Management. |

If no reply within 14 days, bump (one polite follow-up, then escalate via vendor portal / sales channel).

## Open questions

- **Railway BAA timeline**: open ticket as of 2026-05-09. If no response by 2026-06-09, evaluate Fly.io migration.
- **Supabase Storage scope**: ✅ resolved 2026-05-11 — single BAA covers both Postgres and Storage; no addendum needed.
- **Resend Business upgrade**: trigger upgrade when first paying customer signs.
- **Round 1 bounce sweep**: ✅ done 2026-05-12 — only Availity bounced (already handled via Contact Sales form).
- **Cross-region backup (R-005)**: NOT available on Supabase Team; Enterprise only. Need a different mitigation path — likely a nightly `pg_dump` → S3 us-west-2 via a Railway cron, which is free-ish (S3 storage cost on a few-MB-per-night dump is negligible). Postpone the operational work until the broader Phase 15 timeline catches up.
- **Outgoing email footer**: ryan@robworks.info has a sticky auto-append footer pitching Lessoncraft EdTech + FERPA/COPPA. Confusing for vendors when used for healthcare BAA outreach. Fix in Gmail settings → General → Signature, or scrub before sending future BAA replies.
