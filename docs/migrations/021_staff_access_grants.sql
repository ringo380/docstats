-- Migration 021: staff_access_grants — time-limited user consent for staff data access
--
-- A staff_access_grant is created by the user and authorises staff to view their
-- account data through the app for a limited window.  No active grant means no
-- staff-visible data, regardless of the operator's DB-level service-role access.
-- Follows the same expires_at / revoked_at pattern as sessions and invitations.

-- Postgres (Supabase)
CREATE TABLE IF NOT EXISTS docstats_staff_access_grants (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL
                     REFERENCES docstats_users(id) ON DELETE CASCADE,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_staff_access_grants_user_id
    ON docstats_staff_access_grants (user_id);
