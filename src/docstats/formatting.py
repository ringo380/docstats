"""Rich formatting for terminal output and referral exports."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from docstats.models import NPIResult, NPIResponse, SavedProvider, SearchHistoryEntry

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
    entity_tag = "[green]Individual[/green]" if result.is_individual else "[blue]Organization[/blue]"
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
            f"{k}={v}" for k, v in entry.query_params.items()
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
