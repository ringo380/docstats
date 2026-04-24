# Fax delivery (Documo) — activation runbook

Phase 9.C ships the fax channel **code-complete but feature-flagged**.  Live
sends require a signed Documo Business Associate Agreement (BAA) on the
Professional tier.  Until the BAA is countersigned the channel stays off —
the Send form hides fax automatically when `DOCUMO_API_KEY` is absent.

## Activation checklist

1. **BAA signed** with Documo (Professional or higher tier).  HIPAA compliance
   is Documo-side for the PHI in the fax PDF; our side handles audit + cover.
2. **API key** issued via the Documo web app → *Settings → API*.  Copy the
   plaintext key (Documo uses the raw key as the Basic auth value — **not**
   `user:password` base64).
3. **Webhook endpoint** added in Documo → *Settings → Webhooks* pointing at
   `https://referme.help/webhooks/documo`.  Choose HMAC-SHA256 signing;
   copy the signing secret.
4. **Set Railway env vars** (production):
   ```
   DOCUMO_API_KEY=<the API key>
   DOCUMO_WEBHOOK_SECRET=<the webhook signing secret>
   ```
   Optional:
   ```
   DOCUMO_BASE_URL=https://api.documo.com        # override for sandbox
   DOCUMO_COVER_PAGE_ENABLED=false               # we already render our own cover
   ```
5. **Verify**: the referral detail page's Send dropdown will show *Fax*
   within one Railway deploy.  Submit a test fax to an internal test line;
   watch `/admin/deliveries` (Phase 9.E) for the `queued → sending → sent →
   delivered` transitions as Documo pushes webhooks back.

## Sandbox / staging

Documo's sandbox uses a separate API host (see Documo dashboard).  Set
`DOCUMO_BASE_URL=https://sandbox.api.documo.com` and use the sandbox key
to run end-to-end tests without consuming paid fax credits.  Sandbox
webhooks fire with the same signing scheme as production.

## Troubleshooting

**Channel disabled in the Send dropdown** — the `DOCUMO_API_KEY` env var is
absent or empty.  The registry factory raises `ChannelDisabledError` and
`enabled_channels()` filters fax out.  Set the var and redeploy.

**Webhook returns 400 "Invalid signature"** — `DOCUMO_WEBHOOK_SECRET` does
not match the value in the Documo dashboard.  Rotate via the dashboard
and update Railway.  Payloads from the invalid window are dropped; the
sweeper's stuck-sending guard will re-queue the delivery after ~2 min.

**Delivery rows stuck in `sending`** — Documo dropped the webhook.  The
sweeper flips them back to `queued` after `DELIVERY_STUCK_SENDING_SECONDS`
(default 120 s) and the channel resends (idempotent via
`Idempotency-Key`).  Persistent stuck rows indicate a firewall / DNS issue
with the webhook route; curl the endpoint from a Documo-connected shell.

**Recipient rejected at 422** — fax numbers must be US/Canada (10 digits
bare or 11 digits with leading `1`).  The `validate_fax_number` route
guard normalizes `(555) 555-5555` and friends to `+15555555555`.

## Compliance notes

- PHI in the faxed PDF — rendered by `exports/pdf.py` using the
  `fax_cover` + `summary` artifacts from Phase 5.C.  Cover pages embed
  sender org name + recipient name + subject (no PHI in the cover body).
- The multipart `subject` / `notes` fields submitted to Documo are
  explicitly PHI-free and capped to 120 / 500 chars respectively.
- Every outbound fax writes an `audit_events` row with
  `action="delivery.create"` and a `delivery_attempts` row on vendor
  call.  Webhook-driven status updates do **not** write audit rows
  (they're derived from vendor state — the original send is the
  auditable event).
- Documo-side retention is governed by the Documo BAA.  Our side keeps
  the delivery row + attempts indefinitely; cancellation soft-deletes
  via `cancelled_at` (Phase 9.A).
