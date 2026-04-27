"""EHR (SMART-on-FHIR) connection domain models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

EHR_VENDORS: set[str] = {"epic_sandbox"}

# Default scope set for Phase 12.A — Patient-only standalone launch.
# offline_access is needed for refresh_token. fhirUser + openid identify the
# launching user; launch/patient narrows to the patient context picked at auth.
EPIC_SCOPES: str = "openid fhirUser launch/patient patient/Patient.read offline_access"


class EHRConnection(BaseModel):
    """Encrypted SMART-on-FHIR connection. Tokens are Fernet ciphertext."""

    id: int
    user_id: int
    ehr_vendor: str
    iss: str
    patient_fhir_id: str | None
    access_token_enc: str
    refresh_token_enc: str | None
    expires_at: datetime
    scope: str
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def is_active(self) -> bool:
        # Token may be expired but still refreshable; "active" here means the
        # connection row hasn't been revoked. Token-expiry is a separate concern
        # handled by the Epic client's refresh path.
        return self.revoked_at is None


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
