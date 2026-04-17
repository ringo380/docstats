"""Patient — a first-class entity, scope-enforced.

Every Patient row carries exactly one of ``scope_user_id`` (solo mode) or
``scope_organization_id`` (org mode), enforced by a CHECK constraint on both
backends and documented in :class:`docstats.scope.Scope`. All patient CRUD
methods on :class:`docstats.storage_base.StorageBase` take a ``Scope`` as
their first argument; storage layers add the matching scope column to every
WHERE clause so cross-tenant reads are impossible by contract.

Solo-mode nuance (Phase 2 UX concern, not a storage concern): in solo mode
the user IS the patient, so the referral creation flow auto-creates a Patient
row from ``users.first_name`` / ``last_name`` / ``date_of_birth`` rather than
forcing solo users through an explicit patients UI. For storage this is
invisible — the row is still a first-class Patient.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, computed_field


class Patient(BaseModel):
    """A scope-owned patient record."""

    id: int
    scope_user_id: int | None = None
    scope_organization_id: int | None = None

    first_name: str
    last_name: str
    middle_name: str | None = None
    date_of_birth: str | None = None  # ISO YYYY-MM-DD; matches users.date_of_birth
    sex: str | None = None  # "M" / "F" / "O" / "U" / free-text
    mrn: str | None = None

    preferred_language: str | None = None
    pronouns: str | None = None

    phone: str | None = None
    email: str | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_zip: str | None = None

    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None

    notes: str | None = None

    created_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        """Human-readable full name for list views / referral cards."""
        parts = [self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        parts.append(self.last_name)
        return " ".join(parts)
