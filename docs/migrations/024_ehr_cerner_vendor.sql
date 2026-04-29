-- Migration 024: Add cerner_oauth to ehr_vendor CHECK constraint.
-- SQLite's CREATE TABLE ... IF NOT EXISTS handles this at table-creation time
-- (storage.py was updated inline). For Postgres we must drop + re-add the constraint.

ALTER TABLE docstats_ehr_connections
    DROP CONSTRAINT IF EXISTS docstats_ehr_connections_ehr_vendor_check;

ALTER TABLE docstats_ehr_connections
    ADD CONSTRAINT docstats_ehr_connections_ehr_vendor_check
    CHECK (ehr_vendor IN ('epic_sandbox', 'cerner_oauth'));
