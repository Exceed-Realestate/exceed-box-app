# FABLE-AUDIT — Exceed Box (backend + iOS app) vs OneBox demo + D1–D26

Date: 2026-08-19 · Auditor: Fable 5 (adversarial, read-only) · Nothing changed but this file.
Scope: `~/code/exceed-box/index.html` (feature source of truth, 2,752 lines) ·
`~/code/exceed-box-app` (FastAPI/SQLite, api.py = 2,465 lines) ·
`~/code/exceed-box-ios` (Expo SDK 57, TestFlight build 7) · decision log D1–D26.

---

## Verdict

**Two competent halves that have never been connected, shipped as if they were one product.**
The FastAPI backend is real: the ten scoring rules, decay, dedupe, consent, tasks, escalation and
the role matrix are genuinely implemented, tested (95 tests green), and filter in SQL. The iOS app
is a polished mock — every screen exists, but it runs entirely on client-side fixture data and
**cannot talk to the backend at all**: both real sign-in paths are stubs that throw "not connected"
(`AuthContext.tsx:110,136`), and `.env` bakes `EXPO_PUBLIC_USE_MOCKS=1` so TestFlight build 7 is the
demo, not the system. The day someone wires Supabase and points the app at the API, a second batch of
route/shape mismatches (voice notes, push, "this wasn't real", card scan) will surface that the
passing contract tests do not cover because they exercise the backend in isolation, never the app's
actual call strings.

**Single most dangerous thing:** `GET /api/media/{category}/{filename}`
(`app/api.py:2435`) gates only on being any active login. Verified live — a **marketing** user and a
**sales** user (chiaki), neither of whom owns the lead, both fetched a business-card image with
**HTTP 200**. A business-card photo is the single densest PII object in the system (name + company +
phone + email + address in one image), and `card_image_path` is returned in every `LeadDetail`
**unconditionally, even when contact fields are redacted** (`api.py:318`). The redaction that the
whole permission model is built around (SPEC rule 2) and the sales SQL isolation (rule 1) are both
fully defeated for card and voice-note media. This goes to a real Tokyo office holding ~30,000 client
records.

---

## Findings by severity

### CRITICAL

**C1 · Media endpoint leaks all PII across roles — verified live.**
`app/api.py:2435` (`get_media`). Checks `current_user` only; no per-lead ownership, no redaction.
Reproduced against the seeded DB on port 8914:
- marketing@ → `GET /api/media/business_cards/<uuid>.jpg` → **200**
- chiaki@ (sales, non-owner) → same file → **200**
- inactive pending@ → 403, unauthenticated → 401 (so it is gated, just not *enough*).
`card_image_path` is handed to marketing in `_lead_detail` even on a `contact_redacted:true` lead
(`api.py:318`), so this is not even "guess the uuid" — marketing has `leads.all`, opens any lead,
reads the path, fetches the image, and has the name/company/phone/email/address that were redacted
one field up. Same class for `voice_notes`.
**Fix:** store `owner_user_id` (or `lead_id`) alongside each media row; in `get_media`, load the
owning lead and apply `_can_see_contact(user, lead)` for `business_cards` and `_can_view_lead` for
`voice_notes`; return 404 (not 403) on miss so the URL space is not probeable. Stop emitting
`card_image_path` when `contact_redacted`.

**C2 · The iOS app cannot authenticate against the real backend — both routes are stubs.**
`AuthContext.tsx:110` (email/password) and `:136` (Google) both `setAuthError(...notConnected)` and
`throw` in non-mock mode. There is no Supabase client, no JWT persisted from a real login. Combined
with `.env` (`EXPO_PUBLIC_USE_MOCKS=1`) and `eas.json` (no `EXPO_PUBLIC_API_URL` in any build
profile), **TestFlight build 7 is the mock demo** — invented leads, tasks and KPIs, role chosen by
typing `admin@/marketing@/manager@/sales@`. Honest to a tester (a DEMO DATA banner shows,
`App.tsx:20`), but it means the "shipped app" has never exercised one line of the backend. D20/D25b's
Google-SSO-domain-locked login does not exist yet.
**Fix:** wire `@supabase/supabase-js` for both paths, persist the real JWT to the existing
`TOKEN_KEY`, set `EXPO_PUBLIC_API_URL` per non-mock build profile in `eas.json`, and ship a build
with `USE_MOCKS` unset to prove the round trip before calling it delivered.

**C3 · Latent app↔backend contract breaks in the SPEC-V2 wiring — green tests, wrong routes.**
These fire the moment C2 is fixed and the app runs non-mock. All verified by diffing `client.ts`
route strings against the actual `@app.*` decorators:
- **Voice notes** — app calls `GET/POST /api/leads/{id}/voice-memos` (`client.ts:508,516`); backend
  only has `/api/leads/{id}/notes` (`api.py:2140,2166`) → **404**. Worse, the POST sends JSON
  `{duration_seconds, local_uri}` (`VoiceMemoSection.tsx:71`) while the backend wants multipart
  `audio` UploadFile → would be 422 even at the right path. The 🎤 音声メモ feature is dead in real mode.
- **Push / notifications** — app calls `/api/notifications/settings`, `/api/notifications/register`
  (`client.ts:521,526,531`); backend has `/api/push/settings`, `/api/push/register`
  (`api.py:2214,2228,2189`) → **404**. Register also omits `platform`, which the backend requires → 422.
- **"This wasn't real" (D7, mandatory)** — app calls `POST /api/replies/{id}/void`
  (`client.ts:500`); no such route exists. The void path is `POST /api/replies/{id}/send` with
  `{action:'not_real'}` (`api.py:2084`) → **404**. The one-tap that removes wrongly-awarded points
  does nothing.
- **Card scan** — app posts a `fields` JSON blob + `consent` (`client.ts:423-424`); backend reads
  individual Form fields (`name` required) + `consent_given` (`api.py:1876-1880`) → **422 "name is
  required"**. The showroom-iPad capture (D11b#3) fails.
- **Return-shape drift** — `getLeadReplies` types `LeadReply[]` (`client.ts:482`) but the backend
  returns a paginated `{data,...}` envelope (`api.py:2035`); same pattern to check on
  `getBookingSlots`/`getAssignmentRules`.
**Fix:** align every SPEC-V2 route string and body to the backend, and add contract tests that call
through `client.ts` (or assert its literal paths), not just the backend routes.

**C4 · Contract tests pass while the integration boundary is broken.**
`tests/test_contract.py` (28 green) exercises backend routes directly; it never sees the app's route
strings, so all of C3 sails through green. The suite's own stated principle — "a denial that is not
tested does not count as implemented" (SPEC.md §done) — inverts here: an integration path that is not
tested end-to-end does not count as working, and none of §6/§9/§10 are.
**Fix:** a test layer that asserts `client.ts` paths/bodies match live backend routes (a generated
route manifest compared both ways), run in CI.

### HIGH

**H1 · Dashboard and Team full-scan every lead computing score in Python — will not survive 30k.**
`_hot_leads` (`api.py:835-842`) does `SELECT * FROM leads WHERE merged_into IS NULL` then
`scoring.score(c, id)` per row; `scoring.score`→`explain` runs ~3 queries/lead
(`scoring.py:66,52,57`). `tasks.team_status` (`tasks.py:194-220`) is worse — per staffer, per open
task, `scoring.score` again, plus 3 count queries each. At 30k leads a single `/api/dashboard` or
`/api/team` load is ~10^5 SQLite round trips in a Python loop. This is the first thing that falls over
in Tokyo, before any UI issue.
**Fix:** materialize score (a `lead.score` column recomputed on event write + a nightly decay sweep),
or a single windowed SQL aggregate; never recompute all-leads score per request.

**H2 · Pipeline caps 200 cards/column with no pagination — data silently invisible.**
`api.py:749` `LIMIT 200` per stage, no offset/cursor. With ~30k leads mostly in `new`/`nurturing`,
everything past the 200th card in a column cannot be reached from the app at all. `count` is honest,
but the board is not the data.
**Fix:** paginate/virtualize each column; load-more per stage.

**H3 · Decay interpretation is unratified and materially changes scores.**
`scoring.py:27-31` implements **per-event** age decay and flags in-comment that lead-level silence was
the alternative and needs Balraj's call. D8 says "60 days of silence" — ambiguous. Under the current
choice a lead's June reply keeps fading regardless of a December open; under the other reading one
recent open would revive it. This is a business-rule decision sitting unmade inside shipped code.
**Fix:** get the ruling; it changes who re-enters nurture (D8's actual point).

**H4 · Exit states fire automatically but were never ratified (carried over, still open).**
`unsubscribed`/`unreachable` are written by the SendGrid webhook (`tracking.py:116,122`) and
`lost/too_early` exist in schema, but the decision log (§"BUILT BUT NEVER EXPLICITLY APPROVED") and
SPEC.md both mark them provisional. Auto-transitioning a lead out of the funnel on an unratified rule
is a live behaviour, not a label.
**Fix:** a yes/no from Balraj; until then they are correctly badged provisional in the UI — keep it.

### MEDIUM

**M1 · `expected_revenue` returns null — correct, but note it is the only KPI tile that is inert.**
`api.py:884` returns `null` + reason (blocked on D15 stage close-probabilities). Honest; flagged only
so the client knows the 見込み売上 tile shows nothing until those percentages arrive.

**M2 · Referral source has an import door now (`/api/referrals`, `api.py:1234`) — resolves D11a#1**,
but Property Finder, WhatsApp/LINE official, and showroom-walk-in still have model support without a
real inbound path. Consistent with the demo's own gap; noted, not a regression.

**M3 · Mock-mode role isolation is enforced client-side only** (`client.ts:216,274` via `roleCan`).
Fine for a demo behind the DEMO DATA banner, but it must never be read as security — the real
isolation lives in SQL server-side (verified working, below). Keep the banner mandatory; never ship a
mock-enabled release build.

**M4 · List cannot sort by score.** `/api/leads` orders by `l.id` (`api.py:442`) because score is
computed in Python after the SQL page (`:447`). "Hot leads first" is impossible in the main list
without H1's materialization. Minor next to H1 but the same root cause.

---

## Coverage table — demo (OneBox/Exceed Box) → built state

| demo feature | state | what is missing / caveat |
|---|---|---|
| Dashboard KPI tiles (6) | **built** | `expected_revenue` inert (M1, blocked D15) |
| Trend chart | **built** | real per-day counts (`_trend`), not the demo's synthetic curve |
| Bookings by area (+日本ショールーム) | **built** | third slice added per D16 |
| Bookings by source | **built** | attributed to first-touch so bars total the tile (D16 fixed) |
| Reactivation funnel | **built** | real event counts |
| Hot leads / per-rep performance | **built** | but H1 scaling; per-rep gated to dashboard.per_rep |
| Today (mobile) | **built** | complete/snooze wired with error+retry (prior audit fixed) |
| Leads list + filters | **built** | region/source/owner at top level (prior audit fixed) |
| 名刺スキャン card scan | **partial** | flow + manual-entry honest (no OCR), but **C3 route/body break** in real mode |
| CSV/Excel import wizard | **built** | analyze→map→commit, dedupe on import (D11/D13) |
| GoHighLevel 連携/今すぐ同期 | **built (honest stub)** | `configured:false` + manual path; never fakes a sync (D11 read-only) |
| SNS/LP/Gmail receive feed | **partial** | sources modeled; no live "receive feed" action equivalent |
| Kanban pipeline | **partial** | tap→picker not drag (D25d, flagged); **200/column cap, no paging (H2)** |
| Nurture ステップメール | **built** | sequences/steps/stats + provisional exits; nothing sends (honest, no SendGrid) |
| SNS企画 (4 patterns + funnel) | **built** | |
| Tracking 反応検知・スコア | **built** | activity feed + scoring model panel |
| 予約フロー booking | **partial** | settings + slots built; public page out of scope (stated honestly) |
| 自動アサイン + calendar | **built** | rules + week view from own DB; no Google Calendar (honest) |
| システム連携 arch | **built** | live connected/not-connected status board |
| ✨ AI返信案 | **partial** | draft `configured:false` honest; **"this wasn't real" void route broken (C3)** |
| 🎤 音声メモ | **partial** | UI + storage exist; **route + payload break in real mode (C3)**; no transcription (honest) |
| 🔔 Push notifications | **partial** | settings/register/test built honest; **route + missing `platform` break (C3)** |
| Lead drawer actions (5) | **built** | notify/showroom/site/contact-date/follow each create a real task or event |
| 解説モード Explain mode | **built** | `ExplainMode.tsx` + `useExplainMode`, used on Tracking/Nurture/SNS |
| Login (Google SSO @exceed-re.ae) | **missing** | UI only; **both auth paths are stubs (C2)** |

### Decision-log correctness (D1–D26)

| rule | verdict |
|---|---|
| 10 scoring rules (D1–D6, D5a split) | **implemented** — `scoring.py`/`scoring_rules`; partnership +30 assumed pending confirm |
| Decay halve@60 / zero@120 (D8) | **implemented**, `decay_factor` correct — but per-event vs lead-level unratified (H3) |
| Behaviour fades / facts permanent (D8) | **implemented** — `BEHAVIOUR`/`FACTS` sets, booking_completed sits with facts |
| Reply rules-then-AI (D7) | **implemented** (`looks_automated`); AI layer honestly not wired; human-verdict path present |
| "This wasn't real" (D7) | backend `void` exists; **app route broken (C3)** |
| Dedupe/merge on import (D13) | **implemented + tested** (phone +81/0 normalisation asserted) |
| Consent per-channel, upgrade-only (D8/legal) | **implemented** (`lead_consent`, withdrawal final) |
| Six stages + four exits (D10) | **implemented**, stage/exit split into separate columns; exits unratified (H4) |
| Task = 6 fields, reason required (D12) | **implemented**; `reason` enforced |
| Escalation score×lateness (D12b) | **implemented** — `urgency = days × max(1, score/threshold)` |
| Role matrix, SQL-filtered (D12a/D25a) | **implemented + verified live** (below) |
| Manual +30 attributable (D4) | **implemented** — `audit_log.actor_user_id`, `set_by` on event |
| Expected revenue probabilities (D15) | **honestly null**, blocked on Balraj |

---

## Honesty of the product

The six unwired providers are handled correctly everywhere I checked — each returns
`{configured:false, reason, manual_path}` and never fakes success: GoHighLevel sync (`api.py:1991`),
AI draft (`:2051`), card OCR (`:1913`), push test (`:2260`), transcription (`_note_out:2131`),
SendGrid sends (nurture states nothing sends). The prior audit's one fabricated metric
(`totalLeads*7-41`) is gone — mock `first_sends` is now `leads with an email` (`mocks.ts:875`). Mock
data is fabricated but banner-flagged. **No invented-number-as-real lie found.** The dishonesty is not
in the numbers; it is structural: a mock TestFlight build and a backend it cannot reach are being
presented together as "the app," and the login screen implies an SSO that does not exist.

---

## What breaks first in Tokyo (ranked)

1. **Dashboard/Team load time (H1)** — Python per-lead score loops die at 30k before any user complains
   about a button.
2. **Media PII leak (C1)** — not a crash, a breach; 10–15 people each able to pull every client's card
   photo regardless of role.
3. **Pipeline truncation (H2)** — the board quietly shows 200/column of thousands; staff act on a
   partial reality.
4. **The moment the app is pointed at the backend (C2+C3)** — login fails, then scan/voice/push/void
   all 404/422.
5. **Japanese + timezone** — labels carry 日本語 (verified in TaskCard/Leads), but JST/GST is a 5h gap
   (not 4h as the log twice says); booking slots and task due dates are stored as naive ISO strings
   (`api.py:1070,2334`) with no tz — a Dubai-entered slot and a Tokyo-viewed slot will disagree.
   Nothing crashes; appointments land in the wrong hour.
6. **iPad-in-a-stand all day** — shared-device login (D20's own open flag) is unresolved; whoever
   logged in Monday is still "who the app thinks you are" Friday.

---

## What I actually verified working (this session)

- `tests/test_decisions.py` **28/28**, `test_permissions.py` **39/39**, `test_contract.py` **28/28** — all green.
- Live backend on :8914 (dev auth), seeded 63 leads / 434 events.
- **Sales SQL isolation**: chiaki@ `/api/leads?page_size=100` → total 10, single owner "Chiaki". Real.
- **Marketing redaction**: marketing@ list → `contact_redacted:true`, `email:None` on non-owned leads. Real.
- **List shape**: `region`, `source`, `owner_name` present at top level (prior-audit fix holds).
- **Media IDOR reproduced**: marketing@ and chiaki@ both HTTP 200 on a non-owned
  `/api/media/business_cards/<uuid>.jpg`; inactive 403; unauth 401.
- **Task patch contract aligned**: backend accepts `{action:'snooze',until}` and
  `{action:'reassign',owner_user_id}` (`api.py:1050,1066`); app sends exactly those
  (`TodayScreen.tsx:55`, `MemberTasksScreen.tsx:105`) with error+retry banners. Prior-audit break fixed.
- **Provider honesty**: GHL sync, AI draft, push test all returned `configured:false` + manual path.
- **Route-string diff** app↔backend for §6/§9/§10 — mismatches in C3 confirmed by direct grep.
- **Config**: `.env` = `USE_MOCKS=1`; `eas.json` has no API URL in any build profile; auth paths stub.

Not run (would need 30k-row fixture / real cloud): H1 at production scale, Supabase JWT path,
timezone end-to-end, EAS production build env resolution.
