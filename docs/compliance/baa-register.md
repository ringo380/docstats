# Business Associate Agreement (BAA) register

Every vendor that processes, transmits, or stores PHI on behalf of referme.help must have a signed BAA on file. This document is the authoritative index. Update on every vendor add/change/rotate.

**Last reviewed**: 2026-05-10 by Ryan Robson (founder)
**Next review**: 2026-11-10

## Status legend

- ✅ Signed — countersigned PDF on file in `~/Documents/robworks/baa/`
- 🔄 In progress — request sent, awaiting countersig
- ⛔ Not eligible — vendor doesn't offer BAA at our tier (must upgrade or replace)
- 🚫 Not applicable — vendor doesn't touch PHI

## Infrastructure & hosting

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Supabase | Postgres + Storage | 🔄 | — | Team plan required ($25/mo per project). Request sent 2026-05-10 to support@supabase.io (project uhnymifvdauzlmaogjfj); awaiting reply. |
| Railway | App hosting | 🔄 | — | BAA case-by-case. Ticket open. Migration target if declined: Fly.io Pro. |
| Cloudflare | DNS only | 🚫 | — | DNS records do not contain PHI. |
| Namecheap | Domain registrar | 🚫 | — | No PHI. |

## Email & messaging

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Resend | Transactional email | 🔄 | — | Business tier required for BAA. Currently on Pro. Request sent 2026-05-10 to support@resend.com asking for plan-tier confirmation + BAA execution path; awaiting reply. |
| Documo | Fax | 🔄 | — | BAA available on production plan. Request sent. |
| DataMotion | Direct Trust HISP | 🔄 | — | BAA bundles with HISP contract. Not yet contracted. |

## Clinical integrations

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Availity | X12 270/271/278 clearinghouse | 🔄 | — | BAA standard on Essentials API contract. |
| Redox | EHR aggregator | 🔄 | — | BAA bundles with Redox subscription. Sandbox-only currently; production needs new contract. |
| Epic | App Orchard / Showroom | ⛔ | — | Sandbox listing today; production requires Epic-side approval + BAA. Defer until first customer site requests Epic integration. |
| Cerner / Oracle Health | SMART app | ⛔ | — | Sandbox only. Same as Epic. |
| eClinicalWorks | SMART app | ⛔ | — | Sandbox only. Same as Epic. |

## Tooling

| Vendor | Service | BAA status | Effective | Notes |
|---|---|---|---|---|
| Cloudmersive | Virus scan API | 🔄 | — | Enterprise tier offers BAA. Request sent 2026-05-10 to support@cloudmersive.com. Migration path: self-hosted ClamAV if BAA gated to a tier we can't justify. |
| Anthropic | Claude API | 🔄 | — | BAA available for enterprise customers. Request sent 2026-05-10 to support@anthropic.com. Not yet on PHI path; targeting BAA-on-file before Phase 16 (AI drafting) ships. |
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

If no reply within 14 days, bump (one polite follow-up, then escalate via vendor portal / sales channel).

## Open questions

- **Railway BAA timeline**: open ticket as of 2026-05-09. If no response by 2026-06-09, evaluate Fly.io migration.
- **Supabase Storage scope**: confirm the BAA explicitly covers the `attachments` bucket (not just Postgres rows).
- **Resend Business upgrade**: trigger upgrade when (a) first paying customer signs, OR (b) we start sending anything with PHI in the body (we shouldn't).
