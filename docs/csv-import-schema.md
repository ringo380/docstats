# CSV Import Schema

Contract for files uploaded via `POST /imports`. The mapping UI at
`/imports/{id}/map` lets coordinators match arbitrary column headers
to these canonical target fields — the CSV's own column names can be
whatever the source system exports. The mapping is per-import and is
remembered for re-edit flows.

## Limits

- **Size**: 5 MB per file (enforced at upload).
- **Rows**: 2000 data rows per file (enforced at parse). Split larger files.
- **Encoding**: UTF-8 (BOM tolerated — Excel-saved CSVs work).
- **Header**: the first non-empty row is treated as the header.

## Target fields

The mapping UI exposes these targets. Unmapped fields are ignored during
validation and commit. The pipeline still produces a valid referral as long
as the **required** fields are mapped AND non-blank for every row.

### Patient

| Target key | Required | Notes |
|---|---|---|
| `patient_first_name` | yes | Non-blank. |
| `patient_last_name` | yes | Non-blank. |
| `patient_middle_name` | no | |
| `patient_dob` | no | ISO `YYYY-MM-DD` only; malformed values error the row. |
| `patient_mrn` | no | Free-text. In org mode, MRN exact-match against existing patients in the same scope reuses the row instead of creating a new one. |
| `patient_sex` | no | Free-text (`M` / `F` / `O` / `U` suggested). |
| `patient_phone` | no | |
| `patient_email` | no | |

### Clinical reason

| Target key | Required | Notes |
|---|---|---|
| `reason` | yes | Non-blank. What you're asking the specialist. |
| `clinical_question` | no | The specific question. |
| `urgency` | no | One of `routine`, `priority`, `urgent`, `stat`. Defaults to `routine` if blank/unmapped. |
| `requested_service` | no | |
| `diagnosis_primary_icd` | no | |
| `diagnosis_primary_text` | no | |

### Receiving specialist

| Target key | Required | Notes |
|---|---|---|
| `receiving_provider_npi` | no | 10 digits. Malformed values error the row. |
| `receiving_organization_name` | no | |
| `specialty_code` | no | NUCC taxonomy code. Drives the Phase 3 rules engine — when a known code is picked, that specialty's required-fields list is applied to every row in the batch. |
| `specialty_desc` | no | Free-text specialty name. |

### Referring side

| Target key | Required | Notes |
|---|---|---|
| `referring_provider_name` | no | |
| `referring_provider_npi` | no | 10 digits. |
| `referring_organization` | no | |

### Authorization

| Target key | Required | Notes |
|---|---|---|
| `authorization_number` | no | |
| `authorization_status` | no | |

## Header auto-match

The mapping UI pre-populates target pickers using a lowercase-slug alias
table (implemented at `routes/imports.py::_TARGET_ALIASES`). Headers like
`first_name`, `dob`, `MRN`, `reason`, `NPI`, `specialty` auto-match to
their canonical targets; anything else requires a manual pick. Review the
selections before saving — the auto-match is best-effort, not authoritative.

## Validation rules

Per row (see `domain/imports_validate.py`):

1. **Always-required targets** (`patient_first_name`, `patient_last_name`, `reason`) must be non-blank. Each missing field produces its own error.
2. **Urgency** must be one of `routine`, `priority`, `urgent`, `stat` (empty/unmapped = `routine`).
3. **NPI format** — any mapped NPI field (`receiving_provider_npi`, `referring_provider_npi`) must be exactly 10 digits when present.
4. **DOB format** — `patient_dob` must be ISO `YYYY-MM-DD` when present.
5. **Specialty-driven required fields** — when `specialty_code` resolves to a known rule (e.g. `207RC0000X` → Cardiology), that rule's `required_fields.fields` list applies to every row. A missing field on a cardiology row reports `required by Cardiology`.

## Commit behavior

`POST /imports/{id}/commit` writes valid rows into live `patients` +
`referrals` tables. Rows in `error` status are skipped; the summary page
reports `committed`, `skipped_error`, and `failed_at_commit` counts. A
downloadable `error-report.csv` is available from the summary page — fix
offline and re-upload as a fresh import.

Every created referral carries `external_source="bulk_csv"` so the
workspace + audit log can distinguish imported referrals from manually-
entered ones.
