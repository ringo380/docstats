"""CSV bulk import staging.

Two tables model the upload → map → validate → commit pipeline:

- ``csv_imports`` — one row per uploaded file. Scope-owned (user XOR org,
  same CHECK-XOR pattern as patients / insurance_plans). Tracks the overall
  lifecycle of the batch + a column-mapping JSON blob + an error-report
  roll-up for the review UI.

- ``csv_import_rows`` — one row per CSV row, hanging off the parent import.
  No scope columns; scope flows transitively through the parent import.
  Holds the raw CSV row as JSON plus per-row validation errors, and once
  a row is committed it's linked to the resulting referral via ``referral_id``.

State machine for ``csv_imports.status``:

    uploaded ──► mapped ──► validated ──► committed
         │          │           │
         └──────────┴───────────┴──► failed (any error)

State machine for ``csv_import_rows.status``:

    pending ──► valid ──► committed
       │         │
       ├────────►┴──► skipped  (coordinator chose not to create)
       ▼
     error ──► valid  (after inline edit)
         └──► skipped

Storage stays dumb (accepts any valid enum); the Phase 4 route layer
validates transitions via :func:`require_import_transition` /
:func:`require_row_transition`. Keeps the state machine in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, Field

# Batch-level lifecycle.
IMPORT_STATUS_VALUES: Final[tuple[str, ...]] = (
    "uploaded",  # file received, not yet mapped
    "mapped",  # column → field mapping persisted
    "validated",  # rows scored (valid/error/pending)
    "committed",  # valid rows written as referrals
    "failed",  # terminal error (unparseable file, size cap, etc.)
)

IMPORT_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"committed", "failed"})

IMPORT_STATUS_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "uploaded": frozenset({"mapped", "failed"}),
    "mapped": frozenset({"validated", "failed"}),
    "validated": frozenset({"committed", "failed"}),
    "committed": frozenset(),
    "failed": frozenset(),
}

# Per-row status.
IMPORT_ROW_STATUS_VALUES: Final[tuple[str, ...]] = (
    "pending",  # not yet validated
    "valid",  # passes all validators
    "error",  # one or more validation errors present
    "committed",  # referral created from this row
    "skipped",  # coordinator opted out of committing
)

IMPORT_ROW_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"committed", "skipped"})

IMPORT_ROW_STATUS_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "pending": frozenset({"valid", "error", "skipped"}),
    "valid": frozenset({"committed", "skipped", "error"}),
    "error": frozenset({"valid", "skipped"}),
    "committed": frozenset(),
    "skipped": frozenset(),
}


class InvalidImportTransition(ValueError):
    """Raised when a disallowed import-status edge is attempted."""


class InvalidImportRowTransition(ValueError):
    """Raised when a disallowed import-row-status edge is attempted."""


def import_transition_allowed(from_status: str, to_status: str) -> bool:
    allowed = IMPORT_STATUS_TRANSITIONS.get(from_status)
    return bool(allowed and to_status in allowed)


def require_import_transition(from_status: str, to_status: str) -> None:
    if not import_transition_allowed(from_status, to_status):
        raise InvalidImportTransition(
            f"Invalid csv_import transition: {from_status!r} → {to_status!r}"
        )


def row_transition_allowed(from_status: str, to_status: str) -> bool:
    allowed = IMPORT_ROW_STATUS_TRANSITIONS.get(from_status)
    return bool(allowed and to_status in allowed)


def require_row_transition(from_status: str, to_status: str) -> None:
    if not row_transition_allowed(from_status, to_status):
        raise InvalidImportRowTransition(
            f"Invalid csv_import_row transition: {from_status!r} → {to_status!r}"
        )


class CsvImport(BaseModel):
    """A single CSV upload attempt."""

    id: int
    scope_user_id: int | None = None
    scope_organization_id: int | None = None

    uploaded_by_user_id: int | None = None
    original_filename: str
    row_count: int = 0
    status: str = "uploaded"  # must be in IMPORT_STATUS_VALUES

    # Column → target-field mapping, e.g.
    # ``{"patient_first_name": "Patient First", "receiving_npi": "NPI"}``.
    # Populated by the Phase 4 mapping UI; stays ``{}`` until then.
    mapping: dict[str, Any] = Field(default_factory=dict)

    # Roll-up for the review UI: totals, recurring error categories, etc.
    # The per-row detail lives in ``csv_import_rows.validation_errors``.
    error_report: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime
    updated_at: datetime


class CsvImportRow(BaseModel):
    """One row from the parent ``csv_imports``.

    ``raw_json`` is the verbatim CSV row as a dict (header → cell value).
    ``validation_errors`` is a dict keyed by field name; the Phase 4
    validator fills it in. Once a row is committed, ``referral_id`` links
    to the created referral for audit traceability.
    """

    id: int
    import_id: int
    row_index: int  # 1-based row number in the source file
    raw_json: dict[str, Any] = Field(default_factory=dict)
    validation_errors: dict[str, Any] = Field(default_factory=dict)
    referral_id: int | None = None
    status: str = "pending"  # must be in IMPORT_ROW_STATUS_VALUES
    created_at: datetime
    updated_at: datetime
