"""FHIR resource mappers for EHR import.

Manual JSON parsing — tolerant of missing fields; never raises on missing
data. Only raises ValueError on wrong resourceType (for Patient) or skips
the resource silently (for clinical resources).
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


# ---------------------------------------------------------------------------
# Clinical resource mappers (Phase 12.B)
# All tolerate missing fields; skip resources with wrong resourceType silently.
# ---------------------------------------------------------------------------


def parse_fhir_conditions(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map FHIR R4 Condition resources to add_referral_diagnosis kwargs.

    Returns a list of dicts with keys: icd10_code, icd10_desc, is_primary.
    The first entry gets is_primary=True; remaining entries is_primary=False.
    Resources with wrong resourceType or no usable coding are skipped.
    """
    out: list[dict[str, Any]] = []
    for resource in resources:
        if resource.get("resourceType") != "Condition":
            continue
        icd10_code: str | None = None
        icd10_desc: str | None = resource.get("code", {}).get("text")
        for coding in (resource.get("code") or {}).get("coding") or []:
            system: str = coding.get("system", "")
            if "icd-10" in system.lower() or "icd10" in system.lower():
                icd10_code = coding.get("code")
                icd10_desc = coding.get("display") or icd10_desc
                break
            # Fall back to SNOMED if no ICD found yet
            if not icd10_code and ("snomed" in system.lower() or "sct" in system):
                icd10_code = coding.get("code")
                icd10_desc = coding.get("display") or icd10_desc
        if not icd10_code and not icd10_desc:
            continue
        out.append(
            {
                "icd10_code": icd10_code,
                "icd10_desc": icd10_desc,
                "is_primary": len(out) == 0,
            }
        )
    return out


def parse_fhir_medications(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map FHIR R4 MedicationStatement or MedicationRequest resources to
    add_referral_medication kwargs.

    Both resource types use the same ``medicationCodeableConcept`` field for
    the drug name and ``dosage`` / ``dosageInstruction`` for dosing — Epic
    uses MedicationStatement; Cerner uses MedicationRequest.

    Returns a list of dicts with keys: name, dose, route, frequency.
    Resources with wrong resourceType or no usable name are skipped.
    """
    out: list[dict[str, Any]] = []
    for resource in resources:
        rtype = resource.get("resourceType")
        if rtype not in ("MedicationStatement", "MedicationRequest"):
            continue
        med_ref = resource.get("medicationCodeableConcept") or {}
        name: str | None = med_ref.get("text")
        for coding in med_ref.get("coding") or []:
            name = name or coding.get("display") or coding.get("code")
        if not name:
            # MedicationRequest may use medicationReference instead.
            med_ref_field = resource.get("medicationReference") or {}
            name = med_ref_field.get("display")
        if not name:
            continue
        # MedicationStatement uses "dosage"; MedicationRequest uses "dosageInstruction".
        dosage_list = resource.get("dosage") or resource.get("dosageInstruction") or []
        dosage = dosage_list[0] if dosage_list else {}
        dose_qty = (dosage.get("doseAndRate") or [{}])[0].get("doseQuantity") or {}
        dose = f"{dose_qty.get('value', '')} {dose_qty.get('unit', '')}".strip() or None
        route_cc = dosage.get("route") or {}
        route: str | None = route_cc.get("text") or next(
            (c.get("display") for c in route_cc.get("coding") or []), None
        )
        timing = (dosage.get("timing") or {}).get("repeat") or {}
        frequency: str | None = None
        if timing.get("frequency") and timing.get("period") and timing.get("periodUnit"):
            frequency = f"{timing['frequency']} per {timing['period']}{timing['periodUnit']}"
        else:
            frequency = (dosage.get("timing") or {}).get("code", {}).get("text")
        out.append({"name": name, "dose": dose, "route": route, "frequency": frequency})
    return out


def parse_fhir_allergies(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map FHIR R4 AllergyIntolerance resources to add_referral_allergy kwargs.

    Returns a list of dicts with keys: substance, reaction, severity.
    Resources with wrong resourceType or no usable substance are skipped.
    """
    out: list[dict[str, Any]] = []
    for resource in resources:
        if resource.get("resourceType") != "AllergyIntolerance":
            continue
        substance_cc = resource.get("code") or {}
        substance: str | None = substance_cc.get("text") or next(
            (c.get("display") or c.get("code") for c in substance_cc.get("coding") or []), None
        )
        if not substance:
            continue
        reactions = resource.get("reaction") or []
        first_reaction = reactions[0] if reactions else {}
        manifestations = first_reaction.get("manifestation") or [{}]
        reaction_text: str | None = (manifestations[0].get("text") or None) or next(
            (c.get("display") or c.get("code") for c in manifestations[0].get("coding") or []),
            None,
        )
        severity: str | None = first_reaction.get("severity")
        out.append({"substance": substance, "reaction": reaction_text, "severity": severity})
    return out


def parse_fhir_document_references(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map FHIR R4 DocumentReference resources to add_referral_attachment kwargs.

    Returns a list of dicts with keys:
      label, date_of_service, content_url, content_type, inline_data.

    content_url — the attachment.url from content[0] (may be a relative path
    like ``Binary/abc123``; resolution against the FHIR base is the caller's job).
    content_type — MIME from attachment.contentType (informational; route layer
    sniffs actual bytes before trusting this).
    inline_data — base64-encoded bytes from attachment.data (some EHRs embed
    content directly instead of a URL).

    Resources with wrong resourceType are skipped.
    """
    out: list[dict[str, Any]] = []
    for resource in resources:
        if resource.get("resourceType") != "DocumentReference":
            continue
        type_cc = resource.get("type") or {}
        label: str | None = type_cc.get("text") or next(
            (c.get("display") or c.get("code") for c in type_cc.get("coding") or []), None
        )
        label = label or "Imported document"
        date_of_service: str | None = (resource.get("date") or "")[:10] or None

        content_list = resource.get("content") or []
        attachment = (content_list[0] or {}).get("attachment") or {} if content_list else {}
        content_url: str | None = attachment.get("url") or None
        content_type: str | None = attachment.get("contentType") or None
        inline_data: str | None = attachment.get("data") or None

        out.append(
            {
                "label": label,
                "date_of_service": date_of_service,
                "content_url": content_url,
                "content_type": content_type,
                "inline_data": inline_data,
            }
        )
    return out
