# Referral Request Tool — Design Spec

**Created**: 2026-04-01
**Status**: In Progress

## Overview

Transform docstats from a pure NPI lookup tool into a referral request builder. Users search the NPPES registry, save providers they want referrals to, then promote those saved providers onto named referral lists — with custom appointment location/phone overrides for where they actually visit. The tool generates a multi-provider referral request document in both plain text and printable formats.

---

## Context

HMO/insurance referral requests require specific provider information that the NPI registry partially provides but doesn't fully capture. The appointment address and phone often differ from what's listed in NPPES (e.g., a doctor listed at a hospital's main address but seen at a satellite clinic). Users need to attach a reason for referral and scheduling notes per doctor, then hand off a clean document to their PCP or submit it to their insurer.

---

## Data Model

Two new SQLite tables, added via `CREATE TABLE IF NOT EXISTS` in `storage.py` at startup. The `Storage.__init__` must also execute `PRAGMA foreign_keys = ON` per connection for `ON DELETE CASCADE` to be enforced (SQLite disables FK enforcement by default).

### `referral_lists`
```sql
CREATE TABLE IF NOT EXISTS referral_lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### `referral_list_entries`
```sql
CREATE TABLE IF NOT EXISTS referral_list_entries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id           INTEGER NOT NULL REFERENCES referral_lists(id) ON DELETE CASCADE,
    npi               TEXT NOT NULL REFERENCES saved_providers(npi) ON DELETE CASCADE,
    override_address_1 TEXT,
    override_city     TEXT,
    override_state    TEXT,
    override_zip      TEXT,
    override_phone    TEXT,
    notes             TEXT,
    reason            TEXT,
    added_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(list_id, npi)
);
```

**Cascade rules:**
- Deleting a referral list → removes its entries (ON DELETE CASCADE)
- Removing a saved provider → removes their referral list entries (ON DELETE CASCADE)
- Removing a provider from a referral list → saved provider is unaffected

`saved_providers` is unchanged. A provider must be saved before being added to a referral list.

### New Pydantic models (`models.py`)

```python
class ReferralList(BaseModel):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime

class ReferralListEntry(BaseModel):
    id: int
    list_id: int
    npi: str
    override_address_1: str | None = None
    override_city: str | None = None
    override_state: str | None = None
    override_zip: str | None = None
    override_phone: str | None = None
    notes: str | None = None
    reason: str | None = None
    added_at: datetime

    # Resolved display fields (populated when joining with saved_providers)
    display_name: str = ""
    specialty: str = ""
    # NPI fallback address (from saved_providers) for display when no override
    npi_address_1: str = ""
    npi_city: str = ""
    npi_state: str = ""
    npi_zip: str = ""
    npi_phone: str = ""

    @property
    def effective_address_1(self) -> str:
        return self.override_address_1 or self.npi_address_1

    @property
    def effective_city(self) -> str:
        return self.override_city or self.npi_city

    @property
    def effective_state(self) -> str:
        return self.override_state or self.npi_state

    @property
    def effective_zip(self) -> str:
        return self.override_zip or self.npi_zip

    @property
    def effective_phone(self) -> str:
        return self.override_phone or self.npi_phone

    @property
    def address_overridden(self) -> bool:
        return bool(self.override_address_1 or self.override_city)
```

---

## Storage Layer (`storage.py`)

New methods on the `Storage` class:

```python
def create_referral_list(self, name: str) -> ReferralList
def list_referral_lists(self) -> list[ReferralList]          # ordered by created_at DESC
def get_referral_list(self, list_id: int) -> ReferralList | None
def delete_referral_list(self, list_id: int) -> bool
def get_referral_list_entries(self, list_id: int) -> list[ReferralListEntry]  # joins saved_providers for display fields
def add_to_referral_list(self, list_id: int, npi: str, **overrides) -> ReferralListEntry
def update_referral_entry(self, list_id: int, npi: str, **fields) -> ReferralListEntry | None
def remove_from_referral_list(self, list_id: int, npi: str) -> bool
def get_referral_lists_for_npi(self, npi: str) -> list[ReferralList]  # which lists contain this provider
```

`get_referral_list_entries` JOINs `referral_list_entries` with `saved_providers` to populate display fields (display_name, specialty, npi_address_*, npi_phone) so templates receive fully-resolved objects.

---

## Web Layer (`web.py`)

### New routes

| Method | Route | Purpose | Response |
|--------|-------|---------|----------|
| GET | `/referral-lists` | All lists index | `referral_lists.html` |
| POST | `/referral-lists` | Create new list | redirect to `/referral-lists/{id}` |
| GET | `/referral-lists/{id}` | List detail with entries | `referral_list_detail.html` |
| DELETE | `/referral-lists/{id}` | Delete list | htmx row removal or redirect |
| GET | `/referral-lists/{id}/export` | Export page (both tabs) | `referral_list_export.html` |
| GET | `/referral-lists/{id}/export/text` | Download plain text file | `text/plain` attachment |
| POST | `/saved/{npi}/referral` | Add to list (inline form submit) | `_list_badge.html` partial |
| GET | `/saved/{npi}/referral/form` | Render inline add/edit form | `_add_to_list_form.html` partial |
| PATCH | `/saved/{npi}/referral/{list_id}` | Update entry overrides | `_referral_list_entry.html` partial |
| DELETE | `/saved/{npi}/referral/{list_id}` | Remove from list | `_list_badge_empty.html` or empty |

The `?view=print` query param on `/referral-lists/{id}/export` switches the active tab in the template (server-rendered, no JS tab toggle needed beyond a CSS class swap).

### Quick-add behavior
`POST /saved/{npi}/referral` accepts a `list_id` field. If `list_id` is `"new"`, a `list_name` field is required and a new list is created before adding the entry. If no lists exist when the form is rendered, the list selector shows only "+ Create new list…" as the option.

---

## Templates

### New templates

**`referral_lists.html`** — extends `base.html`
- Table: Name (link), Providers count, Created date, Export button, Delete button (hx-delete)
- "+ New List" button at top right → inline form (hx-post to `/referral-lists`)

**`referral_list_detail.html`** — extends `base.html`
- Header: list name, provider count, "Export Referral Document" button
- Per-entry expanded card: display_name, specialty badge, effective address + phone, override indicator, reason, notes, Edit + Remove buttons
- Edit triggers htmx GET to `/saved/{npi}/referral/form?list_id={id}&edit=1` → swaps in `_add_to_list_form.html`

**`referral_list_export.html`** — extends `base.html`
- Plain text tab: monospace `<pre>` block, Copy All button, Download .txt button
- Printable tab: styled provider cards with `@media print` CSS hiding nav/buttons; Print button calls `window.print()`
- Active tab driven by `?view=print` query param

### New partials

**`_add_to_list_form.html`** — inline add/edit form
- List selector (existing lists + "Create new list…" option)
- Reason field, override address fields (address line 1, city, state, zip — separate inputs matching the data model), override phone, notes
- Cancel + Submit buttons
- Used for both add (from saved page) and edit (from list detail page)

**`_list_badge.html`** — replaces "+ Add to List" button after adding
- Green "✓ [list name]" badge; clicking triggers htmx GET for the edit form

**`_referral_list_entry.html`** — single entry card for htmx swap on edit/remove

### Modified templates

**`saved.html`**
- New column (or action button): "Add to List" button OR green badge per row
- Button triggers htmx GET `/saved/{npi}/referral/form` → inserts `_add_to_list_form.html` below the row

**`base.html`**
- Add "Referral Lists" nav item between Saved and History

---

## Formatting (`formatting.py`)

New function:

```python
def referral_list_export_text(
    referral_list: ReferralList,
    entries: list[ReferralListEntry],
) -> str
```

Generates the plain text export. Per-entry block:
```
PROVIDER N OF M
-----------------------------------------------------
Name:       {display_name}
NPI:        {npi}
Specialty:  {specialty}

Appointment Location:
  {effective_address_1}
  {effective_city}, {effective_state} {effective_zip}
  Phone: {effective_phone}
  [Note: appointment address differs from NPI listing]  ← only if address_overridden

Reason for Referral:
  {reason}                                              ← omitted if blank

Notes:
  {notes}                                               ← omitted if blank
```

The printable view is rendered entirely in the Jinja2 template — no Python formatting function needed.

---

## Navigation & UX Details

- **Quick-add**: If no referral lists exist yet when the user clicks "+ Add to List", the list selector shows only "+ Create new list…". Selecting it reveals an inline name input. On submit, the list is created and the provider is added in one request.
- **Multi-list**: A provider can appear on multiple lists. The saved-providers row shows the first list name badge; if on multiple lists, shows "✓ N lists" (no dropdown — click opens the edit form for the first list).
- **Address override UX**: The add/edit form pre-fills override fields with the NPI-sourced address and phone from `saved_providers`. User edits only what differs. If the user clears the override fields, the entry falls back to NPI data.
- **`.gitignore`**: Add `.superpowers/` to `.gitignore`.

---

## Verification

1. `pip install -e ".[web]"` — ensure clean install
2. `docstats web` — start server
3. Search for a provider → save them
4. Go to Saved → click "+ Add to List" → verify inline form appears with pre-filled address
5. Create a new list via the form (no existing lists) → verify list is created and badge appears
6. Add a second provider to the same list → verify list detail shows both entries
7. Edit an entry's override address → verify list detail reflects the change with "✎ overridden" indicator
8. Go to Referral Lists index → verify list appears with correct provider count
9. Click Export → verify plain text tab shows both providers with correct data; overridden addresses flagged
10. Switch to Printable tab → verify card layout; click Print button → browser print dialog opens
11. Click Download .txt → verify file downloads with correct content
12. Remove a provider from the list → verify it disappears from list detail; saved provider still exists
13. Delete the referral list → verify it disappears from index; saved providers unaffected
14. `pytest` — all existing tests pass
15. `ruff check .` — no lint errors
