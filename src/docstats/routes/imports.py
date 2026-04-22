"""CSV bulk-import routes — Phase 4.

Full pipeline: upload → map → validate → review → commit. All five slices
(4.A–4.E) ship in this file: upload + list, column mapping + auto-match,
row-level validation, inline review + row edit, commit + summary +
error-report CSV, and the downloadable column-contract template.

On upload we parse the CSV once, persist every row to ``csv_import_rows``
with ``raw_json`` populated (Phase 1.F schema), and then re-read from the DB
for every subsequent step. No disk/blob storage needed — Railway's ephemeral
filesystem and Supabase Storage's Phase-10 scope stay out of the picture.

Limits: 5 MB file cap and 2000 rows (both enforced at parse time, with a
Content-Length pre-check to reject oversized uploads before Starlette
spools the body to a SpooledTemporaryFile). Larger imports split at the UI;
there's no silent truncation.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Path, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from docstats.domain.audit import record as audit_record
from docstats.domain.imports import (
    IMPORT_ROW_STATUS_VALUES,
    IMPORT_STATUS_VALUES,
    InvalidImportRowTransition,
    InvalidImportTransition,
    require_import_transition,
    require_row_transition,
)
from docstats.domain.imports_validate import validate_row
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope, redirect_htmx, render, saved_count
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

# Canonical target fields the mapping UI offers for each CSV column.
# Sentinels: empty string = "unmapped" (ignored during validate/commit).
# Groups exist so the UI can render related targets together without
# hard-coding the list into the template.
TARGET_FIELD_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Patient",
        (
            ("patient_first_name", "First name"),
            ("patient_last_name", "Last name"),
            ("patient_middle_name", "Middle name"),
            ("patient_dob", "Date of birth (ISO YYYY-MM-DD)"),
            ("patient_mrn", "MRN"),
            ("patient_sex", "Sex"),
            ("patient_phone", "Phone"),
            ("patient_email", "Email"),
        ),
    ),
    (
        "Clinical reason",
        (
            ("reason", "Reason for referral"),
            ("clinical_question", "Clinical question"),
            ("urgency", "Urgency (routine / priority / urgent / stat)"),
            ("requested_service", "Requested service"),
            ("diagnosis_primary_icd", "Primary ICD-10 code"),
            ("diagnosis_primary_text", "Primary diagnosis description"),
        ),
    ),
    (
        "Receiving specialist",
        (
            ("receiving_provider_npi", "NPI"),
            ("receiving_organization_name", "Organization / clinic"),
            ("specialty_code", "Specialty NUCC code"),
            ("specialty_desc", "Specialty description"),
        ),
    ),
    (
        "Referring side",
        (
            ("referring_provider_name", "Referring provider name"),
            ("referring_provider_npi", "Referring provider NPI"),
            ("referring_organization", "Referring organization"),
        ),
    ),
    (
        "Authorization",
        (
            ("authorization_number", "Auth number"),
            ("authorization_status", "Auth status"),
        ),
    ),
)

# Flat set of all valid target keys; used to validate the POSTed mapping
# against forged values (unknown keys → 422).
_VALID_TARGET_KEYS: frozenset[str] = frozenset(
    key for _, items in TARGET_FIELD_GROUPS for key, _ in items
)

# Heuristic auto-match: a CSV header is matched to a target if its
# lowercased slug equals the target key OR any of the listed aliases.
# Keeps the mapping UI pre-populated for the common case where a user's
# column names are reasonable.
_TARGET_ALIASES: dict[str, tuple[str, ...]] = {
    "patient_first_name": ("first_name", "first", "fname", "given_name", "patient_first"),
    "patient_last_name": ("last_name", "last", "lname", "surname", "patient_last", "family_name"),
    "patient_middle_name": ("middle_name", "middle", "mname"),
    "patient_dob": ("dob", "date_of_birth", "birth_date", "birthday"),
    "patient_mrn": ("mrn", "medical_record_number", "record_number"),
    "patient_sex": ("sex", "gender"),
    "patient_phone": ("phone", "phone_number", "patient_telephone"),
    "patient_email": ("email", "patient_email"),
    "reason": ("reason", "referral_reason", "chief_complaint"),
    "clinical_question": ("clinical_question", "question", "consult_question"),
    "urgency": ("urgency", "priority"),
    "requested_service": ("requested_service", "service"),
    "diagnosis_primary_icd": ("icd", "icd10", "diagnosis_code", "primary_dx_code"),
    "diagnosis_primary_text": ("diagnosis", "primary_dx", "dx", "dx_description"),
    "receiving_provider_npi": ("npi", "receiving_npi", "specialist_npi", "to_npi", "dest_npi"),
    "receiving_organization_name": ("receiving_org", "clinic", "destination", "specialist_org"),
    "specialty_code": ("specialty_code", "taxonomy_code", "nucc"),
    "specialty_desc": ("specialty", "specialty_name", "taxonomy"),
    "referring_provider_name": ("referring_name", "pcp_name", "from_name"),
    "referring_provider_npi": ("referring_npi", "pcp_npi", "from_npi"),
    "referring_organization": ("referring_org", "pcp_org"),
    "authorization_number": ("auth_number", "auth", "authorization"),
    "authorization_status": ("auth_status", "authorization_state"),
}


def _auto_match(header: str) -> str:
    """Return a target field key for ``header`` if there's an obvious match, else ''."""
    slug = header.strip().lower().replace("-", "_").replace(" ", "_")
    if slug in _VALID_TARGET_KEYS:
        return slug
    for key, aliases in _TARGET_ALIASES.items():
        if slug in aliases:
            return key
    return ""


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])

# Hard caps enforced at parse time — larger imports must be split by the user.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_UPLOAD_ROWS = 2000


def _ctx(request: Request, user: dict, storage: StorageBase, **extra) -> dict:
    return {
        "request": request,
        "active_page": "imports",
        "user": user,
        "saved_count": saved_count(storage, user["id"]),
        **extra,
    }


@router.get("", response_class=HTMLResponse)
async def imports_list(
    request: Request,
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    imports = storage.list_csv_imports(scope, limit=50)
    return render(
        "imports_list.html",
        _ctx(
            request,
            current_user,
            storage,
            imports=imports,
            status_values=IMPORT_STATUS_VALUES,
            max_bytes=MAX_UPLOAD_BYTES,
            max_rows=MAX_UPLOAD_ROWS,
        ),
    )


@router.get("/template.csv")
async def import_template():
    """Downloadable CSV template with the canonical header row + a sample.

    Unauthenticated — it's just a static template. Coordinators share this
    link with the people collecting the source data. Returns the literal
    target keys as headers so the mapping UI auto-matches every column
    without user intervention on the happy path.
    """
    headers = [key for _, items in TARGET_FIELD_GROUPS for key, _ in items]
    sample = {
        "patient_first_name": "Jane",
        "patient_last_name": "Doe",
        "patient_dob": "1980-05-15",
        "patient_mrn": "MRN-12345",
        "reason": "Chest pain eval",
        "clinical_question": "Rule out cardiac cause",
        "urgency": "routine",
        "specialty_code": "207RC0000X",
        "specialty_desc": "Cardiology",
    }
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerow([sample.get(h, "") for h in headers])
    filename = "docstats_referral_import_template.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _parse_csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Decode + parse CSV bytes. Returns (headers, rows).

    Uses ``csv.DictReader`` so every row is a mapping of header-string →
    raw cell value. Trims BOM if present (Excel-saved CSVs on Windows
    often ship with ``\\ufeff`` at the head of the first column name).
    Raises ``HTTPException(422)`` on any parse failure — the route layer
    catches nothing and lets FastAPI surface the message.
    """
    try:
        text = raw.decode("utf-8-sig")  # handles the Excel BOM
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"File is not UTF-8: {exc}") from exc
    buf = io.StringIO(text)
    reader = csv.DictReader(buf)
    if reader.fieldnames is None:
        raise HTTPException(status_code=422, detail="CSV file has no header row.")
    headers = [h.strip() for h in reader.fieldnames if h]
    if not headers:
        raise HTTPException(status_code=422, detail="CSV file has no columns.")
    rows: list[dict[str, str]] = []
    # Wrap the iteration: DictReader raises ``csv.Error`` on malformed quoting,
    # NUL bytes, etc. Without this, the docstring's "422 on any parse failure"
    # contract silently degrades to a 500.
    try:
        for row in reader:
            # Cap BEFORE append so the 2001st row never lands in memory.
            if len(rows) >= MAX_UPLOAD_ROWS:
                raise HTTPException(
                    status_code=422,
                    detail=f"CSV exceeds {MAX_UPLOAD_ROWS}-row cap. Split the file and retry.",
                )
            # DictReader returns None for missing cells on short rows; coerce
            # to empty str. ``if k is not None`` (rather than ``if k``) lets
            # legitimately empty-string headers flow through — DictReader
            # still drops long-row leftovers into a None key.
            cleaned = {
                k.strip(): (v if v is not None else "") for k, v in row.items() if k is not None
            }
            rows.append(cleaned)
    except csv.Error as exc:
        raise HTTPException(status_code=422, detail=f"Malformed CSV: {exc}") from exc
    return headers, rows


@router.post("", response_class=HTMLResponse)
async def import_create(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    if not file.filename:
        raise HTTPException(status_code=422, detail="No file uploaded.")
    # Early cap via Content-Length — rejects oversized multipart bodies
    # before Starlette spools the full payload to a SpooledTemporaryFile.
    # Honest clients set this header; attackers can omit it, which is why
    # we still do the explicit len() check below as defense-in-depth.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap.",
                )
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid Content-Length header.")
    # UploadFile.read() is unbounded; cap manually (defense in depth vs.
    # clients that lie about Content-Length).
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap.",
        )
    if not raw:
        raise HTTPException(status_code=422, detail="File is empty.")

    headers, rows = _parse_csv(raw)
    if not rows:
        raise HTTPException(status_code=422, detail="CSV has no data rows.")

    csv_import = storage.create_csv_import(
        scope,
        original_filename=file.filename[:255],
        uploaded_by_user_id=current_user["id"],
        row_count=len(rows),
    )

    # Seed csv_import_rows. Row index is 1-based to match user expectation
    # ("row 1 of the CSV" = first data row after the header).
    for idx, row in enumerate(rows, start=1):
        storage.add_csv_import_row(
            scope,
            csv_import.id,
            row_index=idx,
            raw_json=row,
        )

    audit_record(
        storage,
        action="import.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.audit_user_id,
        scope_organization_id=scope.organization_id,
        entity_type="csv_import",
        entity_id=str(csv_import.id),
        metadata={"row_count": len(rows), "headers": headers},
    )

    dest = f"/imports/{csv_import.id}/map"
    return redirect_htmx(request, dest)


def _sorted_headers(rows: list) -> list[str]:
    """Return the union of header keys across the first few raw_json rows.

    CSVs are dict-like by header, but different rows may omit cells. Walking
    the first ~5 rows covers sparsely-populated columns; we preserve the
    order of first appearance to match the user's column layout.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for row in rows[:20]:
        for key in row.raw_json.keys():
            if key not in seen_set:
                seen.append(key)
                seen_set.add(key)
    return seen


@router.get("/{import_id}/map", response_class=HTMLResponse)
async def import_map_form(
    request: Request,
    import_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    preview_rows = storage.list_csv_import_rows(scope, import_id, limit=5)
    headers = _sorted_headers(preview_rows)

    # Preserve any mapping already saved on the import row (re-edit flow).
    # Otherwise auto-match each header to a target field.
    saved_mapping = csv_import.mapping or {}
    current_mapping: dict[str, str] = {}
    for h in headers:
        # mapping is stored as {target_field: csv_header}; invert for the UI
        # which keys by csv_header.
        for target, csv_col in saved_mapping.items():
            if csv_col == h:
                current_mapping[h] = target
                break
        if h not in current_mapping:
            current_mapping[h] = _auto_match(h)

    return render(
        "import_map.html",
        _ctx(
            request,
            current_user,
            storage,
            csv_import=csv_import,
            headers=headers,
            preview_rows=preview_rows,
            current_mapping=current_mapping,
            target_groups=TARGET_FIELD_GROUPS,
            valid_target_keys=_VALID_TARGET_KEYS,
        ),
    )


@router.post("/{import_id}/map", response_class=HTMLResponse)
async def import_map_save(
    request: Request,
    import_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")

    # FastAPI doesn't type-check arbitrary form keys; pull the raw form.
    form = await request.form()

    # Storage shape: {target_field: csv_header}. Inverting here means the
    # validator can index ``row.raw_json[mapping[target]]`` directly.
    new_mapping: dict[str, str] = {}
    seen_targets: set[str] = set()
    for key, value in form.multi_items():
        if not key.startswith("col__"):
            continue
        csv_header = key.removeprefix("col__")
        target = str(value).strip()
        if not target:
            continue  # "unmapped" sentinel
        if target not in _VALID_TARGET_KEYS:
            raise HTTPException(status_code=422, detail=f"Unknown target field {target!r}.")
        if target in seen_targets:
            raise HTTPException(
                status_code=422,
                detail=f"Target field {target!r} mapped to multiple CSV columns.",
            )
        seen_targets.add(target)
        new_mapping[target] = csv_header

    # Transition uploaded → mapped (or stay mapped on re-edit). We only
    # enforce the one edge from "uploaded" — re-editing a mapping on a
    # "mapped" import is fine; editing after validated/committed is not.
    if csv_import.status in ("validated", "committed", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Import is in status {csv_import.status!r}; cannot re-map.",
        )
    try:
        if csv_import.status == "uploaded":
            require_import_transition("uploaded", "mapped")
    except InvalidImportTransition as e:
        raise HTTPException(status_code=409, detail=str(e))

    storage.update_csv_import(
        scope,
        import_id,
        mapping=new_mapping,
        status="mapped",
    )
    audit_record(
        storage,
        action="import.map",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.audit_user_id,
        scope_organization_id=scope.organization_id,
        entity_type="csv_import",
        entity_id=str(import_id),
        metadata={"mapped_fields": sorted(new_mapping.keys())},
    )

    # Auto-run validation so the review page has classified rows immediately;
    # skips the "Run validators" intermediate click.
    counts = _run_validation(storage, scope, import_id)
    # State-machine discipline: even though this is the happy path we just
    # wrote "mapped" → "validated" on, guard the edge explicitly so the
    # transition map stays the single source of truth (see domain/imports.py
    # "route layer validates transitions" contract).
    try:
        require_import_transition("mapped", "validated")
    except InvalidImportTransition as e:
        raise HTTPException(status_code=409, detail=str(e))
    storage.update_csv_import(
        scope,
        import_id,
        status="validated",
        error_report={"valid": counts["valid"], "error": counts["error"]},
    )
    audit_record(
        storage,
        action="import.validate",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.audit_user_id,
        scope_organization_id=scope.organization_id,
        entity_type="csv_import",
        entity_id=str(import_id),
        metadata=counts,
    )

    dest = f"/imports/{import_id}/review"
    return redirect_htmx(request, dest)


# --- Validation + review (Phase 4.C) ---


def _run_validation(storage: StorageBase, scope: Scope, csv_import_id: int) -> dict[str, int]:
    """Run validators across every row and persist results.

    Returns a counts dict ``{valid, error}`` for the caller to store on
    ``csv_imports.error_report`` + audit. We cap the batch at the same
    2000-row limit the parser enforces, so listing all rows at once is
    cheap enough to skip pagination.
    """
    csv_import = storage.get_csv_import(scope, csv_import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    mapping: dict[str, str] = csv_import.mapping or {}
    rows = storage.list_csv_import_rows(scope, csv_import_id, limit=MAX_UPLOAD_ROWS)
    specialty_cache: dict = {}
    counts = {"valid": 0, "error": 0}
    for row in rows:
        errors = validate_row(
            row.raw_json,
            mapping,
            storage=storage,
            scope=scope,
            specialty_cache=specialty_cache,
        )
        new_status = "error" if errors else "valid"
        # Transition guard: rows at this point are pending/valid/error
        # (validate route blocks committed imports). valid→valid and
        # error→error are no-op transitions we allow by short-circuit.
        if row.status != new_status:
            try:
                require_row_transition(row.status, new_status)
            except InvalidImportRowTransition as e:
                logger.warning("skipping illegal row transition: %s", e)
                continue
        storage.update_csv_import_row(
            scope,
            csv_import_id,
            row.id,
            validation_errors=errors,
            status=new_status,
        )
        counts[new_status] += 1
    return counts


@router.post("/{import_id}/validate", response_class=HTMLResponse)
async def import_validate(
    request: Request,
    import_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    if csv_import.status not in ("mapped", "validated"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot validate import in status {csv_import.status!r}.",
        )
    if not csv_import.mapping:
        raise HTTPException(status_code=422, detail="Map columns before validating.")

    counts = _run_validation(storage, scope, import_id)

    # First-time transition from mapped → validated; re-runs stay on validated.
    if csv_import.status == "mapped":
        try:
            require_import_transition("mapped", "validated")
        except InvalidImportTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
    storage.update_csv_import(
        scope,
        import_id,
        status="validated",
        error_report={"valid": counts["valid"], "error": counts["error"]},
    )
    audit_record(
        storage,
        action="import.validate",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.audit_user_id,
        scope_organization_id=scope.organization_id,
        entity_type="csv_import",
        entity_id=str(import_id),
        metadata=counts,
    )
    dest = f"/imports/{import_id}/review"
    return redirect_htmx(request, dest)


@router.get("/{import_id}/review", response_class=HTMLResponse)
async def import_review(
    request: Request,
    import_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    rows = storage.list_csv_import_rows(scope, import_id, limit=MAX_UPLOAD_ROWS)
    counts = {s: 0 for s in IMPORT_ROW_STATUS_VALUES}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return render(
        "import_review.html",
        _ctx(
            request,
            current_user,
            storage,
            csv_import=csv_import,
            rows=rows,
            mapping=csv_import.mapping or {},
            counts=counts,
        ),
    )


@router.post("/{import_id}/rows/{row_id}/edit", response_class=HTMLResponse)
async def import_row_edit(
    request: Request,
    import_id: int = Path(..., ge=1),
    row_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Inline edit of a single row's raw_json cells + immediate re-validate.

    Form keys are ``cell__<header>`` so we can accept an arbitrary set of
    CSV columns without enumerating them in the signature.
    """
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    if csv_import.status == "committed":
        raise HTTPException(status_code=409, detail="Import is committed; rows frozen.")

    # Load current row state
    rows = storage.list_csv_import_rows(scope, import_id, limit=MAX_UPLOAD_ROWS)
    row = next((r for r in rows if r.id == row_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found.")

    form = await request.form()
    new_raw = dict(row.raw_json)
    for key, value in form.multi_items():
        if not key.startswith("cell__"):
            continue
        header = key.removeprefix("cell__")
        # Only update headers the row already knows about — reject
        # arbitrary new columns to keep the schema stable.
        if header in new_raw:
            new_raw[header] = str(value)

    # Re-validate with the updated cells.
    errors = validate_row(
        new_raw,
        csv_import.mapping or {},
        storage=storage,
        scope=scope,
    )
    new_status = "error" if errors else "valid"
    if row.status != new_status:
        try:
            require_row_transition(row.status, new_status)
        except InvalidImportRowTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
    storage.update_csv_import_row(
        scope,
        import_id,
        row_id,
        raw_json=new_raw,
        validation_errors=errors,
        status=new_status,
    )
    audit_record(
        storage,
        action="import.row.edit",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.audit_user_id,
        scope_organization_id=scope.organization_id,
        entity_type="csv_import_row",
        entity_id=f"{import_id}:{row_id}",
        metadata={"new_status": new_status, "error_count": len(errors)},
    )
    dest = f"/imports/{import_id}/review"
    return redirect_htmx(request, dest)


# --- Commit + summary (Phase 4.D) ---


def _value_or_none(raw: dict, mapping: dict[str, str], target: str) -> str | None:
    """Return the stripped cell value for ``target`` or ``None`` when unmapped/blank."""
    header = mapping.get(target)
    if not header:
        return None
    v = raw.get(header)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _upsert_patient_from_row(
    storage: StorageBase, scope: Scope, user_id: int, raw: dict, mapping: dict[str, str]
):
    """Match an existing patient by MRN; else create new.

    Returns ``(Patient, was_created)`` — the boolean lets the caller
    compensate (soft-delete) a just-created patient if the subsequent
    referral-create step fails, preventing orphan rows.

    Matching uses the ``mrn=`` exact-match kwarg on ``list_patients`` (a DB-
    level filter, not the old substring-search-then-filter pattern which
    had a limit-5 window that could miss the exact match when the MRN
    substring-matched five unrelated names). Fuzzy name+DOB matching with
    a coordinator-confirm step is deferred to a Phase 4 follow-up.
    """
    first = _value_or_none(raw, mapping, "patient_first_name") or ""
    last = _value_or_none(raw, mapping, "patient_last_name") or ""
    mrn = _value_or_none(raw, mapping, "patient_mrn")
    dob = _value_or_none(raw, mapping, "patient_dob")

    # MRN exact-match across the scope (only meaningful in org mode where MRNs
    # are unique per org; solo-mode MRN collisions are the user's own data).
    if mrn:
        existing = storage.list_patients(scope, mrn=mrn, limit=1)
        if existing:
            return existing[0], False

    # Create a new row. Date must be ISO or we'd have flagged it at validate.
    new_patient = storage.create_patient(
        scope,
        first_name=first,
        last_name=last,
        middle_name=_value_or_none(raw, mapping, "patient_middle_name"),
        date_of_birth=dob,
        sex=_value_or_none(raw, mapping, "patient_sex"),
        mrn=mrn,
        phone=_value_or_none(raw, mapping, "patient_phone"),
        email=_value_or_none(raw, mapping, "patient_email"),
        created_by_user_id=user_id,
    )
    return new_patient, True


def _create_referral_from_row(
    storage: StorageBase,
    scope: Scope,
    user_id: int,
    raw: dict,
    mapping: dict[str, str],
    patient_id: int,
):
    urgency = _value_or_none(raw, mapping, "urgency") or "routine"
    return storage.create_referral(
        scope,
        patient_id=patient_id,
        reason=_value_or_none(raw, mapping, "reason") or "",
        clinical_question=_value_or_none(raw, mapping, "clinical_question"),
        urgency=urgency,
        requested_service=_value_or_none(raw, mapping, "requested_service"),
        diagnosis_primary_icd=_value_or_none(raw, mapping, "diagnosis_primary_icd"),
        diagnosis_primary_text=_value_or_none(raw, mapping, "diagnosis_primary_text"),
        receiving_provider_npi=_value_or_none(raw, mapping, "receiving_provider_npi"),
        receiving_organization_name=_value_or_none(raw, mapping, "receiving_organization_name"),
        specialty_code=_value_or_none(raw, mapping, "specialty_code"),
        specialty_desc=_value_or_none(raw, mapping, "specialty_desc"),
        referring_provider_name=_value_or_none(raw, mapping, "referring_provider_name"),
        referring_provider_npi=_value_or_none(raw, mapping, "referring_provider_npi"),
        referring_organization=_value_or_none(raw, mapping, "referring_organization"),
        authorization_number=_value_or_none(raw, mapping, "authorization_number"),
        authorization_status=(_value_or_none(raw, mapping, "authorization_status") or "na_unknown"),
        external_source="bulk_csv",
        created_by_user_id=user_id,
    )


@router.post("/{import_id}/commit", response_class=HTMLResponse)
async def import_commit(
    request: Request,
    import_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    if csv_import.status != "validated":
        raise HTTPException(
            status_code=409,
            detail=f"Import must be in 'validated' status to commit (is {csv_import.status!r}).",
        )
    try:
        require_import_transition("validated", "committed")
    except InvalidImportTransition as e:
        raise HTTPException(status_code=409, detail=str(e))

    mapping: dict[str, str] = csv_import.mapping or {}
    rows = storage.list_csv_import_rows(scope, import_id, limit=MAX_UPLOAD_ROWS)
    user_id = current_user["id"]

    committed = 0
    skipped_error = 0
    failed: list[tuple[int, str]] = []  # (row_index, reason)
    for row in rows:
        if row.status != "valid":
            skipped_error += 1
            continue
        patient = None
        patient_was_created = False
        try:
            patient, patient_was_created = _upsert_patient_from_row(
                storage, scope, user_id, row.raw_json, mapping
            )
            referral = _create_referral_from_row(
                storage, scope, user_id, row.raw_json, mapping, patient.id
            )
        except Exception as exc:  # noqa: BLE001 — one-row failure shouldn't abort the batch
            logger.exception("commit failed for row %s of import %s", row.row_index, import_id)
            # Compensation: if we created a NEW patient but the referral
            # insert failed, soft-delete the patient so it doesn't orphan
            # in the tenant's patient list. Reused (matched-MRN) patients
            # stay — they had a prior life outside this import.
            if patient_was_created and patient is not None:
                try:
                    storage.soft_delete_patient(scope, patient.id)
                except Exception:
                    logger.exception(
                        "failed to soft-delete orphaned patient %s after "
                        "referral-create failure on row %s",
                        patient.id,
                        row.row_index,
                    )
            # row.status is "valid" here (filtered above); valid → error is legal.
            require_row_transition(row.status, "error")
            storage.update_csv_import_row(
                scope,
                import_id,
                row.id,
                status="error",
                validation_errors={**(row.validation_errors or {}), "commit": str(exc)},
            )
            failed.append((row.row_index, str(exc)))
            continue
        # row.status is "valid" here (filtered above); valid → committed is legal.
        require_row_transition(row.status, "committed")
        storage.update_csv_import_row(
            scope,
            import_id,
            row.id,
            status="committed",
            referral_id=referral.id,
        )
        committed += 1

    # Update the parent row. Even if every valid row failed mid-commit, we
    # still transition to ``committed`` — the per-row failures live on their
    # own rows + the error_report, and re-running is fine (committed rows
    # are filtered out by the status check above).
    storage.update_csv_import(
        scope,
        import_id,
        status="committed",
        error_report={
            **(csv_import.error_report or {}),
            "committed": committed,
            "skipped_error": skipped_error,
            "failed_at_commit": len(failed),
        },
    )
    audit_record(
        storage,
        action="import.commit",
        request=request,
        actor_user_id=user_id,
        scope_user_id=scope.audit_user_id,
        scope_organization_id=scope.organization_id,
        entity_type="csv_import",
        entity_id=str(import_id),
        metadata={
            "committed": committed,
            "skipped_error": skipped_error,
            "failed_at_commit": len(failed),
        },
    )
    dest = f"/imports/{import_id}/summary"
    return redirect_htmx(request, dest)


@router.get("/{import_id}/summary", response_class=HTMLResponse)
async def import_summary(
    request: Request,
    import_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    rows = storage.list_csv_import_rows(scope, import_id, limit=MAX_UPLOAD_ROWS)
    counts = {s: 0 for s in IMPORT_ROW_STATUS_VALUES}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return render(
        "import_summary.html",
        _ctx(
            request,
            current_user,
            storage,
            csv_import=csv_import,
            rows=rows,
            counts=counts,
        ),
    )


@router.get("/{import_id}/error-report.csv")
async def import_error_report(
    import_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
):
    """Downloadable CSV of errored rows (original cells + error messages).

    Column layout: the original CSV headers + a trailing ``_errors`` column
    with JSON-encoded error dict. Coordinators fix the file offline and
    re-upload — import IDs are new per upload, so no state carries over.
    """
    csv_import = storage.get_csv_import(scope, import_id)
    if csv_import is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    rows = storage.list_csv_import_rows(scope, import_id, status="error", limit=MAX_UPLOAD_ROWS)

    # Union of headers across the errored subset (some rows may have sparse
    # columns) keeps the CSV shape predictable.
    header_keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row.raw_json.keys():
            if k not in seen:
                header_keys.append(k)
                seen.add(k)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([*header_keys, "_row_index", "_errors"])
    for row in rows:
        values = [row.raw_json.get(h, "") for h in header_keys]
        # validation_errors is {field: message} — render as "field: msg; field: msg"
        err_str = "; ".join(
            f"{field}: {msg}" for field, msg in (row.validation_errors or {}).items()
        )
        writer.writerow([*values, row.row_index, err_str])
    filename = f"import_{import_id}_errors_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
