# Disaster Recovery Drill Log

Per `policies/business-continuity.md`, at least one DR drill is performed quarterly. This file logs every drill executed.

## Drill template

```markdown
### YYYY-MM-DD — Drill name

- **Type**: tabletop | live
- **Scenario**: which scenario from `dr-runbook.md`
- **Conducted by**: name(s)
- **Duration**: HH:MM
- **Goal**: what we wanted to verify

#### Outcome

- What worked
- What didn't
- Deficiencies identified

#### Action items

- [ ] Item 1 (owner, due)
- [ ] Item 2 (owner, due)

#### Evidence

- Link to incident note (if any)
- Screenshots
- Restored DB connection string (sanitized)
```

## Drills

_No drills logged yet. First drill scheduled for 2026-Q3._

### Planned drills

| Quarter | Drill | Type |
|---|---|---|
| 2026-Q3 | Scenario 1 — DB PITR restore (live, restore to a Supabase branch, verify, do NOT promote) | Live |
| 2026-Q4 | Scenario 2 — Railway → Fly.io failover (tabletop) | Tabletop |
| 2027-Q1 | Scenario 4 — Founder unavailable (tabletop with designated successor) | Tabletop |
| 2027-Q2 | Scenario 1 again (live), this time including communications dry-run | Live |
