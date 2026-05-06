-- Migration 030: Add visit_location_type column to saved_providers.
--
-- Drives the new appointment-address wizard introduced as a replacement
-- for the inline chip-form UI (templates/_appt_address.html).
--
-- Vocabulary:
--   'practice'  - user visits at the NPPES practice address
--   'televisit' - telehealth / virtual visit (referral letter still
--                 prints the practice address as the doctor's office)
--   'custom'    - user visits at a different location stored in
--                 appt_address / appt_suite
--   NULL        - legacy or not-yet-configured; UI shows "Add appointment
--                 details" CTA, exports fall back to NPPES practice address
--
-- Backfill rule: rows that already had user-supplied appointment details
-- are migrated to the closest matching value; everything else stays NULL.
-- This preserves prior choices without retroactively claiming "practice"
-- for a row the user never touched.

ALTER TABLE docstats_saved_providers
    ADD COLUMN IF NOT EXISTS visit_location_type TEXT
        CHECK (visit_location_type IN ('practice', 'televisit', 'custom'));

UPDATE docstats_saved_providers
   SET visit_location_type = 'televisit'
 WHERE is_televisit = TRUE
   AND visit_location_type IS NULL;

UPDATE docstats_saved_providers
   SET visit_location_type = 'custom'
 WHERE appt_address IS NOT NULL
   AND visit_location_type IS NULL;
