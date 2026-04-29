-- Migration 024: Add cerner_oauth to ehr_vendor CHECK constraint.
-- SQLite's CREATE TABLE ... IF NOT EXISTS handles this at table-creation time
-- (storage.py was updated inline). For Postgres we must drop + re-add the constraint.

ALTER TABLE ehr_connections
    DROP CONSTRAINT IF EXISTS ehr_connections_ehr_vendor_check;

ALTER TABLE ehr_connections
    ADD CONSTRAINT ehr_connections_ehr_vendor_check
    CHECK (ehr_vendor IN ('epic_sandbox', 'cerner_oauth'));
