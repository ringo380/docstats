# Change Management Policy

**Version**: 1.0
**Effective**: 2026-05-10
**Owner**: Founder
**Review cadence**: Annual

## Purpose

Describe how code, schema, configuration, and infrastructure changes are reviewed, deployed, and rolled back. Maps to SOC 2 CC8.1.

## Scope

Production systems: the FastAPI app on Railway, Supabase Postgres schema, Supabase Storage configuration, Railway environment variables, GitHub Actions workflows, and DNS records at Namecheap.

## Change classes

| Class | Examples | Review | Deploy |
|---|---|---|---|
| Code | Bug fixes, features, refactors | PR + CI green | Squash-merge to `main` → Railway auto-deploy |
| Schema | New tables, columns, indexes, constraints | PR + CI green + Management API SQL applied **before** merge | Apply SQL via curl to Supabase Management API; CLAUDE.md captures the pattern |
| Config (env var) | New/rotated secret, feature flag toggle | None for routine; PR-documented for behavior-changing flags | `railway variables --set` then redeploy |
| Infra | Railway plan change, Supabase tier upgrade, new vendor | Document decision in `docs/compliance/decisions/` (lightweight ADR) | Vendor portal action |
| Emergency hotfix | Active P0/P1 incident | Post-hoc PR within 24h | Deploy first, document immediately after |

## Code change procedure

1. **Branch off `main`** with a descriptive name.
2. **Implement + test locally**: `pytest`, `ruff check`, `ruff format --check`, `mypy src/docstats/` should all pass.
3. **Open a PR** with title + body explaining the change. Test plan as a checklist in the body.
4. **CI must pass**: lint, typecheck, both Python versions, vuln scan, secret scan, log-redaction gate.
5. **Self-review** the diff before merge — read every changed line.
6. **Squash-merge** with a clean subject + body. Branch auto-deletes; remote prunes locally.
7. **Watch the Railway deploy** until status is `SUCCESS`. If failed, roll back (see below).

## Schema change procedure

Postgres migrations are not auto-applied. The discipline:

1. Write the migration SQL in `docs/migrations/NNN_name.sql` — pre-prefix `docstats_` on every table name.
2. Apply via Management API curl **before** merging the referencing PR (auto-deploy fires the moment merge lands; routes will 500 otherwise).
3. Verify the column exists via `SELECT column_name FROM information_schema.columns WHERE table_name='docstats_<table>'`.
4. Then squash-merge.

Pattern captured in CLAUDE.md.

## Rollback procedure

### Code rollback

1. `git revert <merge-sha>` on a new branch.
2. PR + CI green + squash-merge.
3. Railway auto-deploys the revert.

For active P0/P1: `railway redeploy` to a known-good prior deployment via the Railway dashboard. Then file the revert PR.

### Schema rollback

Forward-only migrations. Schema rollbacks require a new compensating migration (e.g., `DROP COLUMN`). Coordinate with the code revert.

### Config rollback

`railway variables --set "KEY=<previous>"` + redeploy.

## Audit trail

- **Code**: GitHub commit history + PR records. Retained indefinitely.
- **Schema**: `docs/migrations/` in repo + Supabase migration log.
- **Config**: Railway dashboard activity log. **Gap**: Railway free-plan log retention is short. Consider periodic export to `docs/compliance/config-changes/YYYY-MM.md`.
- **Infra**: vendor portal logs + decisions in `docs/compliance/decisions/`.

## Separation of duties

Solo founder org. Conflict-of-interest gap documented; will split when headcount > 1 (PR author and reviewer become distinct people).

Until then, the discipline is:

- Self-review every diff.
- CI gates that don't depend on the author (Trivy, gitleaks, log-redaction, type checking) provide independent signal.
- Any change of >200 lines triggers an advisor review (LLM second-opinion) before merge.

## Emergency change

If a P0/P1 incident requires deploying without normal review:

1. Document the incident first (open the incident note per `incident-response.md`).
2. Push directly to `main` if necessary (admin override of branch protection).
3. Within 24 hours: open a retrospective PR with the diff + rationale. CI runs against the already-merged code as a smoke check.
4. Post-mortem includes a review of whether normal process could have caught the underlying issue earlier.
