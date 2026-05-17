"""SMART-on-FHIR client + crypto for EHR integrations (Phase 12)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from docstats.ehr.crypto import EHRConfigError, decrypt_token, encrypt_token

if TYPE_CHECKING:
    from docstats.domain.ehr import EHRConnection
    from docstats.domain.patients import Patient
    from docstats.scope import Scope
    from docstats.storage_base import StorageBase


def pick_writeback_connection(
    storage: "StorageBase", scope: "Scope", patient: "Patient"
) -> "EHRConnection | None":
    """Choose the EHR connection to use when writing back for ``patient``.

    Match precedence (first hit wins):

    1. A patient-scoped connection (Issue #155) whose ``patient_fhir_id``
       matches ``patient.ehr_fhir_id`` — set when a parent connected this
       dependent's own MyChart / Cerner / eCW account.
    2. A user-scoped connection whose ``patient_fhir_id`` matches — the
       common case for self-managed patients.
    3. An org-scoped connection whose ``patient_fhir_id`` matches — the
       Redox aggregator case.

    The ``patient_fhir_id`` equality predicate was introduced in PR #142
    to prevent multi-vendor mis-routing; we preserve it across all three
    candidate sets rather than falling back to "first active connection".
    Returns ``None`` when no candidate matches.
    """
    if not patient.ehr_fhir_id:
        return None

    candidates: list[EHRConnection] = []
    if patient.id is not None:
        candidates.extend(storage.list_active_patient_ehr_connections(patient.id))
    if scope.user_id is not None:
        candidates.extend(storage.list_active_ehr_connections(scope.user_id))
    if scope.organization_id is not None:
        candidates.extend(storage.list_active_org_ehr_connections(scope.organization_id))

    return next((c for c in candidates if c.patient_fhir_id == patient.ehr_fhir_id), None)


@dataclass(frozen=True)
class ServiceRequestSnapshot:
    """A read-only snapshot of a remote FHIR ServiceRequest.

    Returned by each vendor module's ``read_service_request`` for the
    issue-#157 status poller. ``raw_status`` is the verbatim value the
    server returned (may include vendor-proprietary extensions); ``status``
    is the same value coerced into the FHIR R4 request-status vocabulary
    (``EHR_STATUS_VALUES`` in ``domain.referrals``) when possible, or
    ``"unknown"`` when the server returns a value we don't recognize.
    """

    status: str
    raw_status: str
    last_modified: datetime | None


def parse_service_request_payload(body: dict) -> ServiceRequestSnapshot:
    """Map a FHIR R4 ServiceRequest JSON body into a snapshot.

    Coerces unknown ``status`` values to ``"unknown"`` so callers can rely
    on the FHIR vocabulary (see ``EHR_STATUS_VALUES``). ``meta.lastUpdated``
    is parsed as an ISO-8601 instant when present.
    """
    from docstats.domain.referrals import EHR_STATUS_VALUES

    raw_status = str(body.get("status") or "")
    status = raw_status if raw_status in EHR_STATUS_VALUES else "unknown"
    last_modified_raw = (
        (body.get("meta") or {}).get("lastUpdated") if isinstance(body.get("meta"), dict) else None
    )
    last_modified: datetime | None = None
    if isinstance(last_modified_raw, str):
        try:
            last_modified = datetime.fromisoformat(last_modified_raw.replace("Z", "+00:00"))
        except ValueError:
            last_modified = None
    return ServiceRequestSnapshot(
        status=status,
        raw_status=raw_status,
        last_modified=last_modified,
    )


__all__ = [
    "EHRConfigError",
    "ServiceRequestSnapshot",
    "decrypt_token",
    "encrypt_token",
    "parse_service_request_payload",
    "pick_writeback_connection",
]
