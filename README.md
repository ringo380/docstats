# docstats

Local-first NPI Registry lookup tool for HMO referral workflows. Queries the [CMS NPPES NPI Registry API](https://npiregistry.cms.hhs.gov/api-page) (v2.1) to find doctors, specialists, clinics, and organizations — then helps you save and export the referral details your PCP office needs.

## Setup

Requires Python 3.12+.

```bash
# Clone and install
cd docstats
pip install -e .

# For development
pip install -e ".[dev]"
```

## Usage

### Search providers

```bash
# Search by last name
docstats search --name smith --state CA

# Search by first + last name
docstats search --name johnson --first michael --state NY

# Search by specialty + location
docstats search --specialty "orthopedic" --city "San Francisco" --state CA

# Search organizations
docstats search --org "kaiser" --state CA

# Limit results
docstats search --name patel --state TX --limit 20

# Bypass cache for fresh results
docstats search --name lee --state CA --no-cache
```

### Look up by NPI

```bash
# Exact NPI lookup (must be 10 digits)
docstats lookup 1234567890
```

### View full details

```bash
# Shows all addresses, specialties, identifiers
docstats show 1234567890
```

### Save providers locally

```bash
# Save a provider for later reference
docstats save 1234567890 --notes "Preferred orthopedist, takes Blue Shield"

# List all saved providers
docstats saved

# Remove a saved provider
docstats remove 1234567890
```

### Export referral summary

```bash
# Plain text (for pasting into referral forms)
docstats export 1234567890

# JSON format
docstats export 1234567890 --format json
```

### View search history

```bash
docstats history
docstats history --limit 50
```

### Debug mode

```bash
docstats -v search --name smith --state CA
```

## Web UI

A browser-based interface is available as an optional install.

### Setup

```bash
pip install -e ".[web]"
```

### Launch

```bash
docstats web                     # http://127.0.0.1:8000
docstats web --port 3000         # custom port
docstats web --reload            # auto-reload for development
```

### Features

- **Search** with in-page results (htmx partial updates, no full page reloads)
- **Provider detail** with specialties, addresses, phone/fax
- **Save/remove** providers with one click
- **Referral export** with copy-to-clipboard and text download
- **Search history** with re-run links
- Shares the same SQLite database as the CLI

Built with FastAPI, Jinja2, htmx, and Pico CSS.

## Architecture

```
src/docstats/
├── cli.py          # Typer CLI commands
├── web.py          # FastAPI web application
├── client.py       # NPPES API client (httpx)
├── models.py       # Pydantic data models
├── normalize.py    # Name/phone/zip formatting
├── storage.py      # SQLite persistence
├── cache.py        # Response cache with TTL
├── scoring.py      # Result ranking and scoring
├── formatting.py   # Rich terminal output + referral export
└── templates/      # Jinja2 HTML templates (htmx + Pico CSS)
```

- **API client** talks to CMS NPPES only via documented parameters
- **Normalization** handles the API's UPPERCASE names, unformatted phones, and raw postal codes
- **SQLite** stores saved providers, search history, and response cache in a single DB at `~/.local/share/docstats/docstats.db`
- **Response cache** defaults to 24h TTL; bypass with `--no-cache`
- **Web and CLI share** the same database, cache, and business logic

## Data Source

All provider data comes from the [CMS National Plan and Provider Enumeration System (NPPES)](https://npiregistry.cms.hhs.gov/). This is public data — no PHI is stored or processed.

## Testing

```bash
pytest
pytest -v  # verbose
```

## Roadmap

### Near-term
- [ ] Local fuzzy matching across saved providers
- [ ] Compare mode for similar providers side-by-side
- [ ] Notes editing for saved providers
- [ ] CSV/JSON bulk export of saved providers

### Future
- [ ] Referral checklist generation
- [ ] Address deduplication heuristics
- [ ] Optional insurer directory cross-reference
- [ ] Shell completions
