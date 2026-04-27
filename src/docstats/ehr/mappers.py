"""FHIR Patient resource → ImportedPatient mapper.

Manual JSON parsing — `fhir.resources` is overkill for the single-resource
Phase 12.A. Tolerant of missing fields (FHIR Patient marks most elements 0..1
or 0..*); never raises on missing data, only on a non-Patient resourceType.
"""

from __future__ import annotations

from typing import Any

from docstats.domain.ehr import ImportedPatient


def _first_name(name_entry: dict) -> tuple[str | None, str | None, str | None]:
    """Return (first, middle, last) from a FHIR HumanName."""
    given: list[str] = name_entry.get("given") or []
    family = name_entry.get("family")
    first = given[0] if given else None
    middle = " ".join(given[1:]) or None if len(given) > 1 else None
    return first, middle, family


def _select_name(names: list[dict]) -> dict:
    """Prefer use=official, fall back to first entry."""
    for n in names:
        if n.get("use") == "official":
            return n
    return names[0] if names else {}


def _extract_mrn(identifiers: list[dict]) -> str | None:
    """Pick the identifier where type.coding[0].code == 'MR'."""
    for ident in identifiers:
        coding_list = (ident.get("type") or {}).get("coding") or []
        for coding in coding_list:
            if coding.get("code") == "MR":
                return ident.get("value")
    # Fallback: some Epic sandboxes don't tag MRN consistently — take the
    # first identifier with a usable value.
    for ident in identifiers:
        val = ident.get("value")
        if isinstance(val, str) and val:
            return val
    return None


def _extract_telecom(telecoms: list[dict]) -> tuple[str | None, str | None]:
    phone: str | None = None
    email: str | None = None
    for t in telecoms:
        system = t.get("system")
        value = t.get("value")
        if not value:
            continue
        if system == "phone" and phone is None:
            phone = value
        elif system == "email" and email is None:
            email = value
    return phone, email


def _extract_address(addresses: list[dict]) -> dict[str, str | None]:
    """Return address fields from preferred address (use=home, else first)."""
    if not addresses:
        return {
            "address_line1": None,
            "address_line2": None,
            "address_city": None,
            "address_state": None,
            "address_zip": None,
        }
    addr = next((a for a in addresses if a.get("use") == "home"), addresses[0])
    lines: list[str] = addr.get("line") or []
    return {
        "address_line1": lines[0] if lines else None,
        "address_line2": lines[1] if len(lines) > 1 else None,
        "address_city": addr.get("city"),
        "address_state": addr.get("state"),
        "address_zip": addr.get("postalCode"),
    }


def parse_fhir_patient(resource: dict[str, Any]) -> ImportedPatient:
    """Map a FHIR R4 Patient resource to an ImportedPatient."""
    if resource.get("resourceType") != "Patient":
        raise ValueError(f"Expected resourceType=Patient, got {resource.get('resourceType')!r}")
    fhir_id = resource.get("id")
    if not fhir_id:
        raise ValueError("FHIR Patient resource is missing 'id'")

    names = resource.get("name") or []
    first, middle, last = _first_name(_select_name(names)) if names else (None, None, None)

    phone, email = _extract_telecom(resource.get("telecom") or [])
    addr = _extract_address(resource.get("address") or [])

    return ImportedPatient(
        fhir_id=fhir_id,
        mrn=_extract_mrn(resource.get("identifier") or []),
        first_name=first,
        last_name=last,
        middle_name=middle,
        date_of_birth=resource.get("birthDate"),
        gender=resource.get("gender"),
        phone=phone,
        email=email,
        **addr,
    )
