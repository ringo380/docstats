# Incident Response Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

Describe how security incidents — especially those involving PHI — are detected, contained, eradicated, recovered from, and reported. Maps to HIPAA §164.308(a)(6) (Security Incident Procedures) and the Breach Notification Rule (§164.400–414).

## Definitions

- **Security incident**: any successful or attempted unauthorized access, use, disclosure, modification, or destruction of information; or interference with system operations.
- **Breach**: an impermissible use or disclosure of PHI that compromises its security or privacy. Default presumption per HIPAA — must conduct a risk assessment to overcome.
- **Sub-processor**: any vendor with a BAA on file (`baa-register.md`).

## Roles

Single-person org currently. The founder fills every role:

- **Incident Commander** — owns the response.
- **Communications lead** — drafts customer / regulator notifications.
- **Technical lead** — investigates, contains, eradicates.

When headcount grows, these will split. For now, document the conflict-of-interest gap and proceed.

## Phases

### 1. Detect

Detection sources, in rough order of expected signal:

- Application audit log anomalies (`/admin/audit` review, alerts on unusual `*.denied` rates).
- Trivy / gitleaks CI failures on push to main.
- Vendor security notices (Supabase status page, Railway, Resend, Documo).
- User reports (`security@referme.help` — set up forwarding).
- External researcher reports (responsible disclosure — see `acceptable-use.md`).

### 2. Triage (within 1 hour of detection)

- Assign severity:
  - **P0** — confirmed PHI exposure to an unauthorized party.
  - **P1** — credible suspected PHI exposure; or active intrusion.
  - **P2** — successful unauthorized access without PHI exposure (e.g., admin console accessed but no records read).
  - **P3** — unsuccessful intrusion attempt requiring notification of vendors but no internal action.
- Open an incident note in `~/Documents/robworks/incidents/YYYY-MM-DD_short-name.md`. This is the master timeline; update in real-time.

### 3. Contain (within 4 hours for P0/P1)

Reversible mitigations first:

- Revoke compromised sessions: `revoke_session()` or wipe the `sessions` table for the affected user.
- Rotate compromised secrets: see `encryption.md` rotation table.
- Disable affected user/org accounts.
- For active intrusion: if needed, take the app offline (Railway `railway down`). The product is not life-safety, and uptime takes second priority to PHI integrity.

### 4. Eradicate

- Root-cause analysis. Don't just patch the symptom.
- For code defects: write a regression test before fixing, then fix.
- For misconfigured vendor: update + document in change log.
- For credential compromise: rotate + investigate how it happened.

### 5. Recover

- Restore service.
- Re-enable affected accounts after confirming clean state.
- Monitor closely for 7 days post-recovery.

### 6. Notify

**HIPAA Breach Notification Rule timelines** (assume PHI of US individuals unless proven otherwise):

| Audience | Threshold | Deadline |
|---|---|---|
| Affected individuals | Any unsecured PHI breach | **60 days** from discovery |
| HHS OCR | Any unsecured PHI breach | **60 days** from discovery (≥500 individuals) or annually (<500) |
| Prominent media | Breach affecting ≥500 individuals in a state/jurisdiction | 60 days |
| Sub-processors covered by BAA | Any incident involving their service | Per BAA terms (typically immediate) |
| State AGs | Per state law (varies; CA, NY, TX have specific rules) | Per state law |
| Cyber insurance carrier | Any P0/P1 | Per policy (typically 24–72 hours) |

Notification template lives in `~/Documents/robworks/incidents/templates/breach-notification.md`. Do not improvise — use the template, fill the slots, send through legal review if available.

### 7. Post-mortem (within 14 days)

Blameless write-up covering:

- Timeline (verbatim from the incident note).
- Root cause(s).
- What worked in detection / response.
- What didn't.
- Concrete action items with owners and due dates. File as PRs/issues, not abstract goals.

Post-mortems live in `~/Documents/robworks/incidents/postmortems/`.

## Specific runbooks

### Suspected EHR token compromise

1. Revoke the affected `ehr_connections` row (`revoke_ehr_connection`).
2. Notify the user via in-app banner + email; ask them to reconnect their EHR.
3. Notify the EHR vendor's security team (contacts in `vendor-outreach/ehr-security-contacts.md` — TODO).
4. Audit `ehr.token_refresh_failed` events for 30 days back to estimate exposure window.

### Suspected webhook-secret leak

1. Rotate `WEBHOOK_INBOX_SECRET` in Railway, redeploy.
2. Coordinate with senders to rotate their copy.
3. Replay any inbound webhooks received during the suspected-leak window from `webhook_inbox` for forensics.

### Supabase service-key compromise

1. Rotate the key in Supabase dashboard.
2. Update `SUPABASE_SERVICE_KEY` env var in Railway, redeploy.
3. Audit recent Supabase activity log for anomalous queries.
4. If compromise predates audit retention: **assume PHI exposure**, treat as P0.

## Drills

Tabletop exercises annually. First drill scheduled for 2026-Q4. Drill log: `docs/compliance/incident-drills.md` (TODO once first drill runs).

## Contacts

- **HHS OCR**: file at https://ocrportal.hhs.gov/ocr/breach/breach_form.jsf
- **Cyber insurance**: TBD — current policy lapsed; renewal pending.
- **Outside counsel**: TBD — engage one before first paying customer.
