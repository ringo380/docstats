# BAA Request — email template

Use as a starting point. Customize with vendor name + plan tier + product context. Send from `support@referme.help` or `legal@referme.help`.

---

**Subject**: Business Associate Agreement (BAA) request — Robworks Software / referme.help

Hello [Vendor] team,

I'm Ryan Robson, founder of Robworks Software, which operates **referme.help** — a referral-management application for US healthcare clinics. We're using [Vendor product] for [specific use case, e.g., "transactional email delivery", "Postgres + Storage hosting", "fax delivery"].

Because our application processes Protected Health Information (PHI) governed by HIPAA, we need a signed Business Associate Agreement (BAA) on file with [Vendor] before routing any PHI through your service.

A few specifics:

- **Account / org**: [your account ID or email]
- **Plan**: [current plan tier] (please confirm whether BAA execution requires upgrading to a different tier, and the cost delta if so)
- **Scope**: PHI elements that will pass through your service include [specific list — e.g., patient names, dates of service, FHIR identifiers, attached clinical documents]
- **Sub-processors**: please confirm whether your sub-processors are also BAA-covered, and where I can find your current sub-processor list

Could you send the BAA template you typically use, or your standard execution path? If you have a procurement / legal contact for this, I'm happy to work with them directly.

For reference, our company information:

- **Legal name**: Robworks Software (sole-proprietor doing business as)
- **Owner**: Ryan Robson
- **Country**: United States
- **Primary contact for BAA**: ringo380@gmail.com

Thanks for the help. Happy to answer any questions about how we'll be using [Vendor product].

Best,
Ryan Robson
Founder, Robworks Software
referme.help

---

## Notes for follow-up

- If they reply with a BAA template: review, redline, sign, store countersigned PDF in `~/Documents/robworks/baa/`, update `docs/compliance/baa-register.md`.
- If they say "BAA only on Enterprise": evaluate cost vs. switching vendors. For pre-revenue, switching is usually right.
- If they decline: document, mark vendor as 🚫 in baa-register, find an alternative.
- If they don't respond within 14 days: bump.
