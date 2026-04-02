# Referral Request Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-provider referral request builder on top of the existing saved providers feature, letting users add doctors to named referral lists with appointment location/phone overrides, then export a combined referral document.

**Architecture:** Two new SQLite tables (`referral_lists`, `referral_list_entries`) with FK cascade. All storage follows the existing `self._conn.execute()` + `self._conn.commit()` pattern. Web routes follow the existing FastAPI + htmx + `_render()` pattern. The saved page gains an inline add-to-list form; a new Referral Lists section handles list management and export.

**Tech Stack:** FastAPI, htmx 2.0.4, Pico CSS, SQLite (WAL), Pydantic v2, Jinja2

---

## File Map

**Create:**
- `src/docstats/templates/referral_lists.html` — Referral Lists index page
- `src/docstats/templates/referral_list_detail.html` — List detail + entry cards
- `src/docstats/templates/referral_list_export.html` — Export page (plain text + printable tabs)
- `src/docstats/templates/_add_to_list_form.html` — Inline add/edit form partial
- `src/docstats/templates/_list_badge.html` — Badge/button state partial (returned by routes after add/cancel)
- `src/docstats/templates/_referral_list_entry.html` — Entry card partial (returned by routes after edit/remove)
- `tests/test_formatting.py` — Tests for referral_list_export_text

**Modify:**
- `.gitignore` — add `.superpowers/`
- `src/docstats/models.py` — add `ReferralList`, `ReferralListEntry`
- `src/docstats/storage.py` — add FK pragma, two new tables, 9 new methods, 2 static helpers
- `src/docstats/formatting.py` — add `referral_list_export_text()`
- `src/docstats/web.py` — add 10 new routes, update imports
- `src/docstats/templates/base.html` — add Referral Lists nav item
- `src/docstats/templates/saved.html` — add per-row referral action column
- `tests/test_storage.py` — new tests for all referral list storage methods

---

### Task 1: .gitignore + Pydantic models

**Files:**
- Modify: `.gitignore`
- Modify: `src/docstats/models.py`

- [ ] **Step 1: Add `.superpowers/` to `.gitignore`**

Append to `.gitignore`:
```
.superpowers/
```

- [ ] **Step 2: Add models to `src/docstats/models.py`**

Append after the `SearchHistoryEntry` class (around line 309, before end of file):

```python
class ReferralList(BaseModel):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    provider_count: int = 0  # populated from COUNT JOIN; not stored in DB


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

    # Populated via JOIN with saved_providers — not stored in referral_list_entries
    display_name: str = ""
    specialty: str = ""
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

- [ ] **Step 3: Verify models import cleanly**

```bash
cd /Users/ryanrobson/git/docstats
python -c "from docstats.models import ReferralList, ReferralListEntry; print('OK')"
```

Expected output: `OK`

- [ ] **Step 4: Commit**

```bash
git add .gitignore src/docstats/models.py
git commit -m "feat: add ReferralList and ReferralListEntry pydantic models"
```

---

### Task 2: Storage schema

**Files:**
- Modify: `src/docstats/storage.py`
- Modify: `tests/test_storage.py`

The storage uses a single persistent connection (`self._conn`). SQLite does not enforce foreign keys by default — we must add `PRAGMA foreign_keys = ON` per connection. The new tables go into `_init_tables()`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_storage.py`:

```python
def test_referral_list_tables_exist(storage: Storage):
    cursor = storage._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('referral_lists', 'referral_list_entries')"
    )
    tables = {row["name"] for row in cursor.fetchall()}
    assert "referral_lists" in tables
    assert "referral_list_entries" in tables


def test_foreign_keys_enabled(storage: Storage):
    row = storage._conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ryanrobson/git/docstats
pytest tests/test_storage.py::test_referral_list_tables_exist tests/test_storage.py::test_foreign_keys_enabled -v
```

Expected: FAILED (tables don't exist yet, FK pragma not set)

- [ ] **Step 3: Add `PRAGMA foreign_keys = ON` to `Storage.__init__`**

In `src/docstats/storage.py`, `Storage.__init__`, add this line immediately after the WAL pragma (line 32):

```python
self._conn.execute("PRAGMA foreign_keys = ON")
```

The block should now read:
```python
self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA foreign_keys = ON")
self._conn.row_factory = sqlite3.Row
self._init_tables()
```

- [ ] **Step 4: Add new tables to `_init_tables()`**

In `src/docstats/storage.py`, extend the `executescript` in `_init_tables()`. Add the two new table definitions after the `search_history` table and its index (before the closing `"""`):

```python
            CREATE TABLE IF NOT EXISTS referral_lists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS referral_list_entries (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id            INTEGER NOT NULL REFERENCES referral_lists(id) ON DELETE CASCADE,
                npi                TEXT NOT NULL REFERENCES saved_providers(npi) ON DELETE CASCADE,
                override_address_1 TEXT,
                override_city      TEXT,
                override_state     TEXT,
                override_zip       TEXT,
                override_phone     TEXT,
                notes              TEXT,
                reason             TEXT,
                added_at           TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(list_id, npi)
            );
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_storage.py::test_referral_list_tables_exist tests/test_storage.py::test_foreign_keys_enabled -v
```

Expected: PASSED

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
pytest -v
```

Expected: all existing tests still pass

- [ ] **Step 7: Commit**

```bash
git add src/docstats/storage.py tests/test_storage.py
git commit -m "feat: add referral_lists and referral_list_entries tables to storage"
```

---

### Task 3: Storage — referral list CRUD methods

**Files:**
- Modify: `src/docstats/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_storage.py`:

```python
def test_create_and_get_referral_list(storage: Storage):
    rl = storage.create_referral_list("My Referrals")
    assert rl.id is not None
    assert rl.name == "My Referrals"
    assert rl.provider_count == 0

    retrieved = storage.get_referral_list(rl.id)
    assert retrieved is not None
    assert retrieved.id == rl.id
    assert retrieved.name == "My Referrals"


def test_list_referral_lists(storage: Storage):
    storage.create_referral_list("List A")
    storage.create_referral_list("List B")
    lists = storage.list_referral_lists()
    assert len(lists) == 2
    names = {rl.name for rl in lists}
    assert "List A" in names
    assert "List B" in names


def test_delete_referral_list(storage: Storage):
    rl = storage.create_referral_list("To Delete")
    assert storage.delete_referral_list(rl.id) is True
    assert storage.get_referral_list(rl.id) is None
    assert storage.delete_referral_list(rl.id) is False


def test_get_referral_list_not_found(storage: Storage):
    assert storage.get_referral_list(9999) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_storage.py::test_create_and_get_referral_list tests/test_storage.py::test_list_referral_lists tests/test_storage.py::test_delete_referral_list tests/test_storage.py::test_get_referral_list_not_found -v
```

Expected: FAILED (methods don't exist yet)

- [ ] **Step 3: Add import + 5 new methods + `_row_to_referral_list` to `storage.py`**

Add `ReferralList, ReferralListEntry` to the import at the top of `storage.py`:

```python
from docstats.models import NPIResult, ReferralList, ReferralListEntry, SavedProvider, SearchHistoryEntry
```

Then add these methods to the `Storage` class (before the `close` method):

```python
    # --- Referral lists ---

    def create_referral_list(self, name: str) -> ReferralList:
        """Create a new named referral list."""
        cursor = self._conn.execute(
            "INSERT INTO referral_lists (name) VALUES (?)", (name,)
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id, name, created_at, updated_at, 0 AS provider_count "
            "FROM referral_lists WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return self._row_to_referral_list(row)

    def list_referral_lists(self) -> list[ReferralList]:
        """List all referral lists with provider counts, newest first."""
        rows = self._conn.execute("""
            SELECT rl.id, rl.name, rl.created_at, rl.updated_at,
                   COUNT(e.npi) AS provider_count
            FROM referral_lists rl
            LEFT JOIN referral_list_entries e ON rl.id = e.list_id
            GROUP BY rl.id
            ORDER BY rl.created_at DESC
        """).fetchall()
        return [self._row_to_referral_list(r) for r in rows]

    def get_referral_list(self, list_id: int) -> ReferralList | None:
        """Get a single referral list by id."""
        row = self._conn.execute("""
            SELECT rl.id, rl.name, rl.created_at, rl.updated_at,
                   COUNT(e.npi) AS provider_count
            FROM referral_lists rl
            LEFT JOIN referral_list_entries e ON rl.id = e.list_id
            WHERE rl.id = ?
            GROUP BY rl.id
        """, (list_id,)).fetchone()
        return self._row_to_referral_list(row) if row else None

    def delete_referral_list(self, list_id: int) -> bool:
        """Delete a referral list and cascade-delete its entries. Returns True if it existed."""
        cursor = self._conn.execute(
            "DELETE FROM referral_lists WHERE id = ?", (list_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_npi_to_referral_lists(self) -> dict[str, list[ReferralList]]:
        """Return {npi: [ReferralList, ...]} for all providers on any list."""
        rows = self._conn.execute("""
            SELECT e.npi, rl.id, rl.name, rl.created_at, rl.updated_at,
                   0 AS provider_count
            FROM referral_list_entries e
            JOIN referral_lists rl ON e.list_id = rl.id
            ORDER BY rl.created_at DESC
        """).fetchall()
        result: dict[str, list[ReferralList]] = {}
        for row in rows:
            result.setdefault(row["npi"], []).append(self._row_to_referral_list(row))
        return result

    @staticmethod
    def _row_to_referral_list(row: sqlite3.Row) -> ReferralList:
        return ReferralList(
            id=row["id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            provider_count=row["provider_count"],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_storage.py::test_create_and_get_referral_list tests/test_storage.py::test_list_referral_lists tests/test_storage.py::test_delete_referral_list tests/test_storage.py::test_get_referral_list_not_found -v
```

Expected: PASSED

- [ ] **Step 5: Run full test suite**

```bash
pytest -v
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add src/docstats/storage.py tests/test_storage.py
git commit -m "feat: add referral list CRUD storage methods"
```

---

### Task 4: Storage — referral list entry CRUD methods

**Files:**
- Modify: `src/docstats/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_storage.py`:

```python
def test_add_and_get_referral_list_entries(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")

    entry = storage.add_to_referral_list(
        list_id=rl.id,
        npi="1234567890",
        reason="Cardiology consult",
        override_phone="(415) 555-9999",
    )
    assert entry.npi == "1234567890"
    assert entry.reason == "Cardiology consult"
    assert entry.override_phone == "(415) 555-9999"
    assert entry.effective_phone == "(415) 555-9999"  # override wins
    assert "John" in entry.display_name  # populated via JOIN


def test_entry_falls_back_to_npi_phone(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")

    entry = storage.add_to_referral_list(list_id=rl.id, npi="1234567890")
    assert entry.effective_phone == entry.npi_phone


def test_update_referral_entry(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")
    storage.add_to_referral_list(list_id=rl.id, npi="1234567890", reason="original")

    updated = storage.update_referral_entry(
        list_id=rl.id, npi="1234567890", reason="updated reason"
    )
    assert updated is not None
    assert updated.reason == "updated reason"


def test_remove_from_referral_list(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")
    storage.add_to_referral_list(list_id=rl.id, npi="1234567890")

    assert storage.remove_from_referral_list(rl.id, "1234567890") is True
    assert storage.get_referral_list_entries(rl.id) == []
    assert storage.remove_from_referral_list(rl.id, "1234567890") is False


def test_provider_count_increments(storage: Storage):
    r1 = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    r2 = NPIResult.model_validate(SAMPLE_NPI2_RESULT)
    storage.save_provider(r1)
    storage.save_provider(r2)
    rl = storage.create_referral_list("My Referrals")
    storage.add_to_referral_list(list_id=rl.id, npi="1234567890")
    storage.add_to_referral_list(list_id=rl.id, npi="9876543210")

    lists = storage.list_referral_lists()
    assert lists[0].provider_count == 2


def test_delete_provider_cascades_to_entries(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")
    storage.add_to_referral_list(list_id=rl.id, npi="1234567890")

    storage.delete_provider("1234567890")
    assert storage.get_referral_list_entries(rl.id) == []


def test_delete_list_cascades_to_entries(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")
    storage.add_to_referral_list(list_id=rl.id, npi="1234567890")

    storage.delete_referral_list(rl.id)
    assert storage.get_referral_list(rl.id) is None


def test_get_referral_lists_for_npi(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")
    storage.add_to_referral_list(list_id=rl.id, npi="1234567890")

    lists = storage.get_referral_lists_for_npi("1234567890")
    assert len(lists) == 1
    assert lists[0].name == "My Referrals"


def test_get_npi_to_referral_lists(storage: Storage):
    result = NPIResult.model_validate(SAMPLE_NPI1_RESULT)
    storage.save_provider(result)
    rl = storage.create_referral_list("My Referrals")
    storage.add_to_referral_list(list_id=rl.id, npi="1234567890")

    membership = storage.get_npi_to_referral_lists()
    assert "1234567890" in membership
    assert membership["1234567890"][0].name == "My Referrals"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_storage.py -k "referral_list_entries or add_to_referral or update_referral or remove_from or cascades or provider_count or referral_lists_for_npi or npi_to_referral" -v
```

Expected: FAILED (methods don't exist yet)

- [ ] **Step 3: Add 5 entry methods + `_row_to_referral_entry` to `storage.py`**

Add these methods to the `Storage` class, after `get_npi_to_referral_lists`:

```python
    def get_referral_list_entries(self, list_id: int) -> list[ReferralListEntry]:
        """Get all entries for a list, joined with saved_providers for display fields."""
        rows = self._conn.execute("""
            SELECT
                e.id, e.list_id, e.npi, e.added_at,
                e.override_address_1, e.override_city, e.override_state,
                e.override_zip, e.override_phone, e.notes, e.reason,
                p.display_name, p.specialty,
                p.address_line1  AS npi_address_1,
                p.address_city   AS npi_city,
                p.address_state  AS npi_state,
                p.address_zip    AS npi_zip,
                p.phone          AS npi_phone
            FROM referral_list_entries e
            JOIN saved_providers p ON e.npi = p.npi
            WHERE e.list_id = ?
            ORDER BY e.added_at ASC
        """, (list_id,)).fetchall()
        return [self._row_to_referral_entry(r) for r in rows]

    def add_to_referral_list(
        self,
        list_id: int,
        npi: str,
        override_address_1: str | None = None,
        override_city: str | None = None,
        override_state: str | None = None,
        override_zip: str | None = None,
        override_phone: str | None = None,
        notes: str | None = None,
        reason: str | None = None,
    ) -> ReferralListEntry:
        """Add a saved provider to a referral list with optional override fields."""
        cursor = self._conn.execute("""
            INSERT INTO referral_list_entries
                (list_id, npi, override_address_1, override_city, override_state,
                 override_zip, override_phone, notes, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (list_id, npi, override_address_1, override_city, override_state,
               override_zip, override_phone, notes, reason))
        self._conn.commit()
        entry_id = cursor.lastrowid
        entries = self.get_referral_list_entries(list_id)
        return next(e for e in entries if e.id == entry_id)

    def update_referral_entry(
        self,
        list_id: int,
        npi: str,
        override_address_1: str | None = None,
        override_city: str | None = None,
        override_state: str | None = None,
        override_zip: str | None = None,
        override_phone: str | None = None,
        notes: str | None = None,
        reason: str | None = None,
    ) -> ReferralListEntry | None:
        """Update override fields for an existing entry. Pass None to clear a field."""
        self._conn.execute("""
            UPDATE referral_list_entries
            SET override_address_1 = ?, override_city = ?, override_state = ?,
                override_zip = ?, override_phone = ?, notes = ?, reason = ?
            WHERE list_id = ? AND npi = ?
        """, (override_address_1, override_city, override_state,
               override_zip, override_phone, notes, reason, list_id, npi))
        self._conn.commit()
        entries = self.get_referral_list_entries(list_id)
        return next((e for e in entries if e.npi == npi), None)

    def remove_from_referral_list(self, list_id: int, npi: str) -> bool:
        """Remove a provider from a referral list. Returns True if entry existed."""
        cursor = self._conn.execute(
            "DELETE FROM referral_list_entries WHERE list_id = ? AND npi = ?",
            (list_id, npi),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_referral_lists_for_npi(self, npi: str) -> list[ReferralList]:
        """Get all referral lists that contain this provider."""
        rows = self._conn.execute("""
            SELECT rl.id, rl.name, rl.created_at, rl.updated_at, 0 AS provider_count
            FROM referral_lists rl
            JOIN referral_list_entries e ON rl.id = e.list_id
            WHERE e.npi = ?
            ORDER BY rl.created_at DESC
        """, (npi,)).fetchall()
        return [self._row_to_referral_list(r) for r in rows]

    @staticmethod
    def _row_to_referral_entry(row: sqlite3.Row) -> ReferralListEntry:
        return ReferralListEntry(
            id=row["id"],
            list_id=row["list_id"],
            npi=row["npi"],
            added_at=datetime.fromisoformat(row["added_at"]),
            override_address_1=row["override_address_1"],
            override_city=row["override_city"],
            override_state=row["override_state"],
            override_zip=row["override_zip"],
            override_phone=row["override_phone"],
            notes=row["notes"],
            reason=row["reason"],
            display_name=row["display_name"] or "",
            specialty=row["specialty"] or "",
            npi_address_1=row["npi_address_1"] or "",
            npi_city=row["npi_city"] or "",
            npi_state=row["npi_state"] or "",
            npi_zip=row["npi_zip"] or "",
            npi_phone=row["npi_phone"] or "",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_storage.py -v
```

Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/docstats/storage.py tests/test_storage.py
git commit -m "feat: add referral list entry CRUD storage methods"
```

---

### Task 5: Formatting — `referral_list_export_text`

**Files:**
- Modify: `src/docstats/formatting.py`
- Create: `tests/test_formatting.py`

- [ ] **Step 1: Create `tests/test_formatting.py` with failing tests**

```python
"""Tests for formatting functions."""

from __future__ import annotations

from datetime import datetime

import pytest

from docstats.formatting import referral_list_export_text
from docstats.models import ReferralList, ReferralListEntry


def _list(name: str = "My Referrals") -> ReferralList:
    return ReferralList(
        id=1,
        name=name,
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
        provider_count=0,
    )


def _entry(
    npi: str = "1234567890",
    display_name: str = "Smith, Jane MD",
    specialty: str = "Cardiology",
    reason: str | None = "Chest pain consult",
    notes: str | None = None,
    override_phone: str | None = None,
    npi_phone: str = "(415) 555-1234",
    override_address_1: str | None = None,
    npi_address_1: str = "123 Main St",
    npi_city: str = "San Francisco",
    npi_state: str = "CA",
    npi_zip: str = "94110",
) -> ReferralListEntry:
    return ReferralListEntry(
        id=1,
        list_id=1,
        npi=npi,
        added_at=datetime(2025, 1, 1),
        display_name=display_name,
        specialty=specialty,
        reason=reason,
        notes=notes,
        override_phone=override_phone,
        npi_phone=npi_phone,
        override_address_1=override_address_1,
        npi_address_1=npi_address_1,
        npi_city=npi_city,
        npi_state=npi_state,
        npi_zip=npi_zip,
    )


def test_export_contains_list_name():
    result = referral_list_export_text(_list("Test List"), [_entry()])
    assert "Test List" in result


def test_export_contains_provider_info():
    result = referral_list_export_text(_list(), [_entry()])
    assert "Smith, Jane MD" in result
    assert "1234567890" in result
    assert "Cardiology" in result


def test_export_uses_override_phone():
    entry = _entry(override_phone="(415) 999-9999")
    result = referral_list_export_text(_list(), [entry])
    assert "(415) 999-9999" in result
    assert "(415) 555-1234" not in result


def test_export_falls_back_to_npi_phone():
    entry = _entry(override_phone=None, npi_phone="(415) 555-1234")
    result = referral_list_export_text(_list(), [entry])
    assert "(415) 555-1234" in result


def test_export_flags_overridden_address():
    entry = _entry(override_address_1="999 Override St")
    result = referral_list_export_text(_list(), [entry])
    assert "differs from NPI listing" in result
    assert "999 Override St" in result


def test_export_no_override_flag_when_no_override():
    entry = _entry()
    result = referral_list_export_text(_list(), [entry])
    assert "differs from NPI listing" not in result


def test_export_omits_blank_reason():
    entry = _entry(reason=None)
    result = referral_list_export_text(_list(), [entry])
    assert "Reason for Referral" not in result


def test_export_includes_reason_when_set():
    entry = _entry(reason="Cardiology consult")
    result = referral_list_export_text(_list(), [entry])
    assert "Reason for Referral" in result
    assert "Cardiology consult" in result


def test_export_omits_blank_notes():
    entry = _entry(notes=None)
    result = referral_list_export_text(_list(), [entry])
    assert "Notes:" not in result


def test_export_multiple_providers():
    entries = [
        _entry("1111111111", "Smith, Jane MD"),
        _entry("2222222222", "Patel, Raj MD"),
    ]
    result = referral_list_export_text(_list(), entries)
    assert "PROVIDER 1 OF 2" in result
    assert "PROVIDER 2 OF 2" in result
    assert "Smith, Jane MD" in result
    assert "Patel, Raj MD" in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_formatting.py -v
```

Expected: FAILED (function doesn't exist yet)

- [ ] **Step 3: Add `referral_list_export_text` to `src/docstats/formatting.py`**

Add `from datetime import datetime` to the imports in `formatting.py` (it's not there yet).

Add `ReferralList, ReferralListEntry` to the models import:

```python
from docstats.models import NPIResult, NPIResponse, ReferralList, ReferralListEntry, SavedProvider, SearchHistoryEntry
```

Append the new function after `referral_export`:

```python
def referral_list_export_text(
    referral_list: ReferralList,
    entries: list[ReferralListEntry],
) -> str:
    """Generate a plain-text referral request document for a full referral list."""
    now = datetime.now()
    today = f"{now.strftime('%B')} {now.day}, {now.year}"
    total = len(entries)

    lines: list[str] = []
    lines.append(f"REFERRAL REQUEST \u2014 {referral_list.name}")
    lines.append(f"Generated: {today}")
    lines.append("=" * 53)
    lines.append("")

    for i, entry in enumerate(entries, 1):
        lines.append(f"PROVIDER {i} OF {total}")
        lines.append("-" * 53)
        lines.append(f"Name:       {entry.display_name}")
        lines.append(f"NPI:        {entry.npi}")
        lines.append(f"Specialty:  {entry.specialty}")
        lines.append("")
        lines.append("Appointment Location:")
        if entry.effective_address_1:
            lines.append(f"  {entry.effective_address_1}")
        parts = [p for p in [entry.effective_city, entry.effective_state] if p]
        city_state = ", ".join(parts)
        zip_part = entry.effective_zip
        if city_state:
            lines.append(f"  {city_state} {zip_part}".rstrip())
        if entry.effective_phone:
            lines.append(f"  Phone: {entry.effective_phone}")
        if entry.address_overridden:
            lines.append("  [Note: appointment address differs from NPI listing]")
        lines.append("")
        if entry.reason:
            lines.append("Reason for Referral:")
            lines.append(f"  {entry.reason}")
            lines.append("")
        if entry.notes:
            lines.append("Notes:")
            lines.append(f"  {entry.notes}")
            lines.append("")

    lines.append("=" * 53)
    lines.append("NPI data sourced from CMS NPPES Registry.")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_formatting.py -v
```

Expected: all passing

- [ ] **Step 5: Run full test suite**

```bash
pytest -v
```

Expected: all passing

- [ ] **Step 6: Run lint**

```bash
ruff check .
```

Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/docstats/formatting.py tests/test_formatting.py
git commit -m "feat: add referral_list_export_text formatting function"
```

---

### Task 6: Web — nav + Referral Lists index

**Files:**
- Modify: `src/docstats/templates/base.html`
- Modify: `src/docstats/web.py`
- Create: `src/docstats/templates/referral_lists.html`

- [ ] **Step 1: Add "Referral Lists" nav item to `base.html`**

In `src/docstats/templates/base.html`, find the nav `<ul>` containing the nav links (lines 69–72). Insert the new item between Saved and History:

```html
            <ul>
                <li><a href="/" {% if active_page == "search" %}class="active"{% endif %}>Search</a></li>
                <li><a href="/saved" {% if active_page == "saved" %}class="active"{% endif %}>Saved</a></li>
                <li><a href="/referral-lists" {% if active_page == "referral-lists" %}class="active"{% endif %}>Referral Lists</a></li>
                <li><a href="/history" {% if active_page == "history" %}class="active"{% endif %}>History</a></li>
            </ul>
```

- [ ] **Step 2: Add 3 routes to `src/docstats/web.py`**

Add `referral_list_export_text` to the formatting import:

```python
from docstats.formatting import referral_export, referral_list_export_text
```

Add `ReferralList, ReferralListEntry` to the models import if models is imported (it isn't currently — the models are imported indirectly). No change needed here; the storage layer handles model construction.

Add these routes after the existing `/saved` route block (around line 400):

```python
@app.get("/referral-lists", response_class=HTMLResponse)
async def referral_lists_index(
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Referral lists index."""
    lists = storage.list_referral_lists()
    return _render("referral_lists.html", {
        "request": request,
        "active_page": "referral-lists",
        "lists": lists,
    })


@app.post("/referral-lists", response_class=HTMLResponse)
async def create_referral_list(
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Create a new referral list and redirect to its detail page."""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    name = str(form.get("name", "")).strip() or "My Referral Request"
    rl = storage.create_referral_list(name)
    return RedirectResponse(url=f"/referral-lists/{rl.id}", status_code=303)


@app.delete("/referral-lists/{list_id}", response_class=HTMLResponse)
async def delete_referral_list(
    list_id: int,
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Delete a referral list (htmx row removal)."""
    storage.delete_referral_list(list_id)
    return HTMLResponse("")
```

- [ ] **Step 3: Create `src/docstats/templates/referral_lists.html`**

```html
{% extends "base.html" %}
{% block title %}Referral Lists - docstats{% endblock %}
{% block content %}
<div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 1rem;">
    <h2>Referral Lists</h2>
    <form method="post" action="/referral-lists" style="display: flex; gap: 0.5rem; align-items: center;">
        <input type="text" name="name" placeholder="New list name…"
               style="padding: 0.35rem 0.6rem; font-size: 0.9rem; width: 200px;" required>
        <button type="submit" style="padding: 0.35rem 0.75rem; font-size: 0.9rem;">+ New List</button>
    </form>
</div>

{% if not lists %}
<p><em>No referral lists yet. Create one above or add providers from the
    <a href="/saved">Saved</a> page.</em></p>
{% else %}
<div style="overflow-x: auto;">
<table class="results-table striped">
    <thead>
        <tr>
            <th>Name</th>
            <th>Providers</th>
            <th>Created</th>
            <th></th>
        </tr>
    </thead>
    <tbody>
        {% for rl in lists %}
        <tr id="list-row-{{ rl.id }}">
            <td><a href="/referral-lists/{{ rl.id }}"><strong>{{ rl.name }}</strong></a></td>
            <td>{{ rl.provider_count }}</td>
            <td>{{ rl.created_at.strftime("%b %d, %Y") }}</td>
            <td>
                <div style="display: flex; gap: 0.25rem;">
                    <a href="/referral-lists/{{ rl.id }}/export" role="button" class="outline"
                       style="padding: 0.25rem 0.5rem; font-size: 0.8rem;">Export</a>
                    <button
                        hx-delete="/referral-lists/{{ rl.id }}"
                        hx-target="#list-row-{{ rl.id }}"
                        hx-swap="outerHTML"
                        class="outline secondary"
                        style="padding: 0.25rem 0.5rem; font-size: 0.8rem;"
                    >Delete</button>
                </div>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Verify manually**

```bash
docstats web
```

Open http://127.0.0.1:8000, confirm "Referral Lists" appears in nav. Click it — should show the index page with the new list form. Create a list named "Test" — should redirect to `/referral-lists/1` (404 for now, detail page not built yet). Go back to `/referral-lists` — "Test" should appear in the table with 0 providers. Click Delete — row should disappear.

- [ ] **Step 5: Commit**

```bash
git add src/docstats/templates/base.html src/docstats/templates/referral_lists.html src/docstats/web.py
git commit -m "feat: add referral lists index page and nav item"
```

---

### Task 7: Web — referral list detail page

**Files:**
- Modify: `src/docstats/web.py`
- Create: `src/docstats/templates/referral_list_detail.html`
- Create: `src/docstats/templates/_referral_list_entry.html`

The detail page renders all entries as expanded cards. Each card has Edit (opens inline form) and Remove buttons. The `_referral_list_entry.html` partial is the card itself — returned by PATCH after edit, so it must include its own wrapping `<div id="entry-card-{npi}">`.

- [ ] **Step 1: Add GET `/referral-lists/{list_id}` route to `web.py`**

```python
@app.get("/referral-lists/{list_id}", response_class=HTMLResponse)
async def referral_list_detail(
    list_id: int,
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Referral list detail page."""
    rl = storage.get_referral_list(list_id)
    if rl is None:
        return HTMLResponse("List not found", status_code=404)
    entries = storage.get_referral_list_entries(list_id)
    return _render("referral_list_detail.html", {
        "request": request,
        "active_page": "referral-lists",
        "referral_list": rl,
        "entries": entries,
    })
```

- [ ] **Step 2: Create `src/docstats/templates/_referral_list_entry.html`**

This partial renders one entry card. It includes its own outer `<div id="entry-card-{npi}">` wrapper so PATCH can swap it with `outerHTML`.

```html
<div id="entry-card-{{ entry.npi }}" style="border: 1px solid var(--pico-muted-border-color); border-radius: var(--pico-border-radius); padding: 1rem; margin-bottom: 0.75rem;">
    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem;">
        <div style="flex: 1;">
            <div style="margin-bottom: 0.35rem;">
                <strong>{{ entry.display_name }}</strong>
                {% if entry.specialty %}
                <span class="badge-ind" style="font-size: 0.78em; font-weight: 400; margin-left: 0.4rem;">{{ entry.specialty }}</span>
                {% endif %}
            </div>
            <div style="font-size: 0.85em; color: var(--pico-muted-color); margin-bottom: 0.25rem;">
                NPI {{ entry.npi }}
            </div>
            <div style="font-size: 0.85em; margin-bottom: 0.2rem;">
                <strong>Appointment:</strong>
                {% if entry.effective_address_1 %}
                {{ entry.effective_address_1 }},
                {% endif %}
                {% if entry.effective_city %}{{ entry.effective_city }},{% endif %}
                {{ entry.effective_state }} {{ entry.effective_zip }}
                {% if entry.effective_phone %}&nbsp;&middot; {{ entry.effective_phone }}{% endif %}
                {% if entry.address_overridden %}
                <span style="color: #2e7d32; font-size: 0.85em; margin-left: 0.3rem;" title="Appointment address differs from NPI listing">&#9998; overridden</span>
                {% endif %}
            </div>
            {% if entry.reason %}
            <div style="font-size: 0.85em; margin-bottom: 0.2rem;">
                <strong>Reason:</strong> {{ entry.reason }}
            </div>
            {% endif %}
            {% if entry.notes %}
            <div style="font-size: 0.85em; color: var(--pico-muted-color);">
                <strong>Notes:</strong> {{ entry.notes }}
            </div>
            {% endif %}
        </div>
        <div style="display: flex; gap: 0.25rem; flex-shrink: 0;">
            <button
                hx-get="/saved/{{ entry.npi }}/referral/form?list_id={{ entry.list_id }}&edit=1"
                hx-target="#entry-form-{{ entry.npi }}"
                hx-swap="innerHTML"
                class="outline"
                style="padding: 0.25rem 0.5rem; font-size: 0.8rem;"
            >Edit</button>
            <button
                hx-delete="/saved/{{ entry.npi }}/referral/{{ entry.list_id }}"
                hx-target="#entry-card-{{ entry.npi }}"
                hx-swap="outerHTML"
                class="outline secondary"
                style="padding: 0.25rem 0.5rem; font-size: 0.8rem;"
            >Remove</button>
        </div>
    </div>
    <div id="entry-form-{{ entry.npi }}"></div>
</div>
```

- [ ] **Step 3: Create `src/docstats/templates/referral_list_detail.html`**

```html
{% extends "base.html" %}
{% block title %}{{ referral_list.name }} - docstats{% endblock %}
{% block content %}
<div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 1rem;">
    <div>
        <a href="/referral-lists" style="font-size: 0.85em; color: var(--pico-muted-color);">&larr; All Lists</a>
        <h2 style="margin: 0.25rem 0 0;">{{ referral_list.name }}</h2>
        <p style="font-size: 0.85em; color: var(--pico-muted-color); margin: 0;">
            {{ referral_list.provider_count }} provider{% if referral_list.provider_count != 1 %}s{% endif %}
        </p>
    </div>
    {% if entries %}
    <a href="/referral-lists/{{ referral_list.id }}/export" role="button"
       style="padding: 0.4rem 0.9rem; font-size: 0.9rem;">Export Referral Document</a>
    {% endif %}
</div>

{% if not entries %}
<p><em>No providers on this list yet. Go to <a href="/saved">Saved</a> and add providers using
    the "+ Add to List" button.</em></p>
{% else %}
{% for entry in entries %}
    {% include "_referral_list_entry.html" %}
{% endfor %}
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Verify manually**

```bash
docstats web
```

1. Save a provider via the search page.
2. Create a referral list at `/referral-lists`.
3. Navigate to `/referral-lists/1` — should show the empty-state message.
4. The Export button should only appear when there are entries.

- [ ] **Step 5: Commit**

```bash
git add src/docstats/web.py src/docstats/templates/referral_list_detail.html src/docstats/templates/_referral_list_entry.html
git commit -m "feat: add referral list detail page and entry card partial"
```

---

### Task 8: Web — add/edit/remove entry flow

**Files:**
- Modify: `src/docstats/web.py`
- Modify: `src/docstats/templates/saved.html`
- Create: `src/docstats/templates/_add_to_list_form.html`
- Create: `src/docstats/templates/_list_badge.html`

This task wires up the full inline add/edit flow. The saved page gains a per-row referral action cell. Routes handle the form, add, edit, and remove.

- [ ] **Step 1: Add 5 routes to `web.py`**

```python
@app.get("/saved/{npi}/referral/button", response_class=HTMLResponse)
async def referral_button(
    npi: str,
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Return the current badge/button state for a provider's referral action cell."""
    lists = storage.get_referral_lists_for_npi(npi)
    return _render("_list_badge.html", {"request": request, "npi": npi, "lists": lists})


@app.get("/saved/{npi}/referral/form", response_class=HTMLResponse)
async def referral_form(
    npi: str,
    request: Request,
    list_id: int | None = Query(None),
    edit: bool = Query(False),
    storage: Storage = Depends(get_storage),
):
    """Render the inline add/edit form for a provider's referral entry."""
    provider = storage.get_provider(npi)
    if provider is None:
        return HTMLResponse("Provider not found", status_code=404)
    lists = storage.list_referral_lists()
    entry = None
    if edit and list_id is not None:
        entries = storage.get_referral_list_entries(list_id)
        entry = next((e for e in entries if e.npi == npi), None)
    return _render("_add_to_list_form.html", {
        "request": request,
        "npi": npi,
        "provider": provider,
        "lists": lists,
        "list_id": list_id,
        "edit": edit,
        "entry": entry,
    })


@app.post("/saved/{npi}/referral", response_class=HTMLResponse)
async def add_to_referral_list(
    npi: str,
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Add a provider to a referral list (htmx inline form submit)."""
    form = await request.form()
    list_id_raw = str(form.get("list_id", "")).strip()
    if list_id_raw == "new":
        list_name = str(form.get("list_name", "")).strip() or "My Referral Request"
        rl = storage.create_referral_list(list_name)
        list_id = rl.id
    else:
        list_id = int(list_id_raw)

    def _field(key: str) -> str | None:
        val = str(form.get(key, "")).strip()
        return val or None

    storage.add_to_referral_list(
        list_id=list_id,
        npi=npi,
        override_address_1=_field("override_address_1"),
        override_city=_field("override_city"),
        override_state=_field("override_state"),
        override_zip=_field("override_zip"),
        override_phone=_field("override_phone"),
        notes=_field("notes"),
        reason=_field("reason"),
    )
    lists = storage.get_referral_lists_for_npi(npi)
    return _render("_list_badge.html", {"request": request, "npi": npi, "lists": lists})


@app.patch("/saved/{npi}/referral/{list_id}", response_class=HTMLResponse)
async def update_referral_entry(
    npi: str,
    list_id: int,
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Update a referral entry's override fields (htmx PATCH from edit form)."""
    form = await request.form()

    def _field(key: str) -> str | None:
        val = str(form.get(key, "")).strip()
        return val or None

    entry = storage.update_referral_entry(
        list_id=list_id,
        npi=npi,
        override_address_1=_field("override_address_1"),
        override_city=_field("override_city"),
        override_state=_field("override_state"),
        override_zip=_field("override_zip"),
        override_phone=_field("override_phone"),
        notes=_field("notes"),
        reason=_field("reason"),
    )
    if entry is None:
        return HTMLResponse("Entry not found", status_code=404)
    return _render("_referral_list_entry.html", {"request": request, "entry": entry})


@app.delete("/saved/{npi}/referral/{list_id}", response_class=HTMLResponse)
async def remove_from_referral_list(
    npi: str,
    list_id: int,
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Remove a provider from a referral list (htmx DELETE from entry card)."""
    storage.remove_from_referral_list(list_id, npi)
    return HTMLResponse("")
```

- [ ] **Step 2: Create `src/docstats/templates/_list_badge.html`**

```html
{% if lists %}
<span
    hx-get="/saved/{{ npi }}/referral/form?list_id={{ lists[0].id }}&edit=1"
    hx-target="#referral-action-{{ npi }}"
    hx-swap="innerHTML"
    style="font-size: 0.8em; color: #2e7d32; background: #e8f5e9; padding: 3px 8px; border-radius: 10px; cursor: pointer; white-space: nowrap;"
    title="Click to edit referral entry"
>&#10003; {{ lists[0].name }}{% if lists | length > 1 %} (+{{ lists | length - 1 }}){% endif %}</span>
{% else %}
<button
    hx-get="/saved/{{ npi }}/referral/form"
    hx-target="#referral-action-{{ npi }}"
    hx-swap="innerHTML"
    class="outline"
    style="padding: 0.25rem 0.5rem; font-size: 0.8rem; white-space: nowrap;"
>+ Add to List</button>
{% endif %}
```

- [ ] **Step 3: Create `src/docstats/templates/_add_to_list_form.html`**

```html
{% if edit %}
<form
    hx-patch="/saved/{{ npi }}/referral/{{ list_id }}"
    hx-target="#entry-card-{{ npi }}"
    hx-swap="outerHTML"
    style="margin-top: 0.75rem; padding: 0.75rem; background: var(--pico-card-background-color); border: 1px solid var(--pico-muted-border-color); border-radius: var(--pico-border-radius);"
>
{% else %}
<form
    hx-post="/saved/{{ npi }}/referral"
    hx-target="#referral-action-{{ npi }}"
    hx-swap="innerHTML"
    style="margin-top: 0.5rem; padding: 0.75rem; background: var(--pico-card-background-color); border: 1px solid var(--pico-muted-border-color); border-radius: var(--pico-border-radius);"
>
    <div style="margin-bottom: 0.5rem;">
        <label style="font-size: 0.8em; font-weight: 600; margin-bottom: 0.25rem; display: block;">ADD TO LIST</label>
        <select name="list_id" id="list-select-{{ npi }}"
                onchange="var n=document.getElementById('new-list-wrap-{{ npi }}');if(n)n.style.display=this.value==='new'?'block':'none';"
                style="font-size: 0.85em; padding: 0.3rem 0.5rem;">
            {% for rl in lists %}
            <option value="{{ rl.id }}">{{ rl.name }}</option>
            {% endfor %}
            <option value="new">+ Create new list…</option>
        </select>
        <div id="new-list-wrap-{{ npi }}" style="display: none; margin-top: 0.35rem;">
            <input type="text" name="list_name" placeholder="List name…"
                   style="font-size: 0.85em; padding: 0.3rem 0.5rem;">
        </div>
        {% if not lists %}
        <script>
        (function() {
            var sel = document.getElementById('list-select-{{ npi }}');
            var wrap = document.getElementById('new-list-wrap-{{ npi }}');
            if (sel && wrap && sel.value === 'new') wrap.style.display = 'block';
        })();
        </script>
        {% endif %}
    </div>
{% endif %}

    <div style="margin-bottom: 0.5rem;">
        <label style="font-size: 0.8em; font-weight: 600; margin-bottom: 0.25rem; display: block;">REASON FOR REFERRAL</label>
        <input type="text" name="reason"
               value="{{ entry.reason if entry else '' }}"
               placeholder="e.g. Cardiology consult for chest pain"
               style="font-size: 0.85em; padding: 0.3rem 0.5rem; width: 100%;">
    </div>

    <div style="margin-bottom: 0.5rem;">
        <label style="font-size: 0.8em; font-weight: 600; margin-bottom: 0.25rem; display: block;">
            APPOINTMENT ADDRESS
            <span style="font-weight: 400; color: var(--pico-muted-color);">
                (blank = use NPI address: {{ provider.address_line1 or "none on file" }})
            </span>
        </label>
        <input type="text" name="override_address_1"
               value="{{ entry.override_address_1 if entry else '' }}"
               placeholder="Street address"
               style="font-size: 0.85em; padding: 0.3rem 0.5rem; width: 100%; margin-bottom: 0.3rem;">
        <div style="display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 0.35rem;">
            <input type="text" name="override_city"
                   value="{{ entry.override_city if entry else '' }}"
                   placeholder="City"
                   style="font-size: 0.85em; padding: 0.3rem 0.5rem;">
            <input type="text" name="override_state"
                   value="{{ entry.override_state if entry else '' }}"
                   placeholder="ST" maxlength="2"
                   style="font-size: 0.85em; padding: 0.3rem 0.5rem;">
            <input type="text" name="override_zip"
                   value="{{ entry.override_zip if entry else '' }}"
                   placeholder="ZIP"
                   style="font-size: 0.85em; padding: 0.3rem 0.5rem;">
        </div>
    </div>

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin-bottom: 0.5rem;">
        <div>
            <label style="font-size: 0.8em; font-weight: 600; margin-bottom: 0.25rem; display: block;">
                APPOINTMENT PHONE
                <span style="font-weight: 400; color: var(--pico-muted-color);">(NPI: {{ provider.phone or "none" }})</span>
            </label>
            <input type="text" name="override_phone"
                   value="{{ entry.override_phone if entry else '' }}"
                   placeholder="(555) 555-0100"
                   style="font-size: 0.85em; padding: 0.3rem 0.5rem; width: 100%;">
        </div>
        <div>
            <label style="font-size: 0.8em; font-weight: 600; margin-bottom: 0.25rem; display: block;">NOTES</label>
            <input type="text" name="notes"
                   value="{{ entry.notes if entry else '' }}"
                   placeholder="e.g. Tuesdays only, need pre-auth"
                   style="font-size: 0.85em; padding: 0.3rem 0.5rem; width: 100%;">
        </div>
    </div>

    <div style="display: flex; gap: 0.5rem; justify-content: flex-end;">
        {% if edit %}
        <button type="button"
                onclick="document.getElementById('entry-form-{{ npi }}').innerHTML = '';"
                class="outline secondary"
                style="padding: 0.3rem 0.75rem; font-size: 0.85rem;">Cancel</button>
        <button type="submit"
                style="padding: 0.3rem 0.75rem; font-size: 0.85rem;">Save Changes</button>
        {% else %}
        <button type="button"
                hx-get="/saved/{{ npi }}/referral/button"
                hx-target="#referral-action-{{ npi }}"
                hx-swap="innerHTML"
                class="outline secondary"
                style="padding: 0.3rem 0.75rem; font-size: 0.85rem;">Cancel</button>
        <button type="submit"
                style="padding: 0.3rem 0.75rem; font-size: 0.85rem;">Add to List</button>
        {% endif %}
    </div>
</form>
```

- [ ] **Step 4: Update `src/docstats/templates/saved.html`**

The `saved.html` route currently passes `providers`. Update the `/saved` route in `web.py` to also pass `referral_lists_by_npi`:

```python
@app.get("/saved", response_class=HTMLResponse)
async def saved(request: Request, storage: Storage = Depends(get_storage)):
    providers = storage.list_providers()
    referral_lists_by_npi = storage.get_npi_to_referral_lists()
    return _render("saved.html", {
        "request": request,
        "active_page": "saved",
        "providers": providers,
        "referral_lists_by_npi": referral_lists_by_npi,
    })
```

Then update `src/docstats/templates/saved.html`. Replace the `<th></th>` (last Actions column header) with a two-column approach — add a "Referral" header, and update each row's action cell to include the referral action div:

```html
{% extends "base.html" %}
{% block title %}Saved Providers - docstats{% endblock %}
{% block content %}
<h2>Saved Providers</h2>

{% if not providers %}
<p><em>No saved providers yet. Search for providers and save them for quick reference.</em></p>
{% else %}
<div style="overflow-x: auto;">
<table class="results-table striped">
    <thead>
        <tr>
            <th>NPI</th>
            <th>Type</th>
            <th>Name</th>
            <th>Specialty</th>
            <th>Location</th>
            <th>Phone</th>
            <th>Notes</th>
            <th>Referral</th>
            <th></th>
        </tr>
    </thead>
    <tbody>
        {% for p in providers %}
        <tr id="saved-row-{{ p.npi }}">
            <td><a href="/provider/{{ p.npi }}">{{ p.npi }}</a></td>
            <td>
                {% if p.entity_type == "Individual" %}
                <span class="badge-ind">Ind</span>
                {% else %}
                <span class="badge-org">Org</span>
                {% endif %}
            </td>
            <td><a href="/provider/{{ p.npi }}">{{ p.display_name }}</a></td>
            <td>{{ p.specialty or "" }}</td>
            <td>
                {% if p.address_city and p.address_state %}
                {{ p.address_city }}, {{ p.address_state }}
                {% endif %}
            </td>
            <td>{{ p.phone or "" }}</td>
            <td>{{ p.notes or "" }}</td>
            <td>
                <div id="referral-action-{{ p.npi }}">
                    {% set provider_lists = referral_lists_by_npi.get(p.npi, []) %}
                    {% if provider_lists %}
                    <span
                        hx-get="/saved/{{ p.npi }}/referral/form?list_id={{ provider_lists[0].id }}&edit=1"
                        hx-target="#referral-action-{{ p.npi }}"
                        hx-swap="innerHTML"
                        style="font-size: 0.8em; color: #2e7d32; background: #e8f5e9; padding: 3px 8px; border-radius: 10px; cursor: pointer; white-space: nowrap;"
                        title="Click to edit referral entry"
                    >&#10003; {{ provider_lists[0].name }}{% if provider_lists | length > 1 %} (+{{ provider_lists | length - 1 }}){% endif %}</span>
                    {% else %}
                    <button
                        hx-get="/saved/{{ p.npi }}/referral/form"
                        hx-target="#referral-action-{{ p.npi }}"
                        hx-swap="innerHTML"
                        class="outline"
                        style="padding: 0.25rem 0.5rem; font-size: 0.8rem; white-space: nowrap;"
                    >+ Add to List</button>
                    {% endif %}
                </div>
            </td>
            <td>
                <div style="display: flex; gap: 0.25rem;">
                    <a href="/provider/{{ p.npi }}/export" role="button" class="outline secondary"
                       style="padding: 0.25rem 0.5rem; font-size: 0.8rem;">Export</a>
                    <button
                        hx-delete="/provider/{{ p.npi }}/save"
                        hx-target="#saved-row-{{ p.npi }}"
                        hx-swap="outerHTML"
                        class="outline secondary"
                        style="padding: 0.25rem 0.5rem; font-size: 0.8rem;"
                    >Remove</button>
                </div>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Verify the full add-to-list flow manually**

```bash
docstats web
```

1. Search for a provider and save them.
2. Go to `/saved` — the Referral column should show "+ Add to List".
3. Click "+ Add to List" — form should expand inline in the cell.
4. If no lists exist, "Create new list…" should be selected and the name input visible.
5. Fill in a reason and click "Add to List" — the cell should replace with a green "✓ [list name]" badge.
6. Go to `/referral-lists` — the list should appear with provider_count = 1.
7. Click the list name → detail page shows the entry with the reason.
8. Click Edit on the entry — edit form opens. Change reason and save — card updates.
9. Click Remove — entry disappears. Saved provider is still on `/saved`.
10. On `/saved`, click the green badge → edit form opens.
11. Click Cancel in the edit form → badge restored.

- [ ] **Step 6: Run full test suite**

```bash
pytest -v
ruff check .
```

Expected: all passing, no lint errors

- [ ] **Step 7: Commit**

```bash
git add src/docstats/web.py src/docstats/templates/saved.html src/docstats/templates/_add_to_list_form.html src/docstats/templates/_list_badge.html
git commit -m "feat: add inline add-to-list flow on saved providers page"
```

---

### Task 9: Web — export page

**Files:**
- Modify: `src/docstats/web.py`
- Create: `src/docstats/templates/referral_list_export.html`

- [ ] **Step 1: Add 2 export routes to `web.py`**

```python
@app.get("/referral-lists/{list_id}/export", response_class=HTMLResponse)
async def referral_list_export(
    list_id: int,
    request: Request,
    view: str = Query("text"),
    storage: Storage = Depends(get_storage),
):
    """Export page for a referral list. ?view=text (default) or ?view=print."""
    rl = storage.get_referral_list(list_id)
    if rl is None:
        return HTMLResponse("List not found", status_code=404)
    entries = storage.get_referral_list_entries(list_id)
    export_text = referral_list_export_text(rl, entries)
    return _render("referral_list_export.html", {
        "request": request,
        "active_page": "referral-lists",
        "referral_list": rl,
        "entries": entries,
        "export_text": export_text,
        "view": view,
    })


@app.get("/referral-lists/{list_id}/export/text")
async def referral_list_export_download(
    list_id: int,
    request: Request,
    storage: Storage = Depends(get_storage),
):
    """Download the referral list as a plain text file."""
    rl = storage.get_referral_list(list_id)
    if rl is None:
        return HTMLResponse("List not found", status_code=404)
    entries = storage.get_referral_list_entries(list_id)
    text = referral_list_export_text(rl, entries)
    safe_name = rl.name.lower().replace(" ", "-").replace("/", "-")
    filename = f"referral-{safe_name}.txt"
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

- [ ] **Step 2: Create `src/docstats/templates/referral_list_export.html`**

```html
{% extends "base.html" %}
{% block title %}Export — {{ referral_list.name }} - docstats{% endblock %}
{% block content %}
<div style="margin-bottom: 1rem;">
    <a href="/referral-lists/{{ referral_list.id }}" style="font-size: 0.85em; color: var(--pico-muted-color);">&larr; {{ referral_list.name }}</a>
    <h2 style="margin: 0.25rem 0 0;">Export Referral Document</h2>
</div>

<!-- Tab bar -->
<div style="display: flex; gap: 0; margin-bottom: 1rem; border-bottom: 2px solid var(--pico-muted-border-color);">
    <a href="/referral-lists/{{ referral_list.id }}/export?view=text"
       style="padding: 0.5rem 1.25rem; font-size: 0.9rem; text-decoration: none;
              {% if view == 'text' %}border-bottom: 2px solid var(--pico-primary); font-weight: 600; color: var(--pico-primary); margin-bottom: -2px;{% else %}color: var(--pico-muted-color);{% endif %}">
        Plain Text
    </a>
    <a href="/referral-lists/{{ referral_list.id }}/export?view=print"
       style="padding: 0.5rem 1.25rem; font-size: 0.9rem; text-decoration: none;
              {% if view == 'print' %}border-bottom: 2px solid var(--pico-primary); font-weight: 600; color: var(--pico-primary); margin-bottom: -2px;{% else %}color: var(--pico-muted-color);{% endif %}">
        Printable View
    </a>
</div>

{% if view == "text" %}
<!-- Plain text tab -->
<div style="display: flex; gap: 0.5rem; justify-content: flex-end; margin-bottom: 0.75rem;">
    <button id="copy-btn" onclick="copyExport()" class="outline"
            style="padding: 0.35rem 0.75rem; font-size: 0.85rem;">Copy All</button>
    <a href="/referral-lists/{{ referral_list.id }}/export/text" role="button"
       style="padding: 0.35rem 0.75rem; font-size: 0.85rem;">&#8595; Download .txt</a>
</div>
<pre id="export-text" class="export-block">{{ export_text }}</pre>
<script>
function copyExport() {
    var text = document.getElementById('export-text').textContent;
    navigator.clipboard.writeText(text).then(function() {
        var btn = document.getElementById('copy-btn');
        btn.textContent = 'Copied!';
        setTimeout(function() { btn.textContent = 'Copy All'; }, 2000);
    });
}
</script>

{% else %}
<!-- Printable tab -->
<style>
@media print {
    header, footer, .no-print { display: none !important; }
    .print-card { page-break-inside: avoid; }
}
</style>
<div class="no-print" style="display: flex; justify-content: flex-end; margin-bottom: 0.75rem;">
    <button onclick="window.print()"
            style="padding: 0.35rem 0.9rem; font-size: 0.85rem;">&#128438; Print / Save as PDF</button>
</div>

<div style="max-width: 680px; margin: 0 auto;">
    <h3 style="margin-bottom: 0.15rem;">Referral Request</h3>
    <p style="font-size: 0.85em; color: var(--pico-muted-color); margin-bottom: 1.25rem;">
        {{ referral_list.name }}
    </p>

    {% for entry in entries %}
    <div class="print-card" style="border: 1px solid var(--pico-muted-border-color); border-radius: var(--pico-border-radius); padding: 1rem; margin-bottom: 1rem;">
        <div style="display: flex; justify-content: space-between; align-items: baseline;">
            <strong>{{ entry.display_name }}</strong>
            <span style="font-size: 0.82em; color: var(--pico-muted-color);">NPI {{ entry.npi }}</span>
        </div>
        {% if entry.specialty %}
        <div style="color: var(--pico-primary); font-size: 0.85em; margin: 0.2rem 0 0.75rem;">{{ entry.specialty }}</div>
        {% endif %}
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; font-size: 0.85em;">
            <div>
                <div style="font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.04em; color: var(--pico-muted-color); margin-bottom: 0.2rem;">Appointment</div>
                {% if entry.effective_address_1 %}<div>{{ entry.effective_address_1 }}</div>{% endif %}
                <div>{{ entry.effective_city }}{% if entry.effective_city and entry.effective_state %}, {% endif %}{{ entry.effective_state }} {{ entry.effective_zip }}</div>
                {% if entry.effective_phone %}<div>{{ entry.effective_phone }}</div>{% endif %}
                {% if entry.address_overridden %}
                <div style="font-size: 0.82em; color: #2e7d32; margin-top: 0.2rem;">&#9998; address overridden</div>
                {% endif %}
            </div>
            <div>
                {% if entry.reason %}
                <div style="font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.04em; color: var(--pico-muted-color); margin-bottom: 0.2rem;">Reason</div>
                <div style="margin-bottom: 0.5rem;">{{ entry.reason }}</div>
                {% endif %}
                {% if entry.notes %}
                <div style="font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.04em; color: var(--pico-muted-color); margin-bottom: 0.2rem;">Notes</div>
                <div>{{ entry.notes }}</div>
                {% endif %}
            </div>
        </div>
    </div>
    {% endfor %}

    <p style="font-size: 0.75em; color: var(--pico-muted-color); text-align: center; margin-top: 1.5rem;">
        NPI data sourced from CMS NPPES Registry
    </p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 3: Verify export end-to-end**

```bash
docstats web
```

1. Add 2 providers to a referral list (with at least one having override address).
2. Go to `/referral-lists/1/export` — plain text tab loads with correct content.
3. Verify overridden addresses show the note "appointment address differs from NPI listing".
4. Click "Copy All" — clipboard should contain the export text.
5. Click "Download .txt" — file downloads as `referral-[list-name].txt` with correct content.
6. Switch to "Printable View" — cards display correctly.
7. Click "Print / Save as PDF" — browser print dialog opens.
8. Verify nav/buttons are hidden in print preview via `@media print`.

- [ ] **Step 4: Run full test suite and lint**

```bash
pytest -v
ruff check .
```

Expected: all passing, no errors

- [ ] **Step 5: Commit**

```bash
git add src/docstats/web.py src/docstats/templates/referral_list_export.html
git commit -m "feat: add referral list export page with plain text and printable views"
```

---

## End-to-End Verification Checklist

Run these after all tasks are complete:

```bash
pip install -e ".[web]"
pytest -v
ruff check .
docstats web
```

1. [ ] "Referral Lists" appears in nav between Saved and History
2. [ ] Search for a provider → save → appears on `/saved` with "+ Add to List"
3. [ ] Click "+ Add to List" → inline form appears with NPI address/phone pre-filled
4. [ ] If no lists: "Create new list…" is pre-selected and name input is visible
5. [ ] Submit form → green "✓ [list name]" badge appears; list exists at `/referral-lists`
6. [ ] Add second provider to same list → detail page shows both
7. [ ] Edit entry override address → "✎ overridden" indicator shows
8. [ ] Remove entry from list → saved provider still on `/saved`
9. [ ] Delete referral list → list gone; saved providers unaffected
10. [ ] Export → plain text has "PROVIDER 1 OF N" headers; overridden addresses flagged
11. [ ] Export → printable view shows cards; Print opens browser dialog
12. [ ] Download .txt → file downloads with correct filename and content
13. [ ] Delete a saved provider → they disappear from referral list entries (cascade)
14. [ ] `pytest` passes; `ruff check .` clean
