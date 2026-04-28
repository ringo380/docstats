-- Migration 024: add ehr_fhir_id to patients.
--
-- Nullable TEXT column that stores the Epic FHIR Patient.id when a patient
-- row was imported via SMART-on-FHIR. Used in Phase 12.B to link a patient
-- to their EHR context so clinical resources can be fetched during referral
-- creation. Set in import_confirm (create_new and merge paths).

ALTER TABLE docstats_patients ADD COLUMN IF NOT EXISTS ehr_fhir_id TEXT;
