-- Migration 037: track remote ServiceRequest status for written-back referrals.
--
-- After ``_ehr_post_create_hook`` / ``_redox_post_create_hook`` POST a FHIR
-- ServiceRequest to the PCP's EHR (Phase 12.B), we want to surface the remote
-- ``status`` field back to the patient so they can see "Received by PCP",
-- "Under review", "Completed by PCP" without logging into MyChart.
--
-- This migration adds the columns the new background poller
-- (``docstats.ehr.status_poller``) needs:
--
--   ehr_vendor          — which vendor module can read this id (matches
--                         docstats_ehr_connections.ehr_vendor values; nullable
--                         so existing rows that predate the poller are skipped)
--   ehr_connection_id   — connection used to perform the write-back; nullable
--                         FK so revoking the connection doesn't orphan the row.
--                         ON DELETE SET NULL keeps the polled status intact.
--   ehr_status          — most recent remote ServiceRequest.status (FHIR R4
--                         vocabulary: draft / active / on-hold / revoked /
--                         completed / entered-in-error / unknown). Free-form
--                         TEXT — CHECK enforced at the Python layer
--                         (EHR_STATUS_VALUES in domain/referrals.py) to keep
--                         the migration reversible if FHIR adds new codes.
--   ehr_status_polled_at — UTC timestamp of the last successful or attempted
--                         poll. Sorts ``NULLS FIRST`` so unseen rows go to
--                         the front of the queue. Bumped on every poll
--                         (success or failure) so errored rows naturally
--                         back off via the LRU ordering.
--   ehr_status_error    — last fetch error excerpt (PHI/token-redacted) for
--                         /admin diagnostics. Cleared on the next success.
--
-- No backfill of ehr_vendor / ehr_connection_id for pre-existing rows with
-- a non-null ehr_service_request_id: there's no reliable historical mapping
-- (the connection the write-back used may have since been revoked, and
-- list_active_ehr_connections returns "first match" not "the one used").
-- Orphan rows stay null and the poller skips them — acceptable since the
-- write-back has been in place for ~3 weeks and prod traffic is sandbox.

ALTER TABLE docstats_referrals
    ADD COLUMN IF NOT EXISTS ehr_vendor TEXT;

ALTER TABLE docstats_referrals
    ADD COLUMN IF NOT EXISTS ehr_connection_id BIGINT
        REFERENCES docstats_ehr_connections(id) ON DELETE SET NULL;

ALTER TABLE docstats_referrals
    ADD COLUMN IF NOT EXISTS ehr_status TEXT;

ALTER TABLE docstats_referrals
    ADD COLUMN IF NOT EXISTS ehr_status_polled_at TIMESTAMPTZ;

ALTER TABLE docstats_referrals
    ADD COLUMN IF NOT EXISTS ehr_status_error TEXT;

-- Partial index on the poller's queue predicate. The poller fetches rows
-- WHERE ehr_service_request_id IS NOT NULL AND ehr_vendor IS NOT NULL
-- AND status NOT IN ('completed','cancelled') ordered by
-- ehr_status_polled_at NULLS FIRST.  Index intentionally narrow so it
-- only carries pollable rows; most referrals never write back.
CREATE INDEX IF NOT EXISTS idx_docstats_referrals_ehr_status_poll
    ON docstats_referrals (ehr_status_polled_at NULLS FIRST, id)
    WHERE ehr_service_request_id IS NOT NULL
      AND ehr_vendor IS NOT NULL
      AND deleted_at IS NULL;

-- Widen the referral_events.event_type CHECK to include the poller-emitted
-- ``ehr_status`` lifecycle row. Original constraint is dropped + re-added
-- since Postgres can't ALTER a CHECK in place. SQLite has its own rebuild
-- in ``_migrate_referral_events_event_type_ehr_status``.
ALTER TABLE docstats_referral_events
    DROP CONSTRAINT IF EXISTS docstats_referral_events_event_type_check;
ALTER TABLE docstats_referral_events
    ADD CONSTRAINT docstats_referral_events_event_type_check
    CHECK (event_type IN (
        'created','status_changed','field_edited','exported','sent',
        'response_received','note_added','assigned','unassigned',
        'dispatched','delivered','delivery_failed','ehr_status'
    ));
