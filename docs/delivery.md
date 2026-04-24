# Delivery subsystem — operator runbook

Phase 9 closed outbound delivery: fax (Documo), email (Resend), and the
share-token viewer.  Direct Trust is deferred until HISP onboarding.
This page is the one-stop reference for day-two operation.

## Architecture in one diagram

```
┌──────────────┐    create_delivery    ┌────────────┐     lifespan     ┌──────────────┐
│  /referrals  │──────────────────────▶│ deliveries │ ─── worker ─────▶│  Dispatcher  │
│   /{id}/send │                       │    (DB)    │◀──── sweep ──────│  (asyncio)   │
└──────────────┘                       └────────────┘                  └──────┬───────┘
                                              ▲                               │ channel.send()
                                              │ vendor webhook                 ▼
                                     ┌────────┴─────────┐              ┌──────────────┐
                                     │  /webhooks/…     │◀─────────────│  Documo /    │
                                     │  (HMAC verify)   │              │  Resend API  │
                                     └──────────────────┘              └──────────────┘
```

- `routes/delivery.py` enqueues `deliveries` rows.
- `delivery/dispatcher.py` sweeps every `DELIVERY_DISPATCHER_INTERVAL_SECONDS`.
- `delivery/channels/*.py` own vendor wire-level contracts.
- `webhook_verifiers/*.py` verify HMAC on inbound status callbacks.
- `routes/admin_deliveries.py` is the operator-facing console.

## Env vars

| Var | Purpose | Default |
|-----|---------|---------|
| `DELIVERY_DISPATCHER_INTERVAL_SECONDS` | Seconds between sweeper iterations | `10` |
| `DELIVERY_STUCK_SENDING_SECONDS` | How long a `sending` row sits before the sweeper retries it | `120` |
| `DELIVERY_MAX_RETRIES` | Retryable-failure cap before row flips to `failed` | `5` |
| `DOCSTATS_SKIP_DELIVERY_DISPATCHER` | Test-only: `1` disables the lifespan sweeper | unset |
| `RESEND_API_KEY` | Resend API token; absence disables the email channel | unset |
| `RESEND_WEBHOOK_SECRET` | `whsec_…` — Svix-format signing secret | unset |
| `RESEND_FROM_ADDRESS` | Optional override for the `From:` header | `referme.help <no-reply@referme.help>` |
| `SHARE_TOKEN_SECRET` | HMAC key for 2FA-answer hashing | unset (blocks share-token creation) |
| `SHARE_TOKEN_BASE_URL` | Public URL base for share links | `https://referme.help` |
| `DOCUMO_API_KEY` | Documo API key; absence disables the fax channel | unset |
| `DOCUMO_WEBHOOK_SECRET` | Documo HMAC-SHA256 signing secret | unset |
| `DOCUMO_BASE_URL` | Override for Documo API host (sandbox) | `https://api.documo.com` |
| `DOCUMO_COVER_PAGE_ENABLED` | `"true"` to layer Documo's cover on top of ours | `false` |

## Exponential backoff

Retryable failures requeue with a bumped `retry_count`.  The next pickup
respects this schedule (±15% jitter):

| retry_count | Min wait |
|-------------|----------|
| 1           | 10 s     |
| 2           | 30 s     |
| 3           | 2 min    |
| 4           | 10 min   |
| 5+          | 1 h      |

At `retry_count >= DELIVERY_MAX_RETRIES` (default 5) the row flips to
`failed` and emits a `delivery_failed` referral event.

## "My delivery is stuck" runbook

Start with the admin console:

1. **`/admin/deliveries/health`** — does the sweeper show `running: yes`?
   If not, the worker died.  Railway log search `"Delivery dispatcher"`
   should show a start line on the current deploy.
2. **Queue depth** — `queued` > 0 AND `oldest_queued_age_seconds` climbing?
   That's the symptom.  `sending` climbing usually means a channel hang.
3. **`/admin/deliveries?status=queued`** — inspect the row.  `retry_count`
   tells you how many attempts happened.  Click into `/admin/deliveries/{id}`
   for the attempt history — each retry shows `error_code` + vendor excerpt.

Common error codes:

| `error_code` | Root cause | Next step |
|--------------|-----------|-----------|
| `channel_disabled` | Env var missing when dispatcher picked up the row | Set the env var + redeploy.  Row is terminal `failed`; re-enqueue via re-send. |
| `rate_limited` | Vendor 429 | Transient; sweeper backs off.  Persistent → check vendor dashboard for plan limits. |
| `vendor_5xx` | Vendor outage | Transient; sweeper backs off.  If it persists past 1h cap → row flips to `failed`. |
| `vendor_4xx` | Recipient rejected or validation error | Fatal.  Fix the recipient / packet input and re-enqueue. |
| `timeout` | Network / vendor-side slow | Transient; sweeper retries. |
| `unexpected` | Bug in the channel impl | Check Railway logs for the stack trace.  File an issue. |

**Dead-lettered webhook** — `POST /webhooks/documo` and `POST /webhooks/resend`
record every inbound payload in `webhook_inbox` before parsing.  If a webhook
failed signature verification it'll appear in the inbox table with
`status='received'` but the corresponding delivery row will not have updated.
Grep the table for the vendor's timestamp range.

## Vendor onboarding checklist

**Adding a new vendor** (e.g. a second fax provider) means implementing the
`Channel` Protocol (``delivery/base.py``), registering a factory in
``delivery/registry.py``, writing a webhook verifier (if the vendor signs its
callbacks), and a route in ``routes/webhooks_vendor.py``.  See
``delivery/channels/fax.py`` and ``webhook_verifiers/documo.py`` as templates.

**Rotating a secret** —

1. Update the env var in Railway (the Variables UI triggers a rolling deploy).
2. Update the vendor's dashboard with the new secret.
3. Watch `/admin/deliveries/health` until the next sweep succeeds.
4. Any webhooks sent with the old secret during the rotation window are
   dead-lettered in `webhook_inbox`; the sweeper's stuck-sending guard will
   re-attempt the delivery after the threshold (default 2 min).

## Cancelling a delivery

- **Coordinator path** — `/referrals/{id}` → the delivery log card → Cancel.
  Records `delivery.cancel`.
- **Admin path** — `/admin/deliveries/{id}` → Cancel delivery button.
  Records `admin.delivery.cancel` with `previous_status` metadata so the
  audit trail distinguishes operator-initiated cancels from user-initiated ones.

Cancel is idempotent: cancelling an already-terminal row is a no-op (returns
False, emits no audit row).  Cancel does NOT flip `sent` rows — once the
packet has left our side, the recipient has it.

## Limits + gotchas

- `packet_artifact` is late-binding.  Attachments added between enqueue and
  dispatch will be included in the packet.  Real bytes-at-enqueue
  snapshotting lands in Phase 10.
- The dispatcher is single-process.  Multi-worker deploys on Railway will
  have each worker running its own sweeper, but the DB row's `sending`
  transition is race-free: the sweeper flips queued→sending in one SQL
  UPDATE before the channel call.  A second worker that also sees the row
  as `queued` will race-lose when it tries to flip — TODO in 9.F is a
  `FOR UPDATE SKIP LOCKED`-style pattern once Postgres direct-SQL lands.
- Health snapshot (`/admin/deliveries/health`) is process-local — on N
  workers you see only the one that served the request.  The DB-backed
  queue depth counts are authoritative across workers.
