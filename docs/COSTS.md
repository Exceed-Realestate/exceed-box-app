# Exceed Box — what it costs to run

Prices checked **2026-09-14** against each provider's own page. Links are to the
page consulted, so you can re-check rather than trust this document later.

Everything is in US dollars unless marked otherwise.

---

## The short answer

| Scenario | Per month |
|---|---|
| **Pilot** — a handful of staff, a few hundred emails | **about $11** |
| **Running** — full Japan office, 10,000 emails a month | **about $40** |
| **At the 30,000 figure** — the whole list in nurture | **about $62** |

For comparison: the vendor quoted **¥8.1 million** to build this. At the middle
figure, running it costs roughly **¥6,000 a month**.

*Updated 2026-09-14 after deployment: the server line is now $0 (it runs on
an in-house Mac already owned) and the domain is the one actually bought.*

---

## Line by line

### Supabase — database, sign-in, file storage

[supabase.com/pricing](https://supabase.com/pricing)

- Pro plan: **$25/month**, includes $10 of compute credit
- Each project costs its own compute; a Micro instance is **$10/month**
- 8 GB disk per project included, then $0.125/GB
- 250 GB egress included, then $0.09/GB
- Daily backups, 7-day retention

**Your incremental cost is $10/month, not $35.** You already pay the $25 base
for Kiki eSIM. Exceed Box adds one Micro project.

⚠️ Worth deciding: three projects now run on that one Pro organisation —
`esim-backend`, `exceed-box` and `exceed-ai`. The account and the card belong to
Spicy Kiwi. Two of the three projects are Exceed work. Fine while building,
awkward the day Exceed pays for its own infrastructure.

30,000 leads with their events came to well under 1 GB, so the disk allowance is
not a concern at this size.

### The server — in-house Mac, $0

Exceed Box runs on an in-house Mac that is already owned and already on
24/7. The API, the mail worker and a Cloudflare Tunnel run as three launchd
services (see OPERATIONS.md). No hosting bill.

- **Cloudflare Tunnel: free.** It carries public traffic to the production host without
  opening a port on the home network.
- Electricity and the internet line are already paid for other uses.

The Docker/Caddy files in `deploy/` still work on any rented Linux server if
that is ever wanted; nothing about the app depends on the production host.

### Email — SendGrid

⚠️ **Not verified.** SendGrid's pricing page redirects in a loop that defeated
four attempts, so these figures come from the earlier decision log rather than
from the page today. **Check before committing.**

- Free: 100 emails/day
- Essentials: about **$20/month** for up to 50,000/month
- A dedicated IP is extra and is not worth it at this volume

At 10,000 emails a month you are inside Essentials with room to spare. The
seven-step sequence across 30,000 people is 210,000 sends spread over about
three months, roughly 70,000/month at peak — which would need the next tier up
for that period only.

### Reading replies — Claude API

[claude.com/pricing](https://claude.com/pricing), verified.

- Sonnet: $2 per million input tokens, $10 per million output
- Haiku: $1 per million input, $5 per million output

A reply is short. Say 1,500 input and 150 output tokens including the
instructions. On Sonnet that is about **$0.0045 per reply**.

| Replies per month | Cost |
|---|---|
| 100 | $0.45 |
| 500 | $2.25 |
| 2,000 (the busiest month of a 30k campaign) | $9 |

Round it to **$1 to $10/month**. Haiku halves it if that ever matters.

### Voice transcription

Optional, off by default. Whisper is about $0.006 per minute of audio. A rep
leaving ten one-minute memos a day, five days a week, is roughly **$1.20/month**
for the whole office. Effectively free at this scale.

### Domain and certificates

- Domain: **exceedbox.app, $14.20/year** at Cloudflare Registrar (bought
  2026-09-14, auto-renew on) — about $1.18/month
- HTTPS: **free**, Cloudflare issues and renews the certificate at the edge

### Apple, if the native app ships

$99/year, already paid on the Spicy Kiwi account. **No incremental cost**,
unless Exceed wants its own developer account, which would be another $99/year.

### WhatsApp, when it happens

Parked. Meta charges per 24-hour conversation, and the rate depends on country
and category. Business verification is free but takes days to weeks. Budget
nothing yet; revisit when you have a number.

---

## The three scenarios in full

### Pilot — a few staff, a few hundred emails

| | |
|---|---|
| Supabase, incremental | $10.00 |
| Server (in-house Mac) | $0.00 |
| SendGrid free tier | $0.00 |
| Claude, ~50 replies | $0.25 |
| Domain, monthly share | $1.18 |
| **Total** | **$11.43** |

### Running — full office, 10,000 emails a month

| | |
|---|---|
| Supabase, incremental | $10.00 |
| Server (in-house Mac) | $0.00 |
| SendGrid Essentials | $20.00 |
| Claude, ~500 replies | $2.25 |
| Transcription | $1.20 |
| Domain, monthly share | $1.18 |
| Contingency, ~15% | $5.19 |
| **Total** | **$39.82** |

### The 30,000 figure — peak of a full campaign

| | |
|---|---|
| Supabase, incremental | $10.00 |
| Server (in-house Mac) | $0.00 |
| SendGrid, ~70,000/month tier | $35.00 |
| Claude, ~2,000 replies | $9.00 |
| Transcription | $1.20 |
| Egress, still inside the allowance | $0.00 |
| Domain, monthly share | $1.18 |
| Contingency, ~10% | $5.64 |
| **Total** | **$62.02** |

That peak lasts only while the campaign runs. Afterwards it settles back to the
middle figure.

---

## What is fixed and what moves

**Fixed regardless of use:** Supabase project ($10) and the domain ($1.18).
About **$11/month** even with nobody using it.

**Moves with use:** email volume is the big one and the only line that could
surprise you. Everything else stays in single digits at this scale.

**No per-seat cost anywhere.** Adding the eleventh salesperson costs nothing.

---

## Two things to check before committing

1. **SendGrid's real price.** It is the largest variable line and the one figure
   here that was not verified today.
2. **Whose card.** Every line above currently lands on Spicy Kiwi. If Exceed is
   to pay, the Supabase organisation should be Exceed's from the start —
   moving it later means migrating a live database rather than changing a
   billing address. The domain is on the balrajplus971 Cloudflare account.
