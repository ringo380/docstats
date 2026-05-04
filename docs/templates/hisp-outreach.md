# HISP outreach email — template

**Use**: cold inquiry to a DirectTrust-accredited HISP (DataMotion, Kno2, iCoreExchange, etc.) to start the sandbox + BAA conversation.

**How to use**:
1. Replace `<placeholders>` with your details.
2. Send from your usual work address (vendors associate identity with the sender domain — sending from `referme.help` if possible improves trust).
3. Send to the vendor's listed sales / partnerships email, or their "Contact Sales" form. Avoid generic info@ when an alternative exists.
4. Send to **DataMotion and Kno2 in parallel** — competing quotes accelerate both.

---

## Subject line options (pick one)

- `Pilot HISP integration — REST API + sandbox availability`
- `referme.help: Direct Trust integration inquiry — pilot volume`
- `Direct Trust messaging for a referral platform — sandbox + BAA questions`

---

## Email body

> Hi <First name or "Direct Trust team">,
>
> I'm Ryan Robson, founder of **referme.help** — a clinical referral platform that helps US providers send and track referral packets between clinics. We're integrating Direct Trust messaging as our outbound channel for sending referral packets to receiving providers, and I'd like to evaluate <Vendor> as our HISP partner.
>
> **Current shape of the integration:**
>
> - Outbound only at first (we send Direct messages on behalf of our org admins to receiving providers).
> - Pilot volume: **≤500 messages/month** for the first 6 months, scaling toward **5K+/month** as we onboard more clinics.
> - Stack: Python / FastAPI service hosted on Railway. We'd consume your **REST API** for sending and **status webhooks** for delivery confirmations. We have working integrations with Resend (email) and Documo (fax) on the same dispatcher; Direct would be the third channel.
> - Packet size: typical 1–10MB PDF; occasional ≤25MB with embedded attachments.
> - Tenancy: each customer organization gets their own Direct address (e.g. `referrals@<their-subdomain>.referme.help`).
>
> **What I'd like to confirm before going further:**
>
> 1. **Pricing for our pilot tier** (≤500 msg/mo). Is there a small-volume / pilot SKU, or do we need to commit to a higher minimum?
> 2. **BAA path** — is BAA included at standard tier, or is it gated behind enterprise pricing? Typical BAA review timeline?
> 3. **Sandbox / test environment** — do you provision a test Direct address up front so we can build and test before the production address goes live?
> 4. **Webhook semantics** — what events fire (sent, delivered, failed, MDN), and how is signature verification done (HMAC, mTLS, other)?
> 5. **Custom FQDN support** — we'd like to provision Direct addresses on a sub-domain we control (e.g. `direct.referme.help`). What DNS records do you require, and what's the verification process?
> 6. **Trust bundle coverage** — which trust bundles do you cross-sign beyond DirectTrust Aggregate? Most of our receiving providers are on hospital systems (Epic, Cerner) and large group practices.
> 7. **Onboarding timeline** — from signed contract to live sandbox to live production address, what's a realistic timeline?
> 8. **Inbound messaging** — we're outbound-only at launch, but expect to add inbound (receiving Direct messages) in a later phase. Is that the same SKU or a separate add-on?
>
> Happy to do a 30-minute discovery call this week or next. I'm based in <Time zone> and flexible on times.
>
> Thanks,
> Ryan Robson
> Founder, referme.help
> <Phone>
> <Calendar link, if any>

---

## Follow-up email (D+5 if no reply)

> Hi <First name>,
>
> Following up on my note from <day> about Direct Trust integration for referme.help. We're picking a HISP partner this month and would love to keep <Vendor> in the running. Even a quick "here's our pilot pricing sheet" or "schedule a 15-min intro" would help.
>
> Thanks,
> Ryan

---

## Notes for the conversation itself

When you get a reply, use these as discovery questions:

- **Compliance posture**: HITRUST? SOC 2 Type II? Both useful when our pilot clinics ask about subprocessor diligence.
- **MDN handling**: do they parse the recipient's MDN (Message Disposition Notification) and surface "displayed" vs "delivered" status, or do they only confirm transport?
- **Bounce categorization**: when a Direct address is invalid or the recipient HISP rejects the message, what error code do we get and how is it surfaced in the webhook?
- **Pricing tiers**: where does the price-per-message break point sit (5K? 10K? 50K?). Knowing the slope helps when we forecast Phase 11 expansion.
- **Subprocessor list**: do they use AWS / GCP / Azure under the hood, or is it self-hosted? Affects our subprocessor disclosure to clinics.
- **Migration path**: if we ever need to switch HISPs, can we port the Direct address? (Usually no, but worth asking — affects switching cost down the line.)

## Don't say in the outreach email

- Anything about specific patient volumes per clinic — it's commercially sensitive and not something the vendor needs at this stage.
- Specific clinic names — vendors love to add logos to their pitch decks; protect customer relationships.
- Internal phase numbers (`9.D`, `Phase 11`, etc.) — vendor doesn't care about our roadmap shorthand.
