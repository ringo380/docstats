"""Rich formatting for terminal output and referral exports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from docstats.models import NPIResult, NPIResponse, SavedProvider, SearchHistoryEntry

if TYPE_CHECKING:
    from docstats.domain.orgs import Organization
    from docstats.domain.patients import Patient
    from docstats.domain.referrals import (
        Referral,
        ReferralAllergy,
        ReferralAttachment,
        ReferralDiagnosis,
        ReferralMedication,
    )

console = Console()


def results_table(response: NPIResponse) -> Table:
    """Format search results as a Rich table."""
    table = Table(
        title=f"Search Results ({response.result_count} found)",
        show_lines=True,
        expand=True,
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("NPI", style="cyan", width=12)
    table.add_column("Type", width=6)
    table.add_column("Name", style="bold", min_width=20)
    table.add_column("Specialty", min_width=15)
    table.add_column("Location", min_width=15)
    table.add_column("Phone", width=16)

    for i, result in enumerate(response.results, 1):
        addr = result.location_address
        location = f"{addr.city}, {addr.state}" if addr else ""
        type_label = "[green]Ind[/green]" if result.is_individual else "[blue]Org[/blue]"

        table.add_row(
            str(i),
            result.number,
            type_label,
            result.display_name,
            result.primary_specialty,
            location,
            result.phone or "",
        )

    return table


def provider_detail(result: NPIResult) -> Panel:
    """Format a single provider as a Rich panel with full details."""
    lines: list[str] = []

    # Header
    entity_tag = (
        "[green]Individual[/green]" if result.is_individual else "[blue]Organization[/blue]"
    )
    lines.append(f"[bold]{result.display_name}[/bold]  {entity_tag}")
    lines.append(f"NPI: [cyan]{result.number}[/cyan]")
    if result.enumeration_date:
        lines.append(f"Enumeration Date: {result.enumeration_date}")
    lines.append(f"Status: {result.status}")
    lines.append("")

    # Taxonomies / Specialties
    if result.taxonomies:
        lines.append("[bold underline]Specialties[/bold underline]")
        for t in result.taxonomies:
            primary = " [yellow]★ Primary[/yellow]" if t.primary else ""
            license_info = f"  License: {t.license} ({t.state})" if t.license else ""
            lines.append(f"  • {t.desc} [{t.code}]{primary}{license_info}")
        lines.append("")

    # Addresses
    if result.addresses:
        lines.append("[bold underline]Addresses[/bold underline]")
        for addr in result.addresses:
            purpose = addr.address_purpose or "UNKNOWN"
            lines.append(f"  [{purpose}]")
            lines.append(f"    {addr.address_1}")
            if addr.address_2:
                lines.append(f"    {addr.address_2}")
            lines.append(f"    {addr.city}, {addr.state} {addr.formatted_postal}")
            if addr.formatted_phone:
                lines.append(f"    Phone: {addr.formatted_phone}")
            if addr.formatted_fax:
                lines.append(f"    Fax:   {addr.formatted_fax}")
        lines.append("")

    # Other names
    if result.other_names:
        lines.append("[bold underline]Other Names[/bold underline]")
        for name in result.other_names:
            if name.organization_name:
                lines.append(f"  • {name.organization_name} ({name.type or 'alias'})")
            elif name.first_name or name.last_name:
                full = f"{name.first_name or ''} {name.last_name or ''}".strip()
                lines.append(f"  • {full} ({name.type or 'alias'})")
        lines.append("")

    # Identifiers
    if result.identifiers:
        lines.append("[bold underline]Identifiers[/bold underline]")
        for ident in result.identifiers:
            desc = ident.get("desc", ident.get("code", ""))
            val = ident.get("identifier", "")
            state = ident.get("state", "")
            lines.append(f"  • {desc}: {val}" + (f" ({state})" if state else ""))
        lines.append("")

    content = "\n".join(lines)
    return Panel(content, title=f"Provider Detail — {result.number}", border_style="cyan")


def saved_table(providers: list[SavedProvider]) -> Table:
    """Format saved providers as a Rich table."""
    table = Table(title="Saved Providers", show_lines=True, expand=True)
    table.add_column("NPI", style="cyan", width=12)
    table.add_column("Type", width=6)
    table.add_column("Name", style="bold", min_width=20)
    table.add_column("Specialty", min_width=15)
    table.add_column("Location", min_width=15)
    table.add_column("Phone", width=16)
    table.add_column("Notes", min_width=10)

    for p in providers:
        location = ""
        if p.address_city and p.address_state:
            location = f"{p.address_city}, {p.address_state}"
        type_label = "[green]Ind[/green]" if p.entity_type == "Individual" else "[blue]Org[/blue]"

        table.add_row(
            p.npi,
            type_label,
            p.display_name,
            p.specialty or "",
            location,
            p.phone or "",
            p.notes or "",
        )

    return table


def history_table(entries: list[SearchHistoryEntry]) -> Table:
    """Format search history as a Rich table."""
    table = Table(title="Search History", show_lines=True, expand=True)
    table.add_column("Date", width=20)
    table.add_column("Parameters", min_width=30)
    table.add_column("Results", width=8, justify="right")

    for entry in entries:
        params_str = ", ".join(
            f"{k}={v}"
            for k, v in entry.query_params.items()
            if k not in ("version", "limit", "skip")
        )
        date_str = entry.searched_at.strftime("%Y-%m-%d %H:%M") if entry.searched_at else ""
        table.add_row(date_str, params_str, str(entry.result_count))

    return table


def referral_export(
    result: NPIResult,
    appt_address: str | None = None,
    appt_suite: str | None = None,
    appt_phone: str | None = None,
    appt_fax: str | None = None,
    is_televisit: bool = False,
) -> str:
    """Generate a plain-text referral-ready summary.

    Suitable for pasting into referral forms or faxing.
    If appt_address is provided, it is appended as a separate section.
    """
    lines: list[str] = []
    lines.append("=" * 50)
    lines.append("PROVIDER REFERRAL INFORMATION")
    lines.append("=" * 50)
    lines.append("")
    lines.append(f"Provider: {result.display_name}")
    lines.append(f"NPI: {result.number}")
    lines.append(f"Type: {result.entity_label}")
    lines.append(f"Specialty: {result.primary_specialty}")

    # Primary taxonomy code (useful for referral forms)
    pt = result.primary_taxonomy
    if pt:
        lines.append(f"Taxonomy Code: {pt.code}")

    lines.append("")

    # Location address
    addr = result.location_address
    if addr:
        lines.append("Practice Address:")
        lines.append(f"  {addr.address_1}")
        if addr.address_2:
            lines.append(f"  {addr.address_2}")
        lines.append(f"  {addr.city}, {addr.state} {addr.formatted_postal}")
        if addr.formatted_phone:
            lines.append(f"  Phone: {addr.formatted_phone}")
        if addr.formatted_fax:
            lines.append(f"  Fax: {addr.formatted_fax}")

    # Mailing address if different
    mail = result.mailing_address
    if mail and addr and mail.address_1 != addr.address_1:
        lines.append("")
        lines.append("Mailing Address:")
        lines.append(f"  {mail.address_1}")
        if mail.address_2:
            lines.append(f"  {mail.address_2}")
        lines.append(f"  {mail.city}, {mail.state} {mail.formatted_postal}")

    if is_televisit:
        lines.append("")
        lines.append("-" * 50)
        lines.append("TELEVISIT")
        lines.append("-" * 50)
        lines.append("  This provider is seen via telehealth/virtual visit.")
    elif appt_address:
        lines.append("")
        lines.append("-" * 50)
        lines.append("MY APPOINTMENT LOCATION")
        lines.append("-" * 50)
        lines.append(f"  {appt_address}")
        if appt_suite:
            lines.append(f"  {appt_suite}")
        if appt_phone:
            lines.append(f"  Phone: {appt_phone}")
        if appt_fax:
            lines.append(f"  Fax: {appt_fax}")
        lines.append("  (This location may differ from the NPI registry address above)")

    lines.append("")
    lines.append(f"Enumeration Date: {result.enumeration_date or 'N/A'}")
    lines.append(f"Status: {result.status}")
    lines.append("")
    lines.append("=" * 50)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# AMA-style referral letter — plaintext
# ─────────────────────────────────────────────────────────────────────


def _user_typed_name(user: dict[str, Any] | None) -> str:
    if not user:
        return "Requesting Clinician"
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    if first and last:
        name = f"{first} {last}"
    else:
        name = user.get("display_name") or user.get("email") or "Requesting Clinician"
    creds = user.get("credentials")
    if creds:
        name = f"{name}, {creds}"
    return name


def _patient_age(dob: str | None, as_of: datetime | None = None) -> int | None:
    if not dob:
        return None
    try:
        birth = datetime.strptime(dob, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = (as_of or datetime.now(tz=timezone.utc)).date()
    years = today.year - birth.year
    if (today.month, today.day) < (birth.month, birth.day):
        years -= 1
    return max(years, 0)


def referral_letter_text(
    referral: "Referral",
    patient: "Patient",
    *,
    organization: "Organization | None" = None,
    current_user: dict[str, Any] | None = None,
    diagnoses: list["ReferralDiagnosis"] | None = None,
    medications: list["ReferralMedication"] | None = None,
    allergies: list["ReferralAllergy"] | None = None,
    attachments: list["ReferralAttachment"] | None = None,
    insurance_plan: Any | None = None,
    include_payer: bool = False,
    generated_at: datetime | None = None,
) -> str:
    """Plaintext rendering of an AMA-style referral letter.

    Mirrors the structure of the rendered PDF (Bunik 7-domain narrative)
    in monospace plain text. Suitable for the "Copy as text" button on
    the referral detail page or for piping into legacy systems that
    accept text-only referrals.

    ``include_payer=True`` flips the body into Scenario B
    (Medical Necessity / Prior Auth) flavor: payer destination block,
    CPT/HCPCS list, conservative-therapy summary, medical-necessity
    statement.
    """
    now = generated_at or datetime.now(tz=timezone.utc)
    diagnoses = list(diagnoses or [])
    medications = list(medications or [])
    allergies = list(allergies or [])
    attachments = list(attachments or [])

    lines: list[str] = []
    sep = "=" * 72

    # Letterhead
    lines.append(sep)
    if organization:
        lines.append(organization.name.upper())
        addr_parts: list[str] = []
        if organization.address_line1:
            addr_parts.append(organization.address_line1)
        if organization.address_line2:
            addr_parts.append(organization.address_line2)
        csz: list[str] = []
        if organization.address_city:
            csz.append(organization.address_city)
        if organization.address_state:
            csz.append(organization.address_state)
        if csz:
            csz_line = ", ".join(csz)
            if organization.address_zip:
                csz_line += " " + organization.address_zip
            addr_parts.append(csz_line)
        if addr_parts:
            lines.append(" · ".join(addr_parts))
        contact: list[str] = []
        if organization.phone:
            contact.append(f"Phone: {organization.phone}")
        if organization.fax:
            contact.append(f"Fax: {organization.fax}")
        if organization.npi:
            contact.append(f"NPI: {organization.npi}")
        if contact:
            lines.append(" · ".join(contact))
    elif referral.referring_organization:
        lines.append(referral.referring_organization)
    else:
        lines.append("(Practice information not configured)")
    lines.append(sep)
    lines.append("")
    lines.append(now.strftime("%B %d, %Y"))
    lines.append("")

    # Addressee
    if include_payer:
        target = "Utilization Management / Prior Authorization"
        if insurance_plan:
            target = f"{insurance_plan.payer_name} — {target}"
        lines.append(f"To: {target}")
    else:
        if referral.receiving_provider_name:
            lines.append(referral.receiving_provider_name)
        if referral.receiving_organization_name:
            lines.append(referral.receiving_organization_name)
        if referral.specialty_desc and not referral.receiving_provider_name:
            lines.append(f"{referral.specialty_desc} Department")
    lines.append("")

    # RE: line
    age = _patient_age(patient.date_of_birth, as_of=now)
    re_parts = [patient.display_name]
    if patient.date_of_birth:
        dob_part = f"DOB {patient.date_of_birth}"
        if age is not None:
            dob_part += f" (age {age})"
        re_parts.append(dob_part)
    if patient.sex:
        re_parts.append(patient.sex)
    if patient.mrn:
        re_parts.append(f"MRN {patient.mrn}")
    lines.append(f"RE: {' · '.join(re_parts)}")
    lines.append("")

    # Salutation
    if include_payer:
        lines.append("To Whom It May Concern:")
    else:
        if referral.receiving_provider_name:
            last_name = referral.receiving_provider_name.split(" ")[-1]
            lines.append(f"Dear Dr. {last_name}:")
        else:
            lines.append("Dear Colleague:")
    lines.append("")

    # Body sections
    if include_payer:
        lines.append("REQUEST TYPE")
        lines.append("-" * 72)
        if referral.urgency in ("urgent", "stat"):
            lines.append("Expedited review (≤72 hours)")
        else:
            lines.append("Routine review (≤14 days)")
        lines.append("")

        lines.append("MEMBER")
        lines.append("-" * 72)
        lines.append(patient.display_name)
        if patient.date_of_birth:
            lines.append(f"DOB: {patient.date_of_birth}")
        if patient.mrn:
            lines.append(f"MRN: {patient.mrn}")
        if insurance_plan:
            lines.append(
                f"Plan: {insurance_plan.payer_name}"
                + (f" — {insurance_plan.plan_type}" if insurance_plan.plan_type else "")
            )
        lines.append("")

        lines.append("REQUESTING PROVIDER")
        lines.append("-" * 72)
        lines.append(_user_typed_name(current_user))
        if current_user and current_user.get("individual_npi"):
            lines.append(f"Individual NPI: {current_user['individual_npi']}")
        if organization and organization.npi:
            lines.append(f"Group NPI: {organization.npi}")
        lines.append("")

        if referral.requested_service or referral.cpt_codes:
            lines.append("SERVICE REQUESTED")
            lines.append("-" * 72)
            if referral.cpt_codes:
                cpt_list = referral.cpt_codes
                if isinstance(cpt_list, str):
                    import json as _json

                    try:
                        cpt_list = _json.loads(cpt_list)
                    except ValueError:
                        cpt_list = []
                for code in cpt_list or []:
                    if not isinstance(code, dict):
                        continue
                    parts = [code.get("code", "—")]
                    if code.get("description"):
                        parts.append(code["description"])
                    if code.get("units"):
                        parts.append(f"x{code['units']}")
                    lines.append("  " + " · ".join(parts))
            elif referral.requested_service:
                lines.append(f"  {referral.requested_service}")
            if referral.place_of_service_code:
                lines.append(f"  Place of Service: {referral.place_of_service_code}")
            lines.append("")

    lines.append("REASON FOR REFERRAL")
    lines.append("-" * 72)
    lines.append(referral.reason or "(See clinical question below.)")
    if referral.clinical_question:
        lines.append("")
        lines.append(f"Specific clinical question: {referral.clinical_question}")
    lines.append("")
    lines.append(f"Urgency: {referral.urgency.upper()}")
    lines.append("")

    if referral.diagnosis_primary_icd or referral.diagnosis_primary_text or diagnoses:
        lines.append("WORKING DIAGNOSIS")
        lines.append("-" * 72)
        if referral.diagnosis_primary_icd or referral.diagnosis_primary_text:
            primary = ""
            if referral.diagnosis_primary_icd:
                primary = referral.diagnosis_primary_icd
            if referral.diagnosis_primary_text:
                primary += (" — " if primary else "") + referral.diagnosis_primary_text
            lines.append(f"Primary: {primary}")
        secondary = [d for d in diagnoses if not d.is_primary]
        for d in secondary:
            line = f"  - {d.icd10_code}"
            if d.icd10_desc:
                line += f" — {d.icd10_desc}"
            lines.append(line)
        lines.append("")

    if medications:
        lines.append("CURRENT MEDICATIONS")
        lines.append("-" * 72)
        for m in medications:
            parts = [m.name]
            if m.dose:
                parts.append(m.dose)
            if m.route:
                parts.append(m.route)
            if m.frequency:
                parts.append(m.frequency)
            lines.append("  - " + ", ".join(parts))
        lines.append("")

    lines.append("ALLERGIES")
    lines.append("-" * 72)
    if allergies:
        for a in allergies:
            line = f"  - {a.substance}"
            if a.reaction:
                line += f" — {a.reaction}"
            if a.severity:
                line += f" ({a.severity})"
            lines.append(line)
    else:
        lines.append("  No known drug allergies (NKDA).")
    lines.append("")

    if include_payer:
        lines.append("MEDICAL NECESSITY")
        lines.append("-" * 72)
        nec = getattr(referral, "medical_necessity_text", None)
        if nec:
            lines.append(nec)
        else:
            lines.append("(Medical necessity narrative not entered.)")
        lines.append("")
        lines.append("CONSERVATIVE / STEP THERAPY")
        lines.append("-" * 72)
        cons = getattr(referral, "conservative_therapy_tried", None)
        if cons:
            lines.append(cons)
        else:
            lines.append("(Conservative therapy history not entered.)")
        lines.append("")
    else:
        # Insurance one-liner (Scenario A)
        lines.append("INSURANCE / AUTHORIZATION")
        lines.append("-" * 72)
        if insurance_plan:
            plan_line = insurance_plan.payer_name
            if insurance_plan.plan_type:
                plan_line += f" ({insurance_plan.plan_type})"
            lines.append(plan_line)
        if referral.authorization_number:
            lines.append(
                f"Authorization #: {referral.authorization_number}"
                f" · status: {referral.authorization_status.replace('_', ' ')}"
            )
        else:
            lines.append(f"Authorization status: {referral.authorization_status.replace('_', ' ')}")
        lines.append("")

    if attachments:
        lines.append("ENCLOSURES")
        lines.append("-" * 72)
        for a in attachments:
            note = a.kind.replace("_", " ")
            if a.date_of_service:
                note += f", {a.date_of_service}"
            if a.checklist_only:
                note += ", to follow"
            lines.append(f"  - {a.label} ({note})")
        lines.append("")

    # Closing + signature
    if include_payer:
        lines.append("I attest that the requested service is medically necessary for this patient")
        lines.append("and that the information provided is accurate.")
    else:
        lines.append("Please don't hesitate to contact our office with any questions about this")
        lines.append("patient or the records enclosed. We appreciate your evaluation.")
    lines.append("")
    lines.append("Sincerely,")
    lines.append("")
    lines.append("")
    lines.append(_user_typed_name(current_user))
    if current_user:
        if current_user.get("individual_npi"):
            lines.append(f"NPI: {current_user['individual_npi']}")
        if current_user.get("state_license_number"):
            lic = f"License: {current_user['state_license_number']}"
            if current_user.get("state_license_state"):
                lic += f" ({current_user['state_license_state']})"
            lines.append(lic)
    if organization:
        contact: list[str] = []
        if organization.phone:
            contact.append(f"Phone: {organization.phone}")
        if organization.fax:
            contact.append(f"Fax: {organization.fax}")
        if contact:
            lines.append(" · ".join(contact))
    if current_user and current_user.get("email"):
        lines.append(f"Direct: {current_user['email']}")
    lines.append("")
    lines.append(sep)
    lines.append("CONFIDENTIAL — Protected Health Information (HIPAA, 45 CFR §164)")
    lines.append(sep)

    return "\n".join(lines)
