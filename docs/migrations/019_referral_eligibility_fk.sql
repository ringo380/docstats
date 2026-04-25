-- Phase 11.C: Link referrals to their most-recent eligibility check.
--
-- This is a soft denormalisation: the definitive list of checks is in
-- eligibility_checks (patient-scoped).  The FK here is a fast "show me
-- the check that was live when this referral was last reviewed" shortcut.
-- NULL = no check has been run yet for this referral's patient+payer context.
--
-- ON DELETE SET NULL: deleting the check row (future admin purge) should
-- not cascade-delete the referral.

ALTER TABLE referrals
  ADD COLUMN IF NOT EXISTS latest_eligibility_check_id BIGINT
    REFERENCES eligibility_checks(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_referrals_eligibility_check
  ON referrals (latest_eligibility_check_id)
  WHERE latest_eligibility_check_id IS NOT NULL;
