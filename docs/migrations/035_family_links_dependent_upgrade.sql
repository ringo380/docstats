-- Migration 035: Track dependent-upgrade family link origin.
--
-- When a parent invites a dependent (age 18+) to manage their own account,
-- a family_links row is created with source_patient_id pointing at the
-- dependent's existing Patient row. On accept, that Patient row (plus all
-- referrals scoped to the parent) is re-parented to the new adult user.
-- The family_link itself then behaves like an ordinary adult linking, giving
-- the parent continued visibility.
--
-- Backs GitHub issue #158.

ALTER TABLE docstats_family_links
    ADD COLUMN IF NOT EXISTS source_patient_id BIGINT
        REFERENCES docstats_patients(id) ON DELETE SET NULL;
