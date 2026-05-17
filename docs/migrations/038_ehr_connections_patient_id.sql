-- Migration 038: Patient-scoped EHR connections (Issue #155).
--
-- Adds a third ownership dimension to docstats_ehr_connections so a parent
-- can connect a minor dependent's patient portal (MyChart proxy, Cerner
-- proxy, eCW proxy) without polluting the parent's own EHR connection set.
-- Today the row is owned by exactly one of (user_id, organization_id);
-- this widens that to a three-way exclusive-or with patient_id.
--
-- The write-back resolver (routes/referrals.py::_ehr_post_create_hook)
-- already picks a connection by matching patient_fhir_id == ehr_fhir_id;
-- it just needs a wider candidate set, which patient_id provides.
--
-- See plans/yes-start-on-155-eventual-sunset.md for the full design.

-- ---- 1) Add patient_id column --------------------------------------------
ALTER TABLE docstats_ehr_connections
    ADD COLUMN IF NOT EXISTS patient_id INTEGER
        REFERENCES docstats_patients(id) ON DELETE CASCADE;

-- ---- 2) Widen exactly-one-owner CHECK to three-way XOR -------------------
ALTER TABLE docstats_ehr_connections
    DROP CONSTRAINT IF EXISTS docstats_ehr_connections_owner_check;

ALTER TABLE docstats_ehr_connections
    ADD CONSTRAINT docstats_ehr_connections_owner_check
    CHECK (
        (user_id IS NOT NULL AND organization_id IS NULL AND patient_id IS NULL)
        OR (user_id IS NULL AND organization_id IS NOT NULL AND patient_id IS NULL)
        OR (user_id IS NULL AND organization_id IS NULL AND patient_id IS NOT NULL)
    );

-- ---- 3) Partial UNIQUE index on (patient_id, ehr_vendor) -----------------
-- Mirrors the existing user-scoped + org-scoped partial unique indices.
-- Only one active connection per (patient, vendor); historical revoked rows
-- are unconstrained for audit-trail purposes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ehr_connections_patient_active
    ON docstats_ehr_connections (patient_id, ehr_vendor)
    WHERE revoked_at IS NULL AND patient_id IS NOT NULL;
