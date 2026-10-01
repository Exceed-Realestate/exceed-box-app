# FABLE-AUDIT-R3 — Exceed Box, round 3 (adversarial re-audit)

Date: 2026-08-19 · Auditor: Fable 5 (read-only) · Nothing changed but this file.
Backend run live on :8918 (dev auth, 63 seeded leads). DB backed up and restored after each
mutating probe. 123 tests green (c1_media 9 · contract 28 · decisions 28 · fable_fixes 18 · permissions 40).

---

## Verdict

**Every round-2 fix (N1–N8) holds under live adversarial probing.** I could not take over an account,
could not pull a redacted voice note as marketing, could not make the list and detail disagree, could
not miscount threshold crossings across up/down/merge/delete, and could not surface the three OCR
wrong-value cases R2 reproduced. `verify_aud` is on and rejects wrong/missing audiences. The
server-side domain lock and `sub`-based identity binding are real 403s, not config hopes.

**Round 3 did not break anything and did not regress the earlier fixes.** What remains is small: one
narrowed-but-not-closed OCR residual (a 2-word Title-Case slogan can still be mis-slotted as `name`
when it is the only unclaimed block), one NaN-confidence robustness gap that is not reachable from the
real device path, and one architectural consequence of the N3 self-heal — read (`GET`) endpoints now
issue writes, which changes their concurrency profile on SQLite and would break against a read-only
replica. All three are LOW.

**Nothing actionable of substance remains that is not either (a) one of these three LOW polish items or
(b) blocked on an external dependency.** The loop has essentially reached its stopping condition: the
real remaining work is Supabase project config, SendGrid, OCR/transcription providers, GoHighLevel,
Google Calendar, APNs/FCM, a public booking domain, and Balraj's own unmade rulings (D8 decay mode,
D15 close-probabilities, D17 sequence exits, H4 auto exit-states, whether audio memos are contact-class).
See the final section for the full blocked list.

---

## Regression table — every round-2 finding

| R2 | finding | R3 verdict |
|---|---|---|
| **N1** | Domain lock had no server enforcement; email-claim adoption = takeover | **HOLDS.** Live: sub-mismatch on a bound email → 403 `account_conflict`; non-exceed domain → 403 `domain_not_allowed`; subdomain `x.exceed-re.ae` → 403; double-`@` `a@exceed-re.ae@gmail.com` → 403 (rsplit takes real domain); empty email → 403. First-login adopt of an *unbound* row works (intended), and a *second* sub then claiming that now-bound email → 403. Uppercase domain accepted case-insensitively then auto-provisions inactive (correct). Client half (`supabase.ts`) applies `assertCompanyDomain` on **both** password and Google paths. |
| **N2** | Voice notes gated one tier weaker than cards | **HOLDS.** Live on lead 1 voice note: marketing 403, non-owner sales 403, owner sales 200, office_manager 200 (has `consent.any`), unauth 401. Now gated on `_can_see_contact`, same as cards (api.py:2693). |
| **N3** | Materialized score drifts stale; list vs detail disagree | **HOLDS.** Poisoned lead 1 to stored `score=999, ts=2020`; list returned **52**, detail returned **52**, and the column self-healed to 52 with a fresh timestamp. `fresh_score()` on every list/pipeline/hot-leads/task read path; `team_status` deliberately not healed (see LOW-3 — real but bounded, and documented in code). |
| **N4** | Threshold-crossing count wrong across edges | **HOLDS.** Live: baseline 3. Signal +30 crosses lead up → 4. Void that event → lead back below threshold → count returns to 3 (excluded despite an `up` row in-window, because `l.score < thr`). Re-cross up (up/down/up) → `DISTINCT lead_id` counts once → 4. Merge lead → excluded (→3). `DELETE` lead → `ON DELETE CASCADE` removed its crossings (0). |
| **N5** | OCR guesser surfaced wrong values (tagline/dept→name, shaky phone) | **HOLDS on all three.** Live `guessFields`: tagline "Your trusted partner in real estate" → `name` blank; kana+`営業部` → `name` blank (`JP_NON_NAME_RE`); phone at Vision conf 0.15 → blank (0.9×0.15=0.135 < 0.5). Structural × `block.confidence` applied on every field. One narrowed residual → NEW-1. |
| **N6** | Furigana emitted `name_kana`, form reads `reading` — dead | **FIXED.** Guesser now emits `reading` (fieldGuesser.types.ts); live valid furigana pairs `name` + `reading` correctly; `CardScanScreen` fill loop reaches it. |
| **N8** | JWT accepted with `verify_aud: False` | **HOLDS.** `_decode`/`devauth.decode` both pass `audience=SUPABASE_AUD, verify_aud:True`. Live: wrong aud → 401, missing aud → 401, correct aud → 200. |
| **N7** | `imports` media branch = defensive dead code | **STILL DEAD (correctly hardened).** `imports` table empty; `media.save("imports", …)` never called. The branch now also requires `import.csv` **and** uploader identity (api.py:2714-2720), so if imports are ever persisted it is already gated. No live path. |

---

## New findings by severity

### LOW

**NEW-1 · OCR `name` can still be a 2-word Title-Case slogan when it is the last unclaimed block.**
`modules/expo-text-recognition/src/fieldGuesser.ts:98-115` (`isNameShaped`) + `:265-274` (fallback).
R2's N5 fix added name-shape gating, which closes the tagline/department cases — but `isNameShaped`
accepts *any* 2-4 word Title-Case string not in the `EN_NON_NAME_WORD_RE` blocklist. Reproduced live:
a card with company/title/email/phone/address all claimed and one remaining block `"Smart Living"` →
`name = {"value":"Smart Living","confidence":0.57}`, above 0.5, so `CardScanScreen` auto-fills it.
Other slogans that dodge the blocklist: "Future Homes", "Dream Living", "Prime Location". The R2 case
("Your trusted partner…") is now caught because it contains "trusted"/"partner"; a shorter slogan
without a blocklisted word is not. Blast radius is small — `name` is required so the rep looks at it,
OCR fills only empty fields, everything is editable — but it still violates the "never invent a name"
rule in this narrow shape.
**Fix:** the last-one-standing name fallback is the weak link; either drop it entirely (a card whose
name did not pair with furigana is better left blank for the rep to type), or lower its confidence
below 0.5 so it never auto-fills, or extend the blocklist. Do not rely on enumerating slogans.

**NEW-2 · A non-finite `block.confidence` bypasses the whole confidence gate (not reachable from the
device today).** `fieldGuesser.ts:277-281`. `CardScanScreen.tsx:62` fills any field with a non-empty
`value`, delegating the *entire* threshold decision to `guessFields`. `guessFields`'s final safety net
is `if (confidence > 0 && confidence < MIN_CONFIDENCE) blank`. If `block.confidence` is `undefined`/`NaN`,
`structural × NaN = NaN`, and `NaN > 0` is false — so the field keeps its value with `confidence: NaN`
and the form fills it. Reproduced live: a block with `confidence: undefined` → `company` surfaced with
`confidence: null` (NaN) and a value. **Not reachable from the real path** — the Swift module always
emits `"confidence": Double(candidate.confidence)` (ExpoTextRecognitionModule.swift:65), and the type
requires `number` — so this is a robustness gap for a future JS caller / mock, not a device bug.
**Fix:** make the gate NaN-safe: `if (!(result[key].confidence >= MIN_CONFIDENCE)) result[key] = emptyField();`
(catches NaN and re-asserts the "value only above threshold" contract `CardScanScreen` depends on).

**NEW-3 · The N3 self-heal makes read (`GET`) endpoints issue writes — changes their concurrency
profile and breaks against a read-only replica.** `scoring.fresh_score()` → `recompute_lead()` runs
`UPDATE leads SET score…` and can `INSERT score_threshold_crossings` from inside `GET /api/leads`,
`/api/pipeline`, `/api/dashboard` (hot_leads), `/api/today`, `/api/team`-adjacent task reads, and
`GET /api/leads/{id}` task lists (api.py:544, 858, 975; tasks.py:152). Two consequences the tests
(all single-threaded) do not cover:
1. **SQLite write contention.** Concurrent dashboard loads by several reps now take write locks on GETs
   (only when a row is stale, >300s), so `SQLITE_BUSY` becomes possible under load where before GETs
   never wrote. On the Postgres deploy target this is fine (row-level locks); on the SQLite that every
   demo and test runs, it is a new failure mode.
2. **Read-replica / read-only DB user breaks.** If GETs are ever routed to a replica or a read-only
   connection (a normal scaling move), these endpoints will error on the write instead of serving.
3. **Crossing-timestamp attribution drifts.** A decay-driven crossing is stamped with *when someone
   happened to read the lead*, not when it truly crossed, and two concurrent stale reads on separate
   connections can each `INSERT` the same `down` crossing (duplicate rows; `crossed_this_week` uses
   `DISTINCT lead_id` so the count is unaffected, but the table accumulates dupes).
**Fix (pick one):** compute the fresh score in memory for the response without persisting on a GET, and
leave persistence to `record()/void()`/`sweep_decay()`; or gate the self-heal write behind "only if this
connection is writable"; or wire `sweep_decay` on a schedule (LOW-4 below) so reads rarely find staleness
and the write-on-read path stays cold. At minimum, document that these GETs require a writable primary.

**LOW-4 (carried, not new) · `sweep_decay` is still unscheduled.** `scripts/sweep_decay.py` exists;
nothing in the repo runs it. Self-heal on read covers every row an endpoint actually returns, so the
*visible* number is always correct — but a lead nobody reads keeps a stale-high stored `score`, which
is what `_hot_leads`'s SQL pre-filter and `/api/scoring/model`'s `l.score >= thr` filter both trust.
The over-count is bounded small (a lead that crossed up this week had a fresh write within 7 days, and
7 days of a 60-day half-life is ~6% decay), and `_hot_leads` re-checks with self-heal after the SQL
filter, but the scoring-model count has no such second check. Wiring the sweep as part of deployment
closes both this and NEW-3's staleness. This is a deployment step, not a code bug.

---

## Notes that are NOT findings (checked, cleared)

- **Adoption-on-first-login is not exploitable within the model.** Adopting an incoming `sub` onto an
  unbound row is intended (a real staffer's first login). Taking over an *admin* unbound row requires a
  validly-signed Supabase token bearing that exact `@exceed-re.ae` address — i.e. control of that
  mailbox — which the domain gate + `email_verified` reject for outsiders. Residual risk lives entirely
  in Supabase signup config (blocked, external).
- **`email_verified` is only rejected when explicitly `false`.** Real Supabase tokens often carry
  `email_verified` inside `user_metadata`, not top-level, so `claims.get("email_verified")` may be a
  no-op for them. This is defense-in-depth on top of the domain gate and Supabase's own verification;
  not independently exploitable, but worth knowing the top-level check may not fire in production.
- **`refreshSupabaseSession` does not re-assert the domain gate** — acceptable, since a stored refresh
  token only exists for a session that already passed the gate, and the backend re-checks domain on
  every request.
- **MOCKS_ENABLED sign-in accepts any password for a demo email and skips the domain gate** — demo mode
  only, fake data, mock token the real backend never accepts. Not a production path.
- **schema.sql and migrations/0004 agree** on `score_threshold_crossings` (columns, both indexes) and
  on `leads.score_updated_at` / `app_user.supabase_uid`. No drift.

---

## Genuinely blocked on an external dependency (set aside)

- **Supabase project** — real JWT round-trip, signup/domain config, `email_verified` semantics, the
  `POST /api/users` real invite, end-to-end `sub` binding. All code paths fail closed without it.
- **SendGrid** — outbound sends and the `/webhooks/email` producer side.
- **OCR provider** — server-side card extraction (`scan_lead.extraction.configured=false`); the on-device
  Vision guesser is the only reader and needs a physical device to exercise fully.
- **Transcription provider** — voice-note transcripts (`configured=false`).
- **GoHighLevel** — no credentials; sync is honestly stubbed read-only.
- **Google Calendar** — no connection; `/api/calendar` reads own DB only.
- **APNs/FCM** — push is stubbed; `notify-rep` degrades to a task (works).
- **Public domain + hosting** — public booking page.
- **Scale** — H1/N4 behaviour at 30k rows (only 63 seeded here).
- **Balraj's unmade rulings** — D8 decay mode (`per_event` vs `lead_level`), D15 stage close-probabilities,
  D17 sequence exit conditions, H4 auto exit-states, and whether an audio memo counts as contact-class
  (the N2 gating choice). All parked behind flags / labelled `provisional`, correctly.

---

## What I verified live this session

- N1: 8 takeover/domain attack shapes, all rejected or correctly handled (see regression table).
- N2: voice-note access across marketing / non-owner sales / owner / office_manager / unauth.
- N3: poisoned stored score reconciled to `explain()` on both list and detail; column persisted.
- N4: up / down / up-again / merge / delete crossing edges, all counted correctly.
- N5/N6: three R2 wrong-value cases all now blank; valid furigana still pairs; `reading` key reaches form.
- N8: wrong / missing / correct `aud` → 401 / 401 / 200.
- Client N1: `assertCompanyDomain` on both password and Google paths in `supabase.ts`.
- 123 backend tests green.

Not exercised (needs infra that does not exist): Supabase round-trip, 30k-row scale, on-device Vision,
concurrent-GET SQLite contention under real load.
