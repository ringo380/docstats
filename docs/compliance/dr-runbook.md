# Disaster Recovery Runbook

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder

Step-by-step procedures for the disaster scenarios in `policies/business-continuity.md`. This runbook is the thing you read at 3 AM during an actual outage.

## Pre-flight (read this once, not during the incident)

- Open incident note: `~/Documents/robworks/incidents/YYYY-MM-DD_short-name.md`. Timestamp every action.
- Communicate early. In-app banner first; per-customer email if outage > 30 min.
- Have the Railway dashboard, Supabase dashboard, and `gh` CLI ready in three browser tabs / terminals.

## Scenario 1 — Database PITR restore

### When to use

- Accidental destructive operation (mass delete, malformed migration, etc.).
- Data corruption discovered.
- Suspected unauthorized data modification.

### Procedure

1. **Stop writes immediately**:
   ```bash
   railway down --service docstats
   ```
   This pauses the service; existing in-flight requests fail. Better than continuing to write atop corrupt state.

2. **Identify the time of corruption**. Use `audit_events` (most operations are logged with timestamps). Save the timestamp `T_BAD`.

3. **Restore to a Supabase branch (NOT prod)**:
   - Supabase dashboard → Database → Point-in-time recovery → "Create branch from point in time".
   - Pick `T_BAD - 1 minute` as the restore point.
   - Branch creation takes 5–15 minutes for our DB size.

4. **Verify the branch**:
   - Connect to the branch via `psql` or Supabase SQL editor.
   - Run targeted queries to confirm the data is healthy at the restore point.
   - Compare to prod: `SELECT count(*) FROM docstats_referrals;` etc.

5. **Decide promotion**:
   - If branch is clean: promote (Supabase dashboard → "Merge branch to production"). **Promotion overwrites prod data between `T_BAD - 1min` and now.** Document what's being lost.
   - If branch is also corrupt: pick an earlier restore point and re-branch.

6. **Resume service**:
   ```bash
   railway up --service docstats
   ```

7. **Communicate**: email affected customers describing what was lost between restore point and incident, with concrete steps for them to re-enter data.

8. **Post-mortem within 14 days**.

### Expected duration

- Detection → service stopped: 5 min
- Branch creation: 15 min
- Verification: 30 min
- Promotion: 5 min
- Service restart: 5 min
- **Total RTO: ~1 hour for read-only resume; longer for full data reconciliation**

## Scenario 2 — Railway outage (failover to Fly.io)

### When to use

- Railway is fully down for > 4 hours and they have no ETA.
- Railway's status page confirms ongoing platform-wide outage.

### Pre-requisites (set up in advance — TODO)

- `fly.toml` checked into the repo.
- Fly.io account + app pre-created (`docstats-failover`).
- Env var snapshot at `~/Documents/robworks/infra-snapshots/latest-railway-vars.txt.gpg`.

### Procedure

1. **Decrypt the env var snapshot**:
   ```bash
   gpg -d ~/Documents/robworks/infra-snapshots/latest-railway-vars.txt.gpg > /tmp/vars.env
   ```

2. **Push env vars to Fly**:
   ```bash
   fly secrets import --app docstats-failover < /tmp/vars.env
   shred -u /tmp/vars.env
   ```

3. **Deploy from main**:
   ```bash
   fly deploy --app docstats-failover --remote-only
   ```

4. **Update DNS**: Namecheap dashboard → `referme.help` → DNS → point apex CNAME at `docstats-failover.fly.dev`. TTL was 300s; propagation 5–15 min.

5. **Verify**: `curl -I https://referme.help/` — should return 200.

6. **Communicate**: in-app banner if banner system survives DB connectivity; otherwise email.

### Cutting back to Railway

When Railway resumes:

1. Verify Railway service deploys cleanly (push a no-op commit if needed).
2. Update DNS at Namecheap back to `zzu1pdts.up.railway.app`.
3. Wait for DNS propagation, then `fly scale count 0 --app docstats-failover` to stop the failover instance.
4. Diff Postgres for any writes that landed on Fly during the failover window — Supabase Postgres is shared (Fly was just compute), so writes should already be in Supabase.

## Scenario 3 — Lost vendor BAA (vendor terminates relationship)

### When to use

A sub-processor unilaterally terminates the BAA (rare but possible — pricing dispute, acquisition, policy change).

### Procedure

1. **Stop new PHI from flowing to that vendor immediately**. Disable the relevant feature flag or toggle.
2. **Migrate existing PHI off**, per the vendor's data export procedure. Confirm deletion in writing.
3. **Onboard a replacement vendor** (per `vendor-risk.md` evaluation).
4. **Update `baa-register.md`**.
5. **Communicate to customers** if the vendor change affects feature availability or data residency.

## Scenario 4 — Founder unavailable

### Successor procedures

The successor (TBD — identify before first paying customer) has:

1. Access to the 1Password "Robworks Production" vault.
2. This entire `docs/compliance/` binder.
3. Authority to: communicate with customers, engage outside counsel, file insurance claims, execute scenarios 1–3 above.

The successor should:

1. Attempt to reach founder for 24 hours via known contacts.
2. If unreachable: post a status update at `referme.help/status` (TBD page).
3. Maintain the service in caretaker mode — handle outages, do not ship new features or accept new customers.
4. Hand off cleanly when founder returns.

## Scenario 5 — Total Supabase loss (extreme)

### When to use

Supabase loses our project entirely (regional disaster + their backups fail). Industry-baseline likelihood is essentially zero, but documented for completeness.

### Procedure

1. **Restore from cross-region S3 backup** (TODO: this pipeline doesn't exist yet; risk R-005).
2. Provision a new Supabase project (or migrate to RDS/Aurora as a permanent failover).
3. Apply schema migrations from `docs/migrations/` in order.
4. Restore data from latest cross-region backup.
5. Update `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` in Railway, redeploy.
6. Communicate to customers; expect 24+ hours of degraded service.

**This scenario currently has unmitigated risk** — see R-005 in the risk assessment.
