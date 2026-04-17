-- Phase 1.B: referrals + referral_events.
--
-- Every referral is scope-owned and references a patient in the same scope.
-- Scope-match between the referral and its patient is enforced at the
-- application layer (storage's ``create_referral`` refuses cross-scope FKs)
-- since DB-level enforcement would require a trigger or generated column.
--
-- referral_events is append-only and scoped transitively through its parent
-- referral — no scope_user_id / scope_organization_id columns on the event row.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/007_referrals.sql)"

CREATE TABLE IF NOT EXISTS docstats_referrals (
    id                            BIGSERIAL PRIMARY KEY,

    -- Exactly one of these two must be set (CHECK below).
    scope_user_id                 INTEGER REFERENCES docstats_users(id) ON DELETE CASCADE,
    scope_organization_id         INTEGER REFERENCES docstats_organizations(id) ON DELETE CASCADE,

    patient_id                    INTEGER NOT NULL REFERENCES docstats_patients(id) ON DELETE RESTRICT,

    referring_provider_npi        TEXT,
    referring_provider_name       TEXT,
    referring_organization        TEXT,

    receiving_provider_npi        TEXT,
    receiving_organization_name   TEXT,

    specialty_code                TEXT,
    specialty_desc                TEXT,

    reason                        TEXT,
    clinical_question             TEXT,
    urgency                       TEXT NOT NULL DEFAULT 'routine'
                                   CHECK (urgency IN ('routine','priority','urgent','stat')),
    requested_service             TEXT,

    diagnosis_primary_icd         TEXT,
    diagnosis_primary_text        TEXT,

    payer_plan_id                 INTEGER,  -- FK to docstats_insurance_plans lands in Phase 1.E
    authorization_number          TEXT,
    authorization_status          TEXT NOT NULL DEFAULT 'na_unknown'
                                   CHECK (authorization_status IN
                                          ('not_required','required_pending','obtained','denied','na_unknown')),

    status                        TEXT NOT NULL DEFAULT 'draft'
                                   CHECK (status IN
                                          ('draft','ready','sent','awaiting_records','awaiting_auth',
                                           'scheduled','rejected','completed','cancelled')),
    assigned_to_user_id           INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,

    external_reference_id         TEXT,
    external_source               TEXT NOT NULL DEFAULT 'manual'
                                   CHECK (external_source IN ('manual','bulk_csv','api')),

    created_by_user_id            INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    created_at                    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at                    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at                    TIMESTAMP WITH TIME ZONE,

    CONSTRAINT docstats_referrals_scope_exactly_one CHECK (
        (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
        OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
    )
);

-- Workspace queue indices: drive /referrals filter views. Compound on scope
-- + status so filtered list queries stay covered.
CREATE INDEX IF NOT EXISTS idx_docstats_referrals_scope_user_status
    ON docstats_referrals (scope_user_id, status, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_docstats_referrals_scope_org_status
    ON docstats_referrals (scope_organization_id, status, updated_at DESC)
    WHERE deleted_at IS NULL;

-- Per-patient referral list — used by the patient-detail view to show
-- referral history.
CREATE INDEX IF NOT EXISTS idx_docstats_referrals_patient
    ON docstats_referrals (patient_id, created_at DESC)
    WHERE deleted_at IS NULL;

-- Assignee queue — for "my referrals" filter.
CREATE INDEX IF NOT EXISTS idx_docstats_referrals_assignee
    ON docstats_referrals (assigned_to_user_id, status, updated_at DESC)
    WHERE assigned_to_user_id IS NOT NULL AND deleted_at IS NULL;


CREATE TABLE IF NOT EXISTS docstats_referral_events (
    id               BIGSERIAL PRIMARY KEY,
    referral_id      INTEGER NOT NULL REFERENCES docstats_referrals(id) ON DELETE CASCADE,
    event_type       TEXT NOT NULL
                      CHECK (event_type IN
                             ('created','status_changed','field_edited','exported','sent',
                              'response_received','note_added','assigned','unassigned')),
    from_value       TEXT,
    to_value         TEXT,
    actor_user_id    INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    note             TEXT,
    created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Timeline view on a referral detail page — newest event first.
CREATE INDEX IF NOT EXISTS idx_docstats_referral_events_referral
    ON docstats_referral_events (referral_id, created_at DESC, id DESC);
