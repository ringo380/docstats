"""CLI interface for docstats using Typer."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from docstats.cache import ResponseCache
from docstats.client import NPPESClient, NPPESError
from docstats.formatting import (
    console as fmt_console,
    history_table,
    provider_detail,
    referral_export,
    results_table,
    saved_table,
)
from docstats.services import search_providers as svc_search, save_provider as svc_save
from docstats.storage import Storage, get_db_path

app = typer.Typer(
    name="docstats",
    help="NPI Registry lookup tool for HMO referral workflows.",
    no_args_is_help=True,
)
console = Console()

# Shared state initialized lazily
_storage: Storage | None = None
_cache: ResponseCache | None = None
_client: NPPESClient | None = None


_CLI_USER_EMAIL = "cli@localhost"


def _get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage


def _get_cli_user_id() -> int:
    """Get or create the local CLI user for single-user SQLite mode."""
    storage = _get_storage()
    user = storage.get_user_by_email(_CLI_USER_EMAIL)
    if user:
        return user["id"]
    return storage.create_user(_CLI_USER_EMAIL, "")


def _get_cache() -> ResponseCache:
    global _cache
    if _cache is None:
        db_path = get_db_path()
        _cache = ResponseCache(db_path)
    return _cache


def _get_client(use_cache: bool = True) -> NPPESClient:
    global _client
    if _client is None:
        cache = _get_cache() if use_cache else None
        _client = NPPESClient(cache=cache)
    return _client


@app.command()
def search(
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="Last name (individual)")] = None,
    first: Annotated[Optional[str], typer.Option("--first", "-f", help="First name")] = None,
    org: Annotated[Optional[str], typer.Option("--org", "-o", help="Organization name")] = None,
    specialty: Annotated[Optional[str], typer.Option("--specialty", "-s", help="Taxonomy/specialty")] = None,
    state: Annotated[Optional[str], typer.Option("--state", help="2-letter state code")] = None,
    city: Annotated[Optional[str], typer.Option("--city", help="City")] = None,
    zip_code: Annotated[Optional[str], typer.Option("--zip", "-z", help="Postal/ZIP code")] = None,
    entity_type: Annotated[Optional[str], typer.Option("--type", "-t", help="NPI-1 (individual) or NPI-2 (organization)")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max results")] = 10,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass response cache")] = False,
) -> None:
    """Search providers by name, specialty, or location."""
    if not any([name, first, org, specialty, city, zip_code]):
        console.print("[red]Provide at least one search parameter (--name, --first, --org, --specialty, --city, or --zip).[/red]")
        raise typer.Exit(1)

    client = _get_client(use_cache=not no_cache)
    storage = _get_storage()
    try:
        response = svc_search(
            client, storage,
            last_name=name,
            first_name=first,
            organization_name=org,
            taxonomy_description=specialty,
            state=state,
            city=city,
            postal_code=zip_code,
            enumeration_type=entity_type,
            limit=limit,
            use_cache=not no_cache,
            user_id=_get_cli_user_id(),
        )
    except NPPESError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    if response.result_count == 0:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit(0)

    console.print(results_table(response))
    console.print(
        f"\n[dim]Use [cyan]docstats show <NPI>[/cyan] for details or "
        f"[cyan]docstats save <NPI>[/cyan] to save a provider.[/dim]"
    )


@app.command()
def lookup(
    npi: Annotated[str, typer.Argument(help="10-digit NPI number")],
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass response cache")] = False,
) -> None:
    """Look up a provider by exact NPI number."""
    client = _get_client(use_cache=not no_cache)
    try:
        result = client.lookup(npi, use_cache=not no_cache)
    except NPPESError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    if result is None:
        console.print(f"[yellow]No provider found for NPI {npi}.[/yellow]")
        raise typer.Exit(0)

    console.print(provider_detail(result))


@app.command()
def show(
    npi: Annotated[str, typer.Argument(help="10-digit NPI number")],
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass response cache")] = False,
) -> None:
    """Show full details for a provider (checks saved providers first, then API)."""
    # Check saved providers first
    storage = _get_storage()
    saved = storage.get_provider(npi, _get_cli_user_id())
    if saved:
        result = saved.to_npi_result()
        console.print(provider_detail(result))
        if saved.notes:
            console.print(f"\n[dim]Notes: {saved.notes}[/dim]")
        return

    # Fall back to API
    client = _get_client(use_cache=not no_cache)
    try:
        result = client.lookup(npi, use_cache=not no_cache)
    except NPPESError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    if result is None:
        console.print(f"[yellow]No provider found for NPI {npi}.[/yellow]")
        raise typer.Exit(0)

    console.print(provider_detail(result))


@app.command()
def save(
    npi: Annotated[str, typer.Argument(help="10-digit NPI number")],
    notes: Annotated[Optional[str], typer.Option("--notes", help="Notes about this provider")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass response cache")] = False,
) -> None:
    """Save a provider to local database for future reference."""
    client = _get_client(use_cache=not no_cache)
    storage = _get_storage()
    try:
        provider = svc_save(client, storage, npi, _get_cli_user_id(), notes=notes, use_cache=not no_cache)
    except NPPESError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)
    except ValueError as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(1)

    console.print(f"[green]Saved:[/green] {provider.display_name} (NPI: {provider.npi})")


@app.command()
def saved(
    # Named 'saved' instead of 'list' to avoid Python builtin shadowing
    search: Annotated[Optional[str], typer.Option("--search", "-s", help="Filter saved providers by name, NPI, specialty, or notes")] = None,
) -> None:
    """List all saved providers."""
    storage = _get_storage()
    user_id = _get_cli_user_id()

    if search:
        providers = storage.search_providers(user_id, search)
    else:
        providers = storage.list_providers(user_id)

    if not providers:
        if search:
            console.print(f"[yellow]No saved providers matching [cyan]{search}[/cyan].[/yellow]")
        else:
            console.print("[yellow]No saved providers. Use [cyan]docstats save <NPI>[/cyan] to save one.[/yellow]")
        raise typer.Exit(0)

    console.print(saved_table(providers))


@app.command()
def export(
    npi: Annotated[str, typer.Argument(help="10-digit NPI number")],
    fmt: Annotated[str, typer.Option("--format", help="Output format: text or json")] = "text",
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass response cache")] = False,
) -> None:
    """Export a referral-ready summary for a provider."""
    # Check saved providers first, then API
    storage = _get_storage()
    saved_prov = storage.get_provider(npi, _get_cli_user_id())

    if saved_prov:
        result = saved_prov.to_npi_result()
    else:
        client = _get_client(use_cache=not no_cache)
        try:
            result = client.lookup(npi, use_cache=not no_cache)
        except NPPESError as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)

        if result is None:
            console.print(f"[yellow]No provider found for NPI {npi}.[/yellow]")
            raise typer.Exit(1)

    if fmt == "json":
        print(result.model_dump_json(indent=2))
    else:
        print(referral_export(result))


@app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", "-l", help="Number of entries to show")] = 20,
) -> None:
    """View recent search history."""
    storage = _get_storage()
    entries = storage.get_history(limit=limit, user_id=_get_cli_user_id())

    if not entries:
        console.print("[yellow]No search history yet.[/yellow]")
        raise typer.Exit(0)

    console.print(history_table(entries))


@app.command()
def note(
    npi: Annotated[str, typer.Argument(help="10-digit NPI number")],
    text: Annotated[str, typer.Argument(help="Note text (use empty string to clear)")],
) -> None:
    """Add or update a note on a saved provider."""
    storage = _get_storage()
    notes = text.strip() or None
    if storage.update_notes(npi, notes, _get_cli_user_id()):
        if notes:
            console.print(f"[green]Note updated for NPI {npi}.[/green]")
        else:
            console.print(f"[green]Note cleared for NPI {npi}.[/green]")
    else:
        console.print(f"[yellow]No saved provider found with NPI {npi}.[/yellow]")


@app.command()
def remove(
    npi: Annotated[str, typer.Argument(help="NPI of provider to remove")],
) -> None:
    """Remove a saved provider from the local database."""
    storage = _get_storage()
    if storage.delete_provider(npi, _get_cli_user_id()):
        console.print(f"[green]Removed provider {npi} from saved list.[/green]")
    else:
        console.print(f"[yellow]No saved provider found with NPI {npi}.[/yellow]")


@app.command(name="export-all")
def export_all(
    fmt: Annotated[str, typer.Option("--format", "-f", help="Output format: csv or json")] = "csv",
    output: Annotated[Optional[str], typer.Option("--output", "-o", help="Output file (default: stdout)")] = None,
) -> None:
    """Export all saved providers as CSV or JSON."""
    import csv
    import io

    storage = _get_storage()
    providers = storage.list_providers(_get_cli_user_id())

    if not providers:
        console.print("[yellow]No saved providers to export.[/yellow]")
        raise typer.Exit(0)

    if fmt == "json":
        data = [p.export_fields() for p in providers]
        text = json.dumps(data, indent=2)
    elif fmt == "csv":
        buf = io.StringIO()
        rows = [p.export_fields() for p in providers]
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        text = buf.getvalue()
    else:
        console.print(f"[red]Unknown format: {fmt}. Use csv or json.[/red]")
        raise typer.Exit(1)

    if output:
        Path(output).write_text(text)
        console.print(f"[green]Exported {len(providers)} providers to {output}[/green]")
    else:
        print(text)


@app.command()
def web(
    host: Annotated[str, typer.Option("--host", "-h", help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port number")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload on code changes")] = False,
) -> None:
    """Launch the web UI."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Web dependencies not installed. Run: pip install docstats[web][/red]")
        raise typer.Exit(1)
    console.print(f"[green]Starting docstats web UI at http://{host}:{port}[/green]")
    uvicorn.run("docstats.web:app", host=host, port=port, reload=reload)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging")] = False,
) -> None:
    """NPI Registry lookup tool for HMO referral workflows."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")
