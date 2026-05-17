"""Integration test: _ehr_post_create_hook uses the dependent's connection
when a parent has both their own MyChart and the dependent's MyChart proxy
linked (Issue #155 regression). Covers the resolver + hook + storage layer
end-to-end without standing up Epic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from docstats.routes.referrals import _ehr_post_create_hook
from docstats.scope import Scope
from docstats.storage import Storage


@pytest.mark.asyncio
async def test_writeback_hook_routes_to_dependent_connection_when_both_exist(tmp_path):
    storage = Storage(db_path=tmp_path / "test.db")
    parent_id = storage.create_user("parent@example.com", "pw")
    parent_scope = Scope(user_id=parent_id)

    # Parent's own MyChart (different FHIR patient id — proves the
    # ``patient_fhir_id`` mis-routing guard works alongside the new
    # patient-scoped lookup).
    storage.create_ehr_connection(
        user_id=parent_id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test/api/FHIR/R4",
        access_token_enc="PARENT_AT",
        refresh_token_enc="PARENT_RT",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="EPIC-PARENT",
    )

    # Dependent + the dependent's MyChart proxy connection.
    child = storage.create_patient(
        parent_scope,
        first_name="Kid",
        last_name="Doe",
        relationship="child",
        ehr_fhir_id="EPIC-CHILD",
    )
    child_conn = storage.create_patient_ehr_connection(
        patient_id=child.id,
        ehr_vendor="epic_sandbox",
        iss="https://fake-epic.test/api/FHIR/R4",
        access_token_enc="CHILD_AT",
        refresh_token_enc="CHILD_RT",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="openid",
        patient_fhir_id="EPIC-CHILD",
    )

    # Referral on the dependent.
    from docstats.domain.referrals import Referral

    ref = storage.create_referral(
        parent_scope,
        patient_id=child.id,
        referring_provider_name="Dr Parent",
        receiving_organization_name="Specialty Clinic",
        specialty_code="207RC0000X",
        specialty_desc="Cardiology",
        reason="Eval",
        urgency="routine",
    )
    assert isinstance(ref, Referral)

    # Stub the heavy network paths. We only care that the hook resolved
    # the CHILD's connection — observe via ``set_referral_ehr_writeback``.
    seen_conn_ids: list[int] = []
    real_set = storage.set_referral_ehr_writeback

    def capture_writeback(*args, **kwargs):
        seen_conn_ids.append(kwargs["ehr_connection_id"])
        return real_set(*args, **kwargs)

    with (
        patch.object(storage, "set_referral_ehr_writeback", side_effect=capture_writeback),
        patch("docstats.routes.ehr._maybe_refresh", return_value="DECRYPTED_AT"),
        patch("docstats.ehr.epic.discover") as disc,
        patch("docstats.ehr.epic.fetch_conditions", return_value=[]),
        patch("docstats.ehr.epic.fetch_medications", return_value=[]),
        patch("docstats.ehr.epic.fetch_allergies", return_value=[]),
        patch("docstats.ehr.epic.fetch_document_references", return_value=[]),
        patch("docstats.ehr.epic.write_service_request", return_value="SR-99"),
    ):
        disc.return_value = type(
            "FakeEndpoints",
            (),
            {"fhir_base": "https://fake-epic.test/api/FHIR/R4"},
        )()
        # Fake request — only the audit record path reads it, and audit
        # swallows exceptions so passing a minimal stand-in is fine.
        from starlette.requests import Request

        scope_asgi = {
            "type": "http",
            "method": "POST",
            "headers": [],
            "path": "/",
            "client": ("127.0.0.1", 0),
        }
        fake_request = Request(scope_asgi)

        await _ehr_post_create_hook(
            referral=ref,
            patient_id=child.id,
            user_id=parent_id,
            scope=parent_scope,
            storage=storage,
            request=fake_request,
        )

    assert seen_conn_ids == [child_conn.id], (
        f"Expected write-back to use dependent connection {child_conn.id}; got {seen_conn_ids}"
    )
