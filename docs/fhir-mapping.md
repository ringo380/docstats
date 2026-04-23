# FHIR mapping reference

**Source of truth**: [`src/docstats/exports/fhir.py`](../src/docstats/exports/fhir.py). This document lags by review cadence — when the two disagree, the code wins.

docstats emits a **FHIR-ish** R4 Bundle from `build_referral_bundle()`. "FHIR-ish" means the output is shaped like HL7 FHIR R4 and passes a smell test for developers familiar with the format, but is **not** guaranteed to pass a strict validator. Phases 12+ (SMART-on-FHIR EHR integrations) will harden the mapping against real validators. For now the export gives downstream systems a machine-readable referral packet.

## Bundle shape

- `resourceType`: `Bundle`
- `type`: `document` — read endpoints emit document bundles; `transaction` is reserved for a future write endpoint where each entry carries `request.method` + `request.url`. Do not change the read bundle to `transaction` just because an earlier draft of the master plan said so.
- `timestamp`: ISO-8601 UTC timestamp of the export
- `entry[]`: in order — Patient, ServiceRequest, [Practitioner], [Organization], [Condition], [Appointment…], [Communication…], [MedicationStatement…], [AllergyIntolerance…], [DocumentReference…], [Endpoint…]. Square brackets denote conditional / plural entries.

## Resource-by-resource mapping

### Patient

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `patient-{id}` | stable within a single bundle; not a global identifier |
| `name[0].family` | `patient.last_name` | |
| `name[0].given` | `[patient.first_name, patient.middle_name?]` | middle omitted when null |
| `birthDate` | `patient.date_of_birth` | ISO `YYYY-MM-DD` |
| `gender` | `patient.sex` | M→male, F→female, O→other, U→unknown; unknown mappings drop the field |
| `identifier[].type.coding[]` | hl7 v2-0203 `MR` | when `patient.mrn` is set |
| `telecom[]` | `patient.phone` / `patient.email` | |
| `communication[0].language.text` | `patient.preferred_language` | free text; no code system |
| `address[0]` | `patient.address_*` fields | omitted entirely when no address fields populated |

### ServiceRequest

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `servicerequest-{referral.id}` | |
| `status` | `_STATUS_MAP[referral.status]` | see §Status/priority vocabs below |
| `intent` | `"order"` | constant — every docstats referral is an order |
| `priority` | `_PRIORITY_MAP[referral.urgency]` | |
| `subject` | `Patient/patient-{id}` | |
| `authoredOn` | `referral.created_at` | ISO-8601 |
| `code.text` | `referral.requested_service` or `referral.specialty_desc` | first non-blank wins |
| `category[].coding[]` | NUCC taxonomy `referral.specialty_code` | system = `http://nucc.org/provider-taxonomy` |
| `reasonCode[].text` | `referral.reason` | |
| `reasonReference[]` | `Condition/condition-{id}-primary` | when a primary diagnosis is present |
| `requester` | `Practitioner/practitioner-{id}-referring` | when referring NPI or name present |
| `performer[]` | `Organization/organization-{id}-receiving` + `{identifier: NPI}` | one or both, depending on what's populated |
| `note[].text` | `referral.clinical_question` | |
| `identifier[]` | `{type.text: "Authorization number", value: referral.authorization_number}` | when auth number set |

### Practitioner (referring)

Emitted only when `referring_provider_npi` OR `referring_provider_name` is populated.

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `practitioner-{referral.id}-referring` | |
| `identifier[]` | US-NPI for `referring_provider_npi` | system = `http://hl7.org/fhir/sid/us-npi` |
| `name[0].text` | `referring_provider_name` | free text |

### Organization (receiving)

Emitted only when `receiving_organization_name` is populated.

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `organization-{referral.id}-receiving` | |
| `name` | `receiving_organization_name` | |
| `identifier[]` | US-NPI for `receiving_provider_npi` | when set |

### Condition (primary diagnosis)

Emitted only when `diagnosis_primary_icd` OR `diagnosis_primary_text` is populated. Secondary diagnoses from the `referral_diagnoses` sub-table are not currently emitted; the headline fields on the Referral are authoritative.

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `condition-{referral.id}-primary` | |
| `subject` | `Patient/patient-{id}` | |
| `code.coding[]` | ICD-10 `diagnosis_primary_icd` | system = `http://hl7.org/fhir/sid/icd-10` |
| `code.text` | `diagnosis_primary_text` (or ICD code) | |

### Appointment (Phase 8.A)

Emitted per `ReferralResponse` that has `appointment_date` set. Multiple responses → multiple Appointments.

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `appointment-{referral.id}-{response.id}` | |
| `status` | `fulfilled` when `response.consult_completed`; `booked` otherwise | closed FHIR vocab — see §Status/priority vocabs |
| `start` | `response.appointment_date` at midnight UTC | ISO-8601; FHIR requires datetime, not date |
| `participant[].actor` | `Patient/patient-{id}` (required="required", status="accepted") | |
| `participant[].actor` | `Practitioner/practitioner-{id}-referring` | when referring side known — status="tentative" |
| `meta.tag[]` | `{system: "https://docstats.app/fhir/received-via", code: response.received_via}` | non-standard extension; carries the channel (fax/portal/email/phone/manual/api) |

### Communication (Phase 8.A)

Emitted per `ReferralResponse` where `consult_completed=True` AND `recommendations_text` is non-blank. Represents the received consult note. `CommunicationRequest` is **not** the right resource — that's for "please send"; this is "we received".

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `communication-{referral.id}-{response.id}` | |
| `status` | `completed` | closed FHIR vocab — see §Status/priority vocabs |
| `sent` | `response.created_at` | ISO-8601 |
| `subject` | `Patient/patient-{id}` | |
| `about[]` | `ServiceRequest/servicerequest-{referral.id}` | |
| `payload[0].contentString` | `response.recommendations_text` | |
| `meta.tag[]` | `{system: "https://docstats.app/fhir/received-via", code: response.received_via}` | same as Appointment |

`attached_consult_note_ref` is reserved for Phase 10 file storage — not currently mapped.

### MedicationStatement

One per `referral_medications` row.

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `medicationstatement-{referral.id}-{med.id}` | |
| `status` | `"active"` | constant |
| `subject` | `Patient/patient-{id}` | |
| `medicationCodeableConcept.text` | `med.name` | free text; no RxNorm coding yet |
| `dosage[0].text` | `med.dose` + ` ` + `med.frequency` | |
| `dosage[0].route.text` | `med.route` | |

### AllergyIntolerance

One per `referral_allergies` row.

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `allergyintolerance-{referral.id}-{allergy.id}` | |
| `patient` | `Patient/patient-{id}` | |
| `code.text` | `allergy.substance` | free text |
| `reaction[0].manifestation[0].text` | `allergy.reaction` | |
| `reaction[0].severity` | `allergy.severity` | free text; not constrained to FHIR vocab |

### DocumentReference

One per `referral_attachments` row.

| FHIR field | docstats source | Notes |
|---|---|---|
| `id` | `documentreference-{referral.id}-{attachment.id}` | |
| `status` | `preliminary` when `checklist_only`, else `current` | |
| `subject` | `Patient/patient-{id}` | |
| `type.text` | `attachment.kind` | |
| `description` | `attachment.label` | |
| `date` | `attachment.date_of_service` | when set |

### Endpoint (Phase 8.A)

One per Direct Trust address returned by NPPES for `receiving_provider_npi`. Fetched fresh per export — not persisted on the referral. Emitted only when an endpoint has `endpointType == "Direct"`.

| FHIR field | docstats source (NPPES `Endpoint`) | Notes |
|---|---|---|
| `id` | `endpoint-{referral.id}-{idx}` | stable within the bundle only |
| `status` | `"active"` | constant — NPPES doesn't convey lifecycle state |
| `connectionType.system` | `http://terminology.hl7.org/CodeSystem/endpoint-connection-type` | |
| `connectionType.code` | `"direct-project"` | |
| `payloadType[0].text` | `endpoint.contentTypeDescription` | free text; NPPES uses descriptive strings, not LOINC |
| `address` | `endpoint.endpoint` | the actual Direct address, e.g. `provider@direct.hospital.com` |
| `name` | `endpoint.affiliationName` or `endpoint.useDescription` | first non-blank wins |

When the NPPES lookup fails (timeout, 5xx, no NPI), **no Endpoint resources are emitted** — the bundle still ships, just without Direct metadata. A warning is logged. Export availability trumps metadata completeness.

## Status / priority vocabularies

### `_STATUS_MAP` (ServiceRequest.status)

FHIR ServiceRequest.status is a closed vocabulary: `draft | active | on-hold | revoked | completed | entered-in-error | unknown`. The regression test `test_build_referral_bundle_status_map` asserts every mapped value is one of these.

| docstats status | FHIR ServiceRequest.status | Reasoning |
|---|---|---|
| `draft` | `draft` | direct |
| `ready` | `active` | order is queued for dispatch |
| `sent` | `active` | order is in flight |
| `awaiting_records` | `on-hold` | blocked on input |
| `awaiting_auth` | `on-hold` | blocked on payer |
| `scheduled` | `active` | no FHIR equivalent; still in progress |
| `rejected` | `revoked` | intentionally created then declined — not `entered-in-error` |
| `completed` | `completed` | direct |
| `cancelled` | `revoked` | direct |

### `_PRIORITY_MAP` (ServiceRequest.priority)

FHIR priority: `routine | urgent | asap | stat`. docstats has an extra `priority` tier between routine and urgent; we collapse it to FHIR `urgent` (closer workflow meaning than `asap`).

| docstats urgency | FHIR priority |
|---|---|
| `routine` | `routine` |
| `priority` | `urgent` |
| `urgent` | `urgent` |
| `stat` | `stat` |

### `_APPOINTMENT_STATUS_MAP` (Appointment.status)

FHIR Appointment.status: `proposed | pending | booked | arrived | fulfilled | cancelled | noshow | entered-in-error | checked-in | waitlist`. The regression test asserts only `booked` and `fulfilled` are emitted.

| ReferralResponse state | FHIR Appointment.status |
|---|---|
| `appointment_date` set, `consult_completed=False` | `booked` |
| `appointment_date` set, `consult_completed=True` | `fulfilled` |

### `_COMMUNICATION_STATUS_MAP` (Communication.status)

FHIR Communication.status: `preparation | in-progress | not-done | on-hold | stopped | completed | entered-in-error | unknown`. docstats only emits Communication when the response represents a completed consult, so we only emit `completed`.

| ReferralResponse state | FHIR Communication.status |
|---|---|
| `consult_completed=True` AND `recommendations_text` non-blank | `completed` |

## Known limitations

- **No Encounter**: the master plan mentions `encounter` on ServiceRequest. docstats doesn't model encounters; the field is omitted. Phase 12 (SMART-on-FHIR) will need this for EHR round-trips.
- **No CPT coding**: `ServiceRequest.code` is free text only. NUCC taxonomy appears on `category`, not `code`.
- **Secondary diagnoses not emitted**: only the primary is mapped to Condition. `referral_diagnoses` sub-table rows beyond the primary are ignored.
- **Free-text severity on AllergyIntolerance**: not constrained to FHIR vocab (`mild|moderate|severe`).
- **`received_via` via `meta.tag`**: non-standard — real FHIR profiles would use an extension URL. Good enough for the "FHIR-ish" bar.
- **No validation against FHIR JSON Schema**: Phases 12+ will add strict validation in CI.
- **Endpoint resources fetched fresh per export**: NPPES is the source of truth; we don't persist Direct addresses, so a provider removing their Direct endpoint propagates to the next export.
