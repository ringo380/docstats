-- Migration 025: add ehr_service_request_id to referrals.
--
-- Nullable TEXT column that stores the Epic FHIR ServiceRequest.id written
-- back to Epic on referral creation (Phase 12.B). NULL means no write-back
-- has occurred (either because the referral predates 12.B or the patient
-- has no active EHR connection).

ALTER TABLE docstats_referrals ADD COLUMN IF NOT EXISTS ehr_service_request_id TEXT;
