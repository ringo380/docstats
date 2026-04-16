"""Pydantic models for NPPES API responses and domain objects."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TypedDict

from pydantic import BaseModel, computed_field

from docstats.normalize import format_credential, format_name, format_phone, format_postal_code


class UserRecord(TypedDict, total=False):
    """Shape of user dicts returned by storage backends."""

    id: int
    email: str
    password_hash: str | None
    github_id: str | None
    github_login: str | None
    display_name: str | None
    first_name: str | None
    last_name: str | None
    middle_name: str | None
    date_of_birth: str | None
    pcp_npi: str | None
    terms_accepted_at: str | None
    terms_version: str | None
    terms_ip: str | None
    terms_user_agent: str | None
    created_at: str
    last_login_at: str | None


class Address(BaseModel):
    """Provider address from NPPES API."""

    address_1: str = ""
    address_2: str | None = None
    address_purpose: str = ""  # LOCATION or MAILING
    address_type: str | None = None
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country_code: str = "US"
    country_name: str | None = None
    telephone_number: str | None = None
    fax_number: str | None = None

    @computed_field
    @property
    def formatted_phone(self) -> str | None:
        return format_phone(self.telephone_number) if self.telephone_number else None

    @computed_field
    @property
    def formatted_fax(self) -> str | None:
        return format_phone(self.fax_number) if self.fax_number else None

    @computed_field
    @property
    def formatted_postal(self) -> str:
        return format_postal_code(self.postal_code)

    @computed_field
    @property
    def one_line(self) -> str:
        parts = [self.address_1]
        if self.address_2:
            parts.append(self.address_2)
        parts.append(f"{self.city}, {self.state} {self.formatted_postal}")
        return ", ".join(parts)


class Taxonomy(BaseModel):
    """Provider taxonomy/specialty from NPPES API."""

    code: str = ""
    desc: str = ""
    primary: bool = False
    license: str | None = None
    state: str | None = None
    taxonomy_group: str | None = None


class OtherName(BaseModel):
    """Alternate name record from NPPES API."""

    code: str | None = None
    type: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    middle_name: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    credential: str | None = None
    organization_name: str | None = None


class Endpoint(BaseModel):
    """Communication endpoint from NPPES API."""

    endpoint: str | None = None
    endpointType: str | None = None
    endpointTypeDescription: str | None = None
    endpointDescription: str | None = None
    affiliation: str | None = None
    affiliationName: str | None = None
    use: str | None = None
    useDescription: str | None = None
    contentType: str | None = None
    contentTypeDescription: str | None = None


class BasicIndividual(BaseModel):
    """Basic info for NPI-1 (individual provider)."""

    first_name: str = ""
    last_name: str = ""
    middle_name: str | None = None
    credential: str | None = None
    name_prefix: str | None = None
    name_suffix: str | None = None
    sex: str | None = None
    sole_proprietor: str | None = None
    enumeration_date: str | None = None
    last_updated: str | None = None
    certification_date: str | None = None
    status: str | None = None
    deactivation_date: str | None = None
    reactivation_date: str | None = None


class BasicOrganization(BaseModel):
    """Basic info for NPI-2 (organization)."""

    organization_name: str = ""
    organizational_subpart: str | None = None
    authorized_official_first_name: str | None = None
    authorized_official_last_name: str | None = None
    authorized_official_middle_name: str | None = None
    authorized_official_title_or_position: str | None = None
    authorized_official_telephone_number: str | None = None
    authorized_official_name_prefix: str | None = None
    authorized_official_name_suffix: str | None = None
    authorized_official_credential: str | None = None
    enumeration_date: str | None = None
    last_updated: str | None = None
    certification_date: str | None = None
    status: str | None = None
    deactivation_date: str | None = None
    reactivation_date: str | None = None


class NPIResult(BaseModel):
    """Single provider result from the NPPES API."""

    number: str
    enumeration_type: str  # NPI-1 or NPI-2
    basic: dict[str, Any] = {}
    addresses: list[Address] = []
    taxonomies: list[Taxonomy] = []
    identifiers: list[dict[str, Any]] = []
    other_names: list[OtherName] = []
    endpoints: list[Endpoint] = []
    practiceLocations: list[dict[str, Any]] = []
    created_epoch: str | None = None
    last_updated_epoch: str | None = None

    @property
    def is_individual(self) -> bool:
        return self.enumeration_type == "NPI-1"

    @property
    def is_organization(self) -> bool:
        return self.enumeration_type == "NPI-2"

    def parsed_basic(self) -> BasicIndividual | BasicOrganization:
        """Parse the basic dict into the appropriate typed model."""
        if self.is_individual:
            return BasicIndividual.model_validate(self.basic)
        return BasicOrganization.model_validate(self.basic)

    @computed_field
    @property
    def display_name(self) -> str:
        """Human-friendly display name."""
        if self.is_individual:
            ind = BasicIndividual.model_validate(self.basic)
            parts = []
            if ind.name_prefix and ind.name_prefix != "--":
                parts.append(format_name(ind.name_prefix))
            parts.append(format_name(ind.first_name))
            if ind.middle_name and ind.middle_name != "--":
                parts.append(format_name(ind.middle_name))
            parts.append(format_name(ind.last_name))
            name = " ".join(parts)
            if ind.credential:
                cred = format_credential(ind.credential)
                if cred:
                    name = f"{name}, {cred}"
            return name
        else:
            org = BasicOrganization.model_validate(self.basic)
            return format_name(org.organization_name)

    @computed_field
    @property
    def entity_label(self) -> str:
        return "Individual" if self.is_individual else "Organization"

    @computed_field
    @property
    def primary_taxonomy(self) -> Taxonomy | None:
        for t in self.taxonomies:
            if t.primary:
                return t
        return self.taxonomies[0] if self.taxonomies else None

    @computed_field
    @property
    def primary_specialty(self) -> str:
        t = self.primary_taxonomy
        return t.desc if t else "Unknown"

    @computed_field
    @property
    def location_address(self) -> Address | None:
        for a in self.addresses:
            if a.address_purpose == "LOCATION":
                return a
        return self.addresses[0] if self.addresses else None

    @computed_field
    @property
    def mailing_address(self) -> Address | None:
        for a in self.addresses:
            if a.address_purpose == "MAILING":
                return a
        return None

    @computed_field
    @property
    def phone(self) -> str | None:
        addr = self.location_address
        if addr and addr.telephone_number:
            return format_phone(addr.telephone_number)
        return None

    @computed_field
    @property
    def fax(self) -> str | None:
        addr = self.location_address
        if addr and addr.fax_number:
            return format_phone(addr.fax_number)
        return None

    @computed_field
    @property
    def status(self) -> str:
        return self.basic.get("status", "Unknown") or "Unknown"

    @computed_field
    @property
    def enumeration_date(self) -> str | None:
        return self.basic.get("enumeration_date")


class NPIResponse(BaseModel):
    """Top-level NPPES API response."""

    result_count: int = 0
    results: list[NPIResult] = []


# --- Domain models for local persistence ---


class SavedProvider(BaseModel):
    """Flattened provider record for local storage."""

    npi: str
    display_name: str
    entity_type: str  # Individual or Organization
    specialty: str | None = None
    phone: str | None = None
    fax: str | None = None
    address_line1: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_zip: str | None = None
    raw_json: str  # full API result for rehydration
    notes: str | None = None
    appt_address: str | None = None
    appt_suite: str | None = None
    appt_phone: str | None = None
    appt_fax: str | None = None
    is_televisit: bool = False
    enrichment_json: str | None = None  # serialized EnrichmentData
    saved_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_npi_result(cls, result: NPIResult, notes: str | None = None) -> SavedProvider:
        """Create a SavedProvider from an API result."""
        addr = result.location_address
        now = datetime.now()
        return cls(
            npi=result.number,
            display_name=result.display_name,
            entity_type=result.entity_label,
            specialty=result.primary_specialty,
            phone=result.phone,
            fax=result.fax,
            address_line1=addr.address_1 if addr else None,
            address_city=addr.city if addr else None,
            address_state=addr.state if addr else None,
            address_zip=addr.formatted_postal if addr else None,
            raw_json=result.model_dump_json(),
            notes=notes,
            saved_at=now,
            updated_at=now,
        )

    def to_npi_result(self) -> NPIResult:
        """Rehydrate the full NPIResult from stored JSON."""
        return NPIResult.model_validate_json(self.raw_json)

    def export_fields(self) -> dict[str, str]:
        """Flat dict of human-readable fields for CSV/JSON export."""
        fields = {
            "NPI": self.npi,
            "Name": self.display_name,
            "Entity Type": self.entity_type,
            "Specialty": self.specialty or "",
            "Phone": self.phone or "",
            "Fax": self.fax or "",
            "Address": self.address_line1 or "",
            "City": self.address_city or "",
            "State": self.address_state or "",
            "ZIP": self.address_zip or "",
            "Notes": self.notes or "",
            "Appointment Address": self.appt_address or "",
            "Appointment Suite": self.appt_suite or "",
            "Appointment Phone": self.appt_phone or "",
            "Appointment Fax": self.appt_fax or "",
            "Televisit": "Yes" if self.is_televisit else "",
            "Saved At": self.saved_at.isoformat() if self.saved_at else "",
        }
        # Enrichment fields (when available)
        if self.enrichment_json:
            try:
                enr = json.loads(self.enrichment_json)
                if enr.get("oig_excluded") is True:
                    fields["OIG Excluded"] = "Yes"
                elif enr.get("oig_excluded") is False:
                    fields["OIG Excluded"] = "No"
                else:
                    fields["OIG Excluded"] = ""
                if enr.get("medicare_enrolled") is True:
                    fields["Medicare Enrolled"] = "Yes"
                elif enr.get("medicare_enrolled") is False:
                    fields["Medicare Enrolled"] = "No"
                if enr.get("total_payments") is not None:
                    fields["Industry Payments ($)"] = f"{enr['total_payments']:.2f}"
            except (json.JSONDecodeError, TypeError):
                pass
        return fields


class SearchHistoryEntry(BaseModel):
    """Record of a past search."""

    id: int | None = None
    query_params: dict[str, str]
    result_count: int
    searched_at: datetime | None = None
