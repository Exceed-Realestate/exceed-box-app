# FABLE-AUDIT-R2 — Exceed Box, round 2 (re-audit after fixes)

Date: 2026-08-19 · Auditor: Fable 5 (adversarial, read-only) · Nothing changed but this file.
Scope: verify round-1 fixes (`FABLE-AUDIT.md`) actually hold, then audit what is new —
`modules/expo-text-recognition/`, `src/auth/supabase.ts`, the two new test files and
`scripts/check-contract.js`. Backend run live on :8916 (dev auth); seeded 63 leads.

---

## Verdict

**The round-1 fixes are real, not cosmetic — verified live, not on trust.** The media PII leak
(C1) is genuinely closed across cards, voice notes and alternate paths; the contract checker (C3/C4)
actually parses call strings and I confirmed it fails when a real mismatch is reintroduced; the score
is materialized and the route/timezone/paging fixes hold under live probes. This is a materially safer
system than round 1.

**But the fixes introduced two new soft spots, and the new code has three real holes:**

1. **The D20 `@exceed-re.ae` domain lock has NO server-side enforcement and is not applied on the
   password path.** The backend verifies the JWT signature and then binds the token to any existing
   active app_user *by email claim* — so if the (not-yet-existing) Supabase project ever permits
   password/email signup for an arbitrary address, that is an account-takeover path, and the only thing
   standing in front of it is Supabase project config that no code here enforces. This is the single
   most dangerous new thing.
2. **The materialized score is correct at write time but drifts stale between decay sweeps, and the
   sweep is never wired.** Worse, the list/pipeline/hot-leads read the stored (stale, inflated) column
   while LeadDetail reads live `explain()` — the *same lead* shows two different scores on two screens
   the moment any decay has elapsed. Demonstrated: lead 1 stored 52 vs live 31 at +90d.
3. **The OCR field guesser can put a wrong value in a field**, violating its own stated rule, in three
   ways I reproduced live: a tagline surfaced as the person's name, a department surfaced as the name,
   and a shakily-read phone surfaced at high confidence because the guesser ignores Vision's own
   per-block OCR confidence entirely.

---

## Regression table — every round-1 finding

| R1 | finding | R2 verdict |
|---|---|---|
| **C1** | Media endpoint leaks PII across roles | **VERIFIED CLOSED.** Live: card — owner 200, non-owner sales 403, marketing 403, unauth 401. `card_image_path` redacted in `_lead_detail` (api.py:381) when contact redacted. No alternate/cross-category path (filename param takes no slashes; `path_for` refuses escape; wrong-category DB lookup 404s). Voice note has a **residual gap — see N2**. |
| **C2** | iOS app cannot authenticate against backend | **FIXED (untestable end-to-end).** `src/auth/supabase.ts` implements real GoTrue password/refresh/OAuth; AuthContext persists token+refresh+expiry, refreshes on restore. No Supabase project exists, so it fails closed. Token *handling* has a **new hole — see N1**. |
| **C3** | App↔backend route/shape breaks | **VERIFIED CLOSED.** `client.ts` routes/bodies/envelopes all corrected (voice→`/notes` multipart, push→`/push/*` with platform, void→`/send {action:'not_real'}`, scan→Form+`consent_given`, replies envelope unwrapped via `leadReplyFromBackend`). `check-contract` 47/47. |
| **C4** | Green tests, untested integration boundary | **VERIFIED CLOSED, with limits.** `check-contract.js` AST-parses real `request()`/`requestMultipart()` call strings and diffs against the *live* OpenAPI. I reintroduced the `/voice-memos` bug in a copy → it hard-failed (exit 1). Blind spots below (N5). |
| **H1** | Per-lead score recompute at 30k | **FIXED, with a new staleness/consistency problem — see N3.** List/pipeline/hot-leads/team now read the `leads.score` column; one endpoint still full-scans (N4). |
| **H2** | Pipeline 200/column, no paging | **VERIFIED CLOSED.** `/api/pipeline` has page/page_size/offset/has_more (api.py:827-855); test reaches past page 1. |
| **H3** | Decay per-event vs lead-level unratified | **PARKED BEHIND A FLAG (correct).** `EXCEEDBOX_DECAY_MODE` default `per_event` (scoring.py:45); both branches tested; `/api/scoring/model` reports the active mode. Still needs Balraj's ruling; flip requires a full re-sweep (folds into N3). |
| **H4** | Auto exit-states unratified | **PARKED BEHIND A FLAG (correct).** `EXCEEDBOX_AUTO_EXIT_STATES` default `1` (tracking.py:30); off still records the legally-required underlying fact; exits always badged provisional. |
| **Timezone** | Naive datetimes, JST/GST 5h | **VERIFIED CLOSED.** Aware-UTC storage, office-zone parse of naive input. Live: Tokyo 14:00→05:00Z, Dubai 14:00→10:00Z (5h). Applied on task create, snooze/reschedule, follow, booking. |
| **M4** | List can't sort by score | **FIXED** via the materialized column (api.py:515 `ORDER BY l.score DESC`); test passes. |

**Tests:** 113 total green (c1_media 5 · contract 28 · decisions 28 · fable_fixes 13 · permissions 39).

---

## New findings by severity

### HIGH

**N1 · The `@exceed-re.ae` domain lock has no server-side enforcement and is missing on the password
path — latent account takeover.**
`app/auth.py:205-229` (`current_user`) verifies the Supabase JWT signature (HS256) and reads `email`,
but **never checks the email domain** and never checks `email_verified`. `load_or_provision`
(`auth.py:172-202`) then does: no row for this `sub` → look up an existing row **by email claim**
(`auth.py:178-179`) and **adopt the incoming `sub` onto that pre-existing account** (`:180-182`). On the
client, the domain gate exists **only in the Google path** (`supabase.ts:172`) — the password path
`signInWithEmail` (`AuthContext.tsx:168`) applies **no domain check at all**.
Consequence: if the (not-yet-created) Supabase project permits email/password signup for an arbitrary
address, an attacker who obtains a validly-signed token bearing `email=<an active staffer's email>`
inherits that staffer's active account and role — full takeover, bypassing the "auto-provision
inactive" fail-closed default. Even without the email collision, the password path lets any Supabase
account in (mitigated only by inactive-by-default). The `supabase.ts` comment claims "the backend is
the real authority on the token's validity" — true for *signature*, false for *domain*, which is the
entire point of D20.
**Fix:** in `current_user`, reject any token whose `email` does not end `@exceed-re.ae` (and require
`email_verified`/correct `aud`) — do not trust Supabase config to be the only gate. Do not bind an
incoming `sub` to an existing active row by email alone. Apply the same domain check on the client
password path as on Google. This must land *before* a Supabase project is created, not after.

### MEDIUM

**N2 · Voice notes are gated one tier weaker than business cards — marketing can pull a rep's audio
memo on a lead whose contact fields are redacted from it.**
`get_media` gates `voice_notes` on `_can_view_lead` (`api.py:2629-2635`, i.e. `leads.all`), while
`business_cards` correctly gates on `_can_see_contact`. Marketing has `leads.all` but only
`consent.own`. Live: marketing@ → **200** on a non-owned voice note; non-owner sales → 403. A voice
memo dictated by a rep routinely contains the client's name/phone/email spoken aloud — the exact PII
the redaction model (and C1's own fix) exists to withhold from marketing. Round 1 explicitly flagged
voice notes as "same class" as cards; the fix downgraded them. `test_c1_media_security.py` only asserts
the *sales* denial, so it locks in the weaker behaviour.
**Fix:** gate `voice_notes` on `_can_see_contact`, same as cards, unless Balraj rules audio memos are
non-contact.

**N3 · Materialized score drifts stale between sweeps, and the list vs detail read it inconsistently —
the same lead shows two scores.**
`recompute_lead` runs on every `record()`/`void()` (scoring.py:212,224), so the column is exact *at
write time*. But behaviour points decay with pure time passing, reconciled only by `sweep_decay`
(scoring.py:260) — which is **not wired** (only `scripts/sweep_decay.py`, a documented-but-unscheduled
cron). Meanwhile the list (api.py:524), pipeline (:835), hot-leads (:944) and team read the **stored**
column, while `_lead_detail` reads **live `explain()`** (api.py:365). Demonstrated on seed data at +90
days with no sweep: lead 1 stored **52** vs `explain()` **31**; lead 33 stored **24** vs **1**. So
hot-leads shows leads as hot that detail shows as cold, and the D8 decay's whole point (leads dropping
out of nurture) silently doesn't happen in the list views until a cron nobody has scheduled runs.
Flipping `EXCEEDBOX_DECAY_MODE` (N/H3) makes the column wrong for *every* lead until a manual sweep.
`test_materialized_score_matches_explain` only checks immediately post-write, so it never sees this.
**Fix:** either wire the sweep (launchd/cron) as part of deployment and say so, or have the list read
`explain()` for the top-N it actually shows, or store `score_updated_at` and recompute lazily on read
when older than the decay granularity. At minimum, make list and detail read the same source.

**N4 · `/api/scoring/model` still full-scans every live lead, scoring each twice — the exact H1 pattern
H1 was meant to kill.**
`api.py:1470-1474` loops `SELECT id FROM leads WHERE merged_into IS NULL` and calls `scoring.score()`
**twice** per lead (now and week_ago) to count "crossed threshold this week." At 30k leads that is
~60k `explain()` calls ≈ ~180k SQLite round trips per request, on an endpoint every active role may
view. It was missed because the materialized column can only answer "now", not "a week ago". (The SNS
sample loop at :1668 also calls `scoring.score()` but is bounded to LIMIT 5, so it is fine.)
**Fix:** filter to leads currently `score >= threshold` via the column first, then recompute week_ago
only for that (small) set — or store a weekly threshold-crossing counter.

**N5 · OCR field guesser can surface a wrong value, breaking its own "never invented" rule — three
reproduced cases.**
`modules/expo-text-recognition/src/fieldGuesser.ts`. Reproduced live by running `guessFields` on
crafted blocks:
- **Tagline → name (0.6):** a card where company/title/email/phone/address all parse leaves one
  unclaimed block; the name-by-elimination fallback (`fieldGuesser.ts:201-209`) assigns it to `name`
  with **no name-specific signal**. Input `"Your trusted partner in real estate"` → `name` =
  `{"value":"Your trusted partner in real estate","confidence":0.6}`, above the 0.5 threshold, so
  `CardScanScreen` auto-fills it.
- **Department → name (0.8):** the furigana pairing (`:182-197`) assigns the block *below* a kana line
  to `name` with no validation it is a name. Input kana `"えいぎょう"` + `"営業部"` → `name` =
  `{"value":"営業部","confidence":0.8}`.
- **Shaky OCR → high confidence:** the guesser **never reads `block.confidence`** (Vision's per-line
  OCR confidence, present in `ExpoTextRecognition.types.ts:21` and returned by the Swift module). Its
  confidences are purely structural (regex shape). A phone Vision read at confidence 0.15 →
  `phone {"value":"090-0000-0000","confidence":0.8}`. For phone/email a single misread digit is a
  wrong value presented as trustworthy.
Mitigations that limit blast radius: fields are editable, OCR fills only *empty* fields
(`CardScanScreen.tsx:62`), and name is required so the rep looks at it. But the product rule is
"low-confidence must come back empty, never invented," and these three surface invented/mis-slotted
values above threshold.
**Fix:** multiply structural confidence by Vision's `block.confidence` before comparing to
MIN_CONFIDENCE; require a positive name signal (kana-adjacency alone is not enough — validate the
neighbour is not a title/dept/company/address token) and drop the pure last-one-standing name guess, or
lower its confidence below the fill threshold.

### LOW

**N6 · Furigana reading never populates the form — key mismatch.** The guesser emits `name_kana`
(`fieldGuesser.ts:187,194`) but `CardScanFields` uses `reading` (`src/api/types.ts:555`). The fill loop
(`CardScanScreen.tsx:60-66`) writes `next['name_kana']` (a phantom key the UI never shows) and never
`reading`, so the furigana guess is silently discarded. Dead functionality, not a correctness/leak
issue. Extra `name_kana` key is harmless to the backend (it reads fields individually).
**Fix:** map `name_kana` → `reading` in the fill loop.

**N7 · `get_media` `imports` branch is defensive dead code.** `media.save("imports", …)` is never
called (only `business_cards` at api.py:2042 and `voice_notes` at :2301). No import file is written and
no import path is returned to any client, so the `import.csv`-gated branch (api.py:2636-2644) guards
nothing today. Not a live leak; note it so it is not mistaken for a tested path. If imports are ever
persisted, marketing (which holds `import.csv`) would be able to read any import file — a cross-lead
PII dump — by uuid, so gate per-uploader then.

**N8 · Supabase JWT accepted with `verify_aud: False`** (`auth.py:142`). A token minted for a different
audience/project sharing the same HS256 secret would be accepted. Low on its own; fold the `aud` check
into N1's hardening.

---

## What I verified working (this session)

- **C1 live:** business card — owner 200 / non-owner sales 403 / marketing 403 / unauth 401; voice note
  — non-owner sales 403 (marketing 200, that is N2); alternate-category and traversal paths 404.
- **C3 checker actually catches breaks:** reintroduced `/api/leads/{id}/voice-memos` in a copy of
  `client.ts` (real repo untouched, read path monkeypatched) → 2 hard failures, exit 1. Baseline 47/47.
  All backend calls route through `client.ts`'s `api` object; only external fetches are Supabase auth.
- **Timezone live:** Tokyo naive 14:00 → `2026-09-01T05:00:00+00:00`; Dubai naive 14:00 →
  `2026-09-01T10:00:00+00:00` (5h).
- **H1 materialization + drift:** column == `explain()` now; diverges over time without a sweep
  (lead 1: 52 vs 31 at +90d; lead 33: 24 vs 1). Sweep script exists, not scheduled.
- **OCR guesser:** three wrong-value cases reproduced by running `guessFields` directly.
- **Backend auth:** `current_user` verifies signature, no domain check, binds by email claim,
  auto-provisions unknown as inactive sales. Password client path has no domain gate.
- **Tests:** 113 green across 5 files, incl. the two new ones.

Not exercised (needs infra that does not exist): the Supabase round trip end-to-end (no project),
H1/N4 at 30k rows, on-device Vision OCR on a physical device, EAS production env resolution.
