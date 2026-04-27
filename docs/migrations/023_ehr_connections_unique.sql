-- Migration 023: tighten ehr_connections partial index to UNIQUE.
--
-- Migration 022 created a plain partial index gating one active connection
-- per (user_id, ehr_vendor); the route layer revoke-then-insert pattern
-- (revoke ALL active rows for the user-vendor pair, then INSERT) is
-- race-safe by itself. Promoting to UNIQUE adds a DB-level guarantee so a
-- duplicate active row simply cannot exist even under bizarre concurrent
-- traffic.
--
-- Predicate is deterministic (`revoked_at IS NULL`) so a partial UNIQUE
-- index is permitted on Postgres.

DROP INDEX IF EXISTS idx_ehr_connections_user_active;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ehr_connections_user_active
    ON docstats_ehr_connections (user_id, ehr_vendor)
    WHERE revoked_at IS NULL;
