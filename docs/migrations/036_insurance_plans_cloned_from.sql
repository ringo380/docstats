-- Migration 036: Track the source plan when a shared insurance plan is
-- cloned into a different scope at referral-create time.
--
-- Without this column the dedup logic in _resolve_payer_plan_for_referral
-- can only match on (payer_name, plan_type, plan_name); two linked family
-- members sharing plans with identical labels (e.g. both on "Aetna PPO")
-- silently collide so the second pick reuses the first holder's clone and
-- the referral inherits the wrong holder's member_id_pattern.
--
-- Soft link (no FK) — the source row can be soft-deleted independently and
-- we don't want CASCADE / SET NULL surprises on the clone.

ALTER TABLE docstats_insurance_plans
    ADD COLUMN IF NOT EXISTS cloned_from_plan_id BIGINT;
