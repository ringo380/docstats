# Attachment file storage — Phase 10.A

Attachment uploads ship **code-complete, feature-flagged**.  The flag is
`ATTACHMENT_UPLOAD_ENABLED`; leave it unset in production until the
Supabase BAA is signed (Team tier or higher).  With the flag off, every
route under `/attachments` and `POST /referrals/{id}/attachments` returns
404, and the referral detail page hides the upload form.

## Architecture

```
┌──────────────┐  POST multipart   ┌────────────────┐  put(bytes)   ┌──────────────┐
│ Referral     │──────────────────▶│ /referrals/    │──────────────▶│  Supabase    │
│ detail form  │                   │ {id}/attach..  │               │  Storage     │
└──────────────┘                   └────────────────┘               │  (private    │
                                          │                         │   bucket)    │
                                          │ storage_ref             │              │
                                          ▼                         └──────────────┘
                                   ┌────────────────┐                      ▲
                                   │ referral_      │                      │ signed URL
                                   │ attachments    │                      │ (15 min)
                                   │ (DB row)       │──── GET /attach.. ───┘
                                   └────────────────┘
```

- `routes/attachments.py` is the entry point.
- `storage_files/base.py` defines the `StorageFileBackend` Protocol.
- `storage_files/supabase_store.py` is the production adapter.
- `storage_files/memory_store.py` is the in-memory adapter (tests + the
  dev fallback when Supabase creds are absent).
- `storage_files/mime.py` sniffs MIME from magic bytes — **we never trust
  the client's `Content-Type` header.**
- `storage_files/factory.py::get_file_backend()` is the FastAPI dep-injection
  target; tests override via `app.dependency_overrides`.

## Env vars

| Var | Purpose | Default |
|-----|---------|---------|
| `ATTACHMENT_UPLOAD_ENABLED` | `1` / `true` / `yes` to enable the routes | unset (disabled) |
| `ATTACHMENT_STORAGE_BACKEND` | `supabase` or `memory`; explicit override | auto-detect from creds |
| `SUPABASE_URL` | Supabase project URL (already in the stack for Postgres) | unset |
| `SUPABASE_SERVICE_KEY` | Service role key for the same project | unset |
| `SUPABASE_STORAGE_BUCKET` | Name of the private attachments bucket | `attachments` |

When `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are both set (they already
are in production), the factory picks `SupabaseFileBackend` unless
`ATTACHMENT_STORAGE_BACKEND=memory` overrides.  The in-memory backend is
a process-local singleton, so restart-of-the-worker wipes uploads —
suitable only for tests and local dev.

## Bucket setup (one-time, before flipping the flag)

1. In the Supabase dashboard → **Storage** → **New bucket**:
   - Name: `attachments` (or whatever `SUPABASE_STORAGE_BUCKET` will be set to).
   - **Public**: **off** (absolutely).
   - Allowed MIME types: leave empty (we enforce at the app layer against
     a narrow allow-list).
   - File size limit: 50 MB (matches `MAX_UPLOAD_BYTES`).
2. Under **Policies** leave default (service-role-only access).  The
   backend uses `SUPABASE_SERVICE_KEY` which bypasses RLS; no policies
   are needed unless we later expose the bucket to `anon` keys (we don't).
3. Sign the BAA via Supabase → **Account** → **Security** → **BAA**.
   BAA requires Team plan or higher.
4. Flip `ATTACHMENT_UPLOAD_ENABLED=1` in Railway.

## Security posture

- **PHI content only in the object bytes.**  Nothing PHI-sensitive
  appears in logs, object paths, or signed URLs.  Paths are
  `{scope_prefix}/{referral_id}/{attachment_id}.{ext}` — even a leaked
  signed URL reveals only a referral id the recipient was already
  authorized to see.
- **MIME allow-list, sniffed not declared** — PDF, JPEG, PNG, TIFF, DOCX.
  Everything else bounces at 415 before the bytes touch the bucket.
- **Signed URLs only** — 15 minute expiry by default.  The bucket is
  never configured public.
- **Audit rows** — every upload emits `attachment.create`; every download
  emits `attachment.view`; every delete emits `attachment.delete`.
  Metadata includes `referral_id`, `kind`, `mime_type`, `size_bytes`, and
  (10.B onward) the `scanner` that cleared the upload.  Rejected uploads
  emit `attachment.scan_rejected` with `threats` and `scanner` fields;
  scanner outages with `VIRUS_SCAN_REQUIRED=1` emit `attachment.scan_unavailable`.
- **Scope isolation** — attachments inherit their scope from the parent
  referral via `StorageBase.get_referral_attachment(scope, id)`.  Cross-
  tenant downloads return 404, not 403 (prevents existence leaks).
- **Encryption at rest** — Supabase provides AES-256 on all objects.
  Envelope encryption for high-sensitivity fields lands in Phase 15.

## Virus scanning (Phase 10.B)

Every upload runs through a `VirusScanner` **before** the bytes leave
our process.  Cloudmersive is the default vendor (REST, BAA available at
Enterprise tier); a no-op scanner ships for local dev so developers
aren't blocked by a missing API key.

| Env var | Purpose | Default |
|---|---|---|
| `VIRUS_SCANNER_BACKEND` | `cloudmersive` / `noop` / `none` / unset for auto | auto (Cloudmersive if key set, else no-op) |
| `CLOUDMERSIVE_API_KEY` | Cloudmersive REST API key | unset |
| `CLOUDMERSIVE_BASE_URL` | Override host for sandbox envs | `https://api.cloudmersive.com` |
| `CLOUDMERSIVE_TIMEOUT` | Per-scan timeout in seconds (clamped [1, 300]) | `60` |
| `VIRUS_SCAN_REQUIRED` | **Fail-closed** when `1`/`true`: scanner outage → 502.  Unset/`0` → log and proceed (dev only) | unset (permissive) |

### Policy matrix

| Scanner returns | `VIRUS_SCAN_REQUIRED` | Outcome |
|---|---|---|
| Clean verdict | — | Upload proceeds; audit records `scanner=<name>` |
| Infected verdict | — | **422**; audit `attachment.scan_rejected`; no DB row, no bucket write |
| `ScannerUnavailable` | `1` | **502**; audit `attachment.scan_unavailable`; no upload |
| `ScannerUnavailable` | unset/`0` | Log warning; upload proceeds with `scanner=none` |
| Scanner = `None` | `1` | **502** (misconfiguration); no upload |
| Scanner = `None` | unset/`0` | Upload proceeds with `scanner=none` (dev only) |

### Cloudmersive wire contract

- `POST https://api.cloudmersive.com/virus/scan/file`
- Header: `Apikey: <key>`
- Multipart field: `inputFile` (camelCase — **not** `input_file`, which is
  the Python SDK symbol)
- Response JSON: `{"CleanResult": bool, "FoundViruses": [{"FileName": "...",
  "VirusName": "..."}, ...]}`.  `FoundViruses` may be absent on a clean
  result — we treat missing as empty.

Sources: [Cloudmersive Virus Scan docs](https://api.cloudmersive.com/docs/virus.asp).

### Production rollout checklist

1. Sign the Cloudmersive BAA (Enterprise plan).
2. Mint an API key in the Cloudmersive dashboard.
3. Set Railway vars: `CLOUDMERSIVE_API_KEY`, `VIRUS_SCAN_REQUIRED=1`.
4. Watch `/admin/audit?action=attachment.scan_unavailable` for any
   transient scanner outages after deploy.
5. Flip `ATTACHMENT_UPLOAD_ENABLED=1` only after both the Supabase BAA
   and the Cloudmersive BAA are in place.

## Retention (Phase 10.C)

Attachments are **hard-deleted** after their tenant's retention window
expires.  The sweep runs as a lifespan-managed asyncio task alongside
the Phase 9 delivery dispatcher.

| Env var | Purpose | Default |
|---|---|---|
| `ATTACHMENT_RETENTION_INTERVAL_SECONDS` | Seconds between retention sweeps (clamped [60, 604800]) | `86400` (24h) |
| `DOCSTATS_SKIP_ATTACHMENT_RETENTION` | Test-only: `1` disables the lifespan sweep | unset |

### Per-tenant retention policy

- **Orgs** — `organizations.attachment_retention_days` (30 – 10950,
  default 2555 ≈ 7 years).  Admins edit this on the **Org settings**
  page in the admin console.
- **Solo users** — platform-wide default
  (`DEFAULT_ATTACHMENT_RETENTION_DAYS` = 2555 days).  No per-user
  override in 10.C; the feature lands if/when solo mode grows.
- **Retention floor** — 30 days is below the delivery dispatcher's
  exponential-backoff cap (Phase 9.E), so we never purge a document
  before its initial delivery retries exhaust.

### Sweep behavior

1. Enumerate every live org plus every solo user that owns bucket-backed
   attachments.
2. Per tenant: compute `cutoff = now - retention_days`; pull up to
   `DEFAULT_BATCH_SIZE` (500) expired rows; iterate.
3. For each attachment: delete the bucket object (**best-effort** —
   failures leave orphan bytes for the next sweep), hard-delete the DB
   row, emit `attachment.purged` audit.
4. Bounded: up to `DEFAULT_MAX_BATCHES_PER_TENANT` (10) iterations per
   tenant per sweep, so one giant tenant can't starve peers.

### Failure modes

- **Bucket delete fails** — DB row is still removed; bucket bytes become
  the next sweep's problem.  Audit row records the policy decision.
- **One tenant's query fails** — the sweep logs the error and moves on
  to the next tenant.  No tenant blocks another.
- **`list_attachments_expired` called without scope** — raises
  `ValueError`.  Callers must specify exactly one of
  `scope_organization_id` / `scope_user_id`; guards against the broken
  caller that would accidentally purge everyone's data.

### Audit trail

- `attachment.purged` with `{referral_id, kind, storage_ref, reason: "retention"}`.
  No `actor_user_id` (background job).  Visible in the admin audit log
  filter datalist.

## Packet embedding (Phase 10.D)

PDF attachments can be spliced into an outbound packet so the receiving
specialist gets the referral summary + real lab/imaging/consult-note
PDFs in a single document.  Non-PDF attachments (images, DOCX) are
**not** inlined — pypdf can't concatenate non-PDFs without conversion,
so they remain in the `attachments` checklist entry.

### Include-token contract

- **`attachment_pdfs`** — pseudo-artifact; accepted only in
  `?include=` on the packet route, never as `?artifact=` on its own.
  Splices every PDF-backed attachment on the referral at the spot it
  appears in the include list.
- Ordering: put `fax_cover,summary,attachments,attachment_pdfs` to end
  on the actual attachments; put `fax_cover,summary,attachment_pdfs,
  attachments` to end on the checklist (unusual but possible).
- **Missing blobs** (bucket 404 on an attachment whose DB row says
  `storage_ref` is set) — logged and skipped; the packet still renders
  without that piece, and the checklist entry tells the recipient which
  document is missing.

### Dispatcher integration

The delivery dispatcher consults `delivery.packet_artifact["include"]`
at send time.  `build_delivery_packet()` rebuilds the caller's scope
from the delivery's denormalized `scope_user_id` / `scope_organization_id`
columns, pulls any attachment PDFs via the file backend, renders each
artifact via WeasyPrint, and concatenates with pypdf.  The route layer
(`/referrals/{id}/export.pdf?artifact=packet`) follows the same code
path so downloads and fax/email sends produce byte-identical packets.

### UI

The export preview (`/referrals/{id}/export`) surfaces an
"Attachment PDFs (embedded)" checkbox **only when at least one
bucket-backed attachment exists on the referral**.  Checklist-only
rows don't trigger the toggle (they have no bytes to embed).

## What's NOT in 10.D

- **Image → PDF conversion** — JPEG/PNG/TIFF attachments stay in the
  checklist.  A conversion pass lands when a real coordinator request
  shows up.
- **DOCX → PDF conversion** — same rationale.  Would need LibreOffice
  as a sidecar process; out of scope for the MVP.
- **Packet embedding** — Phase 10.D teaches `exports/pdf.py::render_packet`
  to pull the real bytes and embed them into the outbound packet.
  Today the packet includes only the checklist row.
- **S3 adapter** — a `S3FileBackend` swap drops in behind the Protocol
  whenever the product decides Supabase isn't the right home.

## "My upload failed" runbook

1. **415 Unsupported MIME** — The sniffer didn't recognize the bytes.
   Typical cause: the user renamed a `.jpg` to `.pdf`.  The allow-list
   is narrow on purpose; if a legitimate format keeps bouncing, widen
   `ALLOWED_MIME_TYPES` in `storage_files/base.py` and add a sniff
   branch to `storage_files/mime.py`.
2. **422 Label is required / unknown kind** — Client-side validation
   should prevent these; if they surface, the request bypassed the form.
3. **413 File exceeds 50 MB upload cap** — We reject on
   `Content-Length` before spooling.  If the client chunks without a
   header, we re-check after reading and still 413.
4. **502 Upload failed; please retry** — The bucket call raised.  Check
   Railway logs for `Supabase upload failed for {path}`.  Common causes:
   - `SUPABASE_SERVICE_KEY` rotated and the env var is stale.
   - Bucket doesn't exist yet (one-time setup skipped).
   - Supabase incident.  The placeholder DB row is rolled back, so the
     user sees no phantom attachment.
5. **404 on download of a row that exists** — `storage_ref` is NULL,
   meaning the row is a pre-10.A "checklist only" marker.  The download
   route gates on `storage_ref` being set.
