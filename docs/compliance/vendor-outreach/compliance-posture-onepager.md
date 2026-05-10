# referme.help — compliance posture (one-pager)

For prospects, partners, and security questionnaires. Last updated 2026-05-10.

## Snapshot

referme.help is operated by **Robworks Software** (US sole-proprietor). The product is a referral-management application for US healthcare clinics — patients and clinicians share clinical context, attach records, and dispatch referral packets via fax, Direct Trust, or email. PHI is processed in the normal course of operation; HIPAA Security Rule + Privacy Rule + Breach Notification Rule apply.

## What we do well today

| Control | Status |
|---|---|
| BAAs with all PHI sub-processors | In progress (target: complete by Q3 2026) — see below |
| TLS 1.2+ everywhere; HSTS preload | ✅ Live |
| Encryption at rest (AES-256, managed) | ✅ Live (Supabase) |
| Application-layer token encryption (Fernet) | ✅ Live for EHR OAuth tokens |
| Append-only audit log, 7-year retention target | ✅ Live |
| Multi-tenant scope isolation enforced at storage layer | ✅ Live; tested |
| PHI consent gate distinct from ToS, version-bumpable | ✅ Live |
| Role-based access control (6 roles, hierarchical) | ✅ Live |
| Cross-tenant guards on every admin route | ✅ Live |
| Vulnerability scanning (Trivy) on every PR | ✅ Live |
| Secret scanning (gitleaks) on every PR | ✅ Live |
| AST gate preventing PHI in log output | ✅ Live |
| Soft-delete + retention sweep for attachments (per-org TTL) | ✅ Live |
| Webhook signing (HMAC-SHA-256, 5-min replay window, 256KB cap) | ✅ Live |
| Virus scanning of uploaded attachments (Cloudmersive) | ✅ Live |

## What we're building toward

| Control | Target |
|---|---|
| All BAAs signed | Q3 2026 |
| Cross-region database backup with restoration drill | Q3 2026 |
| Quarterly access review documented | Q3 2026 (first one) |
| Annual external pen test | Funded on first enterprise deal |
| SOC 2 Type I report | Funded on first enterprise deal |
| SOC 2 Type II observation window | Begins after Type I |
| HITRUST CSF | Only if forced by an enterprise prospect |

## Sub-processors

Authoritative list maintained at [internal — share on signed NDA + BAA request]. High-level categories:

- Hosting (compute) — US-region
- Database + object storage — US-region
- Email delivery — US-region
- Fax delivery — US
- Direct Trust HISP — US
- X12 270/271/278 clearinghouse — US
- EHR aggregator — US
- Virus scanning — US
- Domain registrar + DNS

All PHI-touching sub-processors are required to sign a BAA before connection.

## Documentation available on request

- Detailed encryption posture
- Access control + provisioning policies
- Incident response policy + breach notification procedure
- Change management policy
- Vendor risk policy
- Data classification policy
- Acceptable use policy
- Risk management policy
- Business continuity / disaster recovery policy
- Latest annual risk assessment
- BAA register
- Audit log architecture

We can also walk through the application's PHI-flow in a 30-minute call.

## Honest limitations (for procurement teams that appreciate it)

- We are pre-revenue; no SOC 2 audit yet. Engaging an auditor is funded on first enterprise deal close.
- We are a one-person operation today. Single-point-of-failure risk on the founder is documented + partially mitigated; not eliminated.
- We have not yet undergone third-party penetration testing.
- We do not currently carry cyber insurance; engaging a carrier before first paying customer.

If any of the above is a hard procurement blocker, let's talk early — we can sometimes find a path (e.g., conditional BAA + verbal commitment to fund audit by [date]).

## Contact

Ryan Robson, Founder
ringo380@gmail.com (until `security@referme.help` is set up)
