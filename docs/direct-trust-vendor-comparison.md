# Direct Trust HISP vendor comparison

**Last updated**: 2026-05-02
**Status**: Pre-decision research for Phase 9.D
**Pilot scale**: ≤500 messages/month

This document compares HISP (Health Information Service Provider) vendors for the deferred Phase 9.D Direct Trust channel. Direct Trust messaging is the only DirectTrust-Accredited path for sending PHI by "secure email"; every HISP is a regulated intermediary that signs/encrypts via S/MIME and routes between accredited domains.

## Decision criteria (weighted for our pilot)

| Criterion | Why it matters | Weight |
|---|---|---|
| **REST API** (vs SMTP/S-MIME only) | We're an async FastAPI service; SMTP integration adds days of MIME-handling code | High |
| **Sandbox / test address** | Lets us land 9.D code with end-to-end CI, not "trust me" | High |
| **BAA at standard tier** | Enterprise-only BAA = gate before we can send any real PHI | High |
| **Status webhooks** | Phase 9 dispatcher relies on webhook callbacks for `delivered`/`failed` updates | High |
| **Pricing transparency** | Pilot budget; per-message preferred over per-seat | Medium |
| **DirectTrust accreditation** | Required for cross-HISP interop with hospital systems | Medium |
| **Onboarding speed** | Weeks vs months to first sandbox send | Medium |
| **Provisioning a custom FQDN** (e.g. `direct.referme.help`) | Brand identity on outbound; cleaner than vendor-subdomain | Low (nice-to-have) |

## Vendor matrix

> Public-source claims only. Numbers in italics are approximate / inferred. **Confirm everything in the actual vendor call.**

### 1. DataMotion Direct

- **Direct API**: REST. Public docs at `https://docs.datamotion.com/`.
- **Auth**: API key + per-request HMAC.
- **Webhooks**: Yes — delivery status callbacks via configurable URL.
- **Sandbox**: Yes — test environment provisioned during onboarding.
- **BAA**: Standard at Pro tier; confirm if available at pilot pricing.
- **DirectTrust accredited**: Yes.
- **Pricing** _(public estimate)_: per-message, ~$0.05–0.15 range; minimum monthly commit varies.
- **Onboarding**: 2–4 weeks typical (BAA + identity verification + DNS).
- **Custom FQDN**: Supported.
- **Strengths**: Best public REST docs of the bunch; established player; clean status webhook semantics.
- **Risks**: Pricing not on the public site → negotiation slows the clock.
- **Notes**: Original Phase 9 plan recommendation. Default candidate to beat.

### 2. Updox

- **Direct API**: Hybrid — REST for some operations, SMTP relay for Direct messaging in some plans. Direct is part of a broader patient-engagement platform.
- **Auth**: OAuth2 + API token.
- **Webhooks**: Yes for the broader platform; Direct-specific status delivery less documented publicly.
- **Sandbox**: Yes (via partner program); requires partner agreement first.
- **BAA**: Yes; tier-dependent.
- **DirectTrust accredited**: Yes (operates as a HISP).
- **Pricing**: Bundled into Updox platform subscription; standalone Direct pricing not advertised.
- **Onboarding**: 3–6 weeks (more sales-cycle gates than DataMotion).
- **Custom FQDN**: Supported.
- **Strengths**: Strong specialty/clinic adoption; if a future feature needs broader patient-engagement primitives, single-vendor leverage.
- **Risks**: Bundling pulls us into platform features we don't need. Standalone Direct may be a poor fit for our use case.

### 3. MaxMD / Kno2

- **Direct API**: REST (Kno2 acquired MaxMD). Modern API surface.
- **Auth**: OAuth2.
- **Webhooks**: Yes.
- **Sandbox**: Yes.
- **BAA**: Standard.
- **DirectTrust accredited**: Yes (one of the original HISPs).
- **Pricing**: Per-message + per-domain fee. Mid-range.
- **Onboarding**: 2–4 weeks.
- **Custom FQDN**: Supported.
- **Strengths**: One of the most widely-used HISPs; high probability that receiving providers are already on Kno2 (improves trust-bundle resolution success).
- **Risks**: Larger vendor → slower contract negotiation for pilot-sized accounts.

### 4. Paubox

- **Direct API**: REST. Best-known for "encrypted email that doesn't require recipient login" — a different product from Direct Trust messaging proper.
- **Note**: Paubox's headline product is HIPAA-encrypted email (TLS + AES at rest), NOT DirectTrust-accredited Direct messaging in the strictest sense. They DO offer a Direct messaging add-on, but it's a smaller part of the business.
- **Auth**: API key.
- **Webhooks**: Yes.
- **Sandbox**: Yes for the encrypted-email product; Direct sandbox availability less clear.
- **BAA**: Standard.
- **DirectTrust accredited**: Confirm — their Direct add-on routes through a partner HISP in some configurations.
- **Pricing**: Transparent — published online, ~$29/user/mo for encrypted email. Direct add-on pricing not public.
- **Strengths**: Easiest signup; transparent pricing; great for clinic-to-patient encrypted email.
- **Risks**: May not be a true HISP for our use case. If we need cross-HISP interop with hospital EHRs, Paubox may route through a partner and add latency / failure modes. **Treat as backup, not primary.**

### 5. iCoreExchange (iCoreConnect)

- **Direct API**: REST via the iCoreConnect platform.
- **Auth**: API key.
- **Webhooks**: Yes.
- **Sandbox**: Yes.
- **BAA**: Standard.
- **DirectTrust accredited**: Yes.
- **Pricing**: Tiered; small-volume tier exists. Confirm in the call.
- **Onboarding**: 2–4 weeks.
- **Custom FQDN**: Supported.
- **Strengths**: Smaller vendor → likely faster contract turnaround for a pilot account; specialty/dental clinic focus.
- **Risks**: Smaller market footprint → fewer receiving providers may resolve cleanly via the Direct trust bundle. Documentation thinner than DataMotion's public surface.

## Tiered recommendation

| Position | Vendor | Why |
|---|---|---|
| **Primary** | DataMotion Direct | Best public REST docs, established player, clean webhook semantics, original plan recommendation. Negotiate pilot pricing. |
| **Strong alternate** | MaxMD / Kno2 | Highest probability that receiving providers are already on the network → fewer trust-bundle resolution failures. Worth getting a quote in parallel. |
| **Backup** | iCoreExchange | If primary + alternate sales cycles stall, smaller vendor likely closes faster. |
| **Probably not** | Updox | Bundling pulls us into platform features we don't need. |
| **Not a fit (re-check)** | Paubox | Their primary product is HIPAA email, not DirectTrust messaging. Confirm their Direct add-on is DirectTrust-accredited before considering. |

## Outreach plan

1. Send the templated outreach email (see `docs/templates/hisp-outreach.md`) to **DataMotion** and **Kno2** in parallel.
2. If neither replies within 7 days, expand to iCoreExchange.
3. Decision deadline: **2026-05-16** (2 weeks). Vendor pick locked by then to keep the contract-by-2026-06-13 target.

## Deeper questions to ask in the discovery call

These are the differentiators that public sites won't answer:

1. **Trust bundle scope**: which trust bundles do they cross-sign? (DirectTrust Aggregate is table stakes; some HISPs add SAFE-BioPharma or state HIE bundles.)
2. **Failed-delivery semantics**: when a recipient HISP rejects the message, do we get a webhook with the reason or a silent NDR email?
3. **Bounce handling**: how do delivery confirmations differ from "received but not read" vs "rendered by recipient"? Direct Trust's MDN (Message Disposition Notification) coverage varies.
4. **Attachment size limits**: our packets can hit 10–25MB with embedded attachment PDFs (Phase 10).
5. **Inbound (receiving) Direct messages**: do we want to receive responses Phase-9-side, or is this outbound-only? If inbound, additional inbox/parsing infrastructure required.
6. **Audit log retention**: how long does the HISP retain message metadata? HIPAA requires 6 years retention for our side, but we may rely on the HISP's records for non-repudiation.
7. **DNS records they require**: TXT for domain verification, MX for inbound, possibly CNAME for sub-domain delegation. We use Namecheap → confirm they support the records the vendor needs.
8. **Migration path**: if we need to switch HISPs later, can we keep the same Direct address? (Usually no; switching means recipients update their address books.)

## Sources

- DataMotion: <https://docs.datamotion.com/>, <https://www.datamotion.com/products/direct-secure-messaging/>
- Updox: <https://updox.com/products/secure-messaging/>
- Kno2 / MaxMD: <https://kno2.com/products/direct-messaging/>
- Paubox: <https://www.paubox.com/products/email-suite>
- iCoreExchange: <https://icoreconnect.com/icoreexchange/>
- DirectTrust Accredited HISPs list: <https://directtrust.org/who-we-are/our-members/accredited-trust-anchor-bundle/>
