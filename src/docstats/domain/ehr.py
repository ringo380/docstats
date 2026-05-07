"""EHR (SMART-on-FHIR) connection domain models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

EHR_VENDORS: set[str] = {"epic_sandbox", "cerner_oauth", "ecw_smart", "redox"}

# Default scope set for Phase 12.A — Patient-only standalone launch.
# offline_access is needed for refresh_token. fhirUser + openid identify the
# launching user; launch/patient narrows to the patient context picked at auth.
# Epic patient-facing standalone launch:
# - `launch/patient` is an EHR-LAUNCH scope (sidebar in Epic) and breaks
#   MyChart's standalone OAuth — Epic forwards it to MyChart with no actual
#   data scope, MyChart returns "request is invalid".
# - `offline_access` requires "Requires Persistent Access" checked in the
#   Epic developer portal. Added in 12.B alongside _maybe_refresh wiring.
EPIC_SCOPES: str = "openid fhirUser patient/Patient.read offline_access"

# Scope set for EHR-launch (sidebar): Epic provides patient context via the
# launch token so launch/patient is not needed; `launch` is required.
EPIC_SCOPES_EHR_LAUNCH: str = "openid fhirUser launch offline_access"

# Cerner/Oracle Health scope sets.
# Cerner uses MedicationRequest (not MedicationStatement), so the resource
# scope references that resource type.
CERNER_SCOPES: str = (
    "openid fhirUser patient/Patient.read patient/Condition.read "
    "patient/MedicationRequest.read patient/AllergyIntolerance.read "
    "patient/DocumentReference.read offline_access"
)
CERNER_SCOPES_EHR_LAUNCH: str = "openid fhirUser launch offline_access"

# eClinicalWorks (eCW) scope sets — Phase 12.D.
# eCW uses MedicationRequest like Cerner. The eCW dev portal does NOT
# expose `openid` / `fhirUser` / `offline_access` / `launch` as checkbox
# scopes — those are gated by separate radio buttons (OpenID? Yes; Refresh
# Token? Yes/Offline). We still send them in the OAuth `scope` parameter
# per the SMART App Launch spec; the portal radios just authorize the app
# to USE them.
ECW_SCOPES: str = (
    "openid fhirUser patient/Patient.read patient/Condition.read "
    "patient/MedicationRequest.read patient/AllergyIntolerance.read "
    "patient/DocumentReference.read offline_access"
)
ECW_SCOPES_EHR_LAUNCH: str = "openid fhirUser launch offline_access"

# Redox aggregator scope set — Phase 12.E.
# Redox uses system/* scopes (backend-to-backend trust boundary) instead of
# patient/* (per-user SMART trust). Refresh tokens are not used; the JWT-bearer
# assertion grant re-issues access tokens on demand.
REDOX_SCOPES: str = (
    "system/Patient.read system/Condition.read system/MedicationRequest.read "
    "system/AllergyIntolerance.read system/DocumentReference.read "
    "system/ServiceRequest.write"
)


class EHRConnection(BaseModel):
    """EHR connection record.

    For SMART-on-FHIR vendors (Epic/Cerner/eCW), tokens are Fernet ciphertext.
    For backend-to-backend vendors (Redox), token-related columns are NULL —
    those connections re-mint short-lived access tokens via JWT-bearer
    assertion on each request.

    Exactly one of ``user_id`` / ``organization_id`` is set. Storage CHECK
    enforces this; in Python prefer the ``is_org_scoped`` property.
    """

    id: int
    user_id: int | None
    organization_id: int | None = None
    ehr_vendor: str
    iss: str
    patient_fhir_id: str | None
    access_token_enc: str | None
    refresh_token_enc: str | None
    expires_at: datetime | None
    scope: str | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def is_active(self) -> bool:
        # Token may be expired but still refreshable; "active" here means the
        # connection row hasn't been revoked. Token-expiry is a separate concern
        # handled by the per-vendor refresh path (or, for Redox, by re-signing
        # a fresh JWT assertion).
        return self.revoked_at is None

    @property
    def is_org_scoped(self) -> bool:
        return self.organization_id is not None


class ImportedPatient(BaseModel):
    """Subset of FHIR Patient extracted from a SMART-on-FHIR import.

    Field shape mirrors the Patient row columns we'd write on `create_patient`
    so the route layer can pass through with minimal massaging.
    """

    fhir_id: str
    mrn: str | None
    first_name: str | None
    last_name: str | None
    middle_name: str | None
    date_of_birth: str | None  # ISO YYYY-MM-DD
    gender: str | None  # FHIR administrative-gender: male/female/other/unknown
    phone: str | None
    email: str | None
    address_line1: str | None
    address_line2: str | None
    address_city: str | None
    address_state: str | None
    address_zip: str | None
