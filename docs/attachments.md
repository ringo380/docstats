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
  Metadata includes `referral_id`, `kind`, `mime_type`, `size_bytes`.
- **Scope isolation** — attachments inherit their scope from the parent
  referral via `StorageBase.get_referral_attachment(scope, id)`.  Cross-
  tenant downloads return 404, not 403 (prevents existence leaks).
- **Encryption at rest** — Supabase provides AES-256 on all objects.
  Envelope encryption for high-sensitivity fields lands in Phase 15.

## What's NOT in 10.A

- **Virus scanning** — Phase 10.B (Cloudmersive with BAA is the default
  vendor choice; ClamAV sidecar is an alternative if RAM budget allows).
  Until 10.B, uploaded files are trusted to be non-malicious — do NOT
  enable the flag in a production org without that safety net.
- **Retention** — Phase 10.C adds a nightly purge job governed by an
  org-configurable retention policy (default 7 years).
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
