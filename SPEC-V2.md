# Exceed Box — SPEC v2: everything still missing from the OneBox/Exceed Box demo

Extends `SPEC.md` (still authoritative for auth, the four roles, the permission matrix and the
existing 7 screens). This file covers **every remaining feature in the demo** at
`~/code/exceed-box/index.html` (live: `exceed-realestate.github.io/exceed-box/`).

Balraj: *"can you build all of it and then also check with OneBox and Exceed Box if there is anything
missing. make sure to have all — like uploading business card there was a feature right?"*

## Coverage audit — what the demo has vs what exists

| demo view | status |
|---|---|
| dashboard · today · leads · kanban | ✅ built |
| **tracking** 反応検知・興味スコアリング | ❌ build now |
| **nurture** ステップメール | ❌ build now |
| **sns** SNS企画・コンテンツ導線 | ❌ build now |
| **booking** 予約フロー | ❌ build now |
| **assign** 自動アサイン・営業カレンダー | ❌ build now |
| **arch** システム連携 | ❌ build now (reference screen) |

Plus these features, which live inside views and were all missed:
**名刺スキャン** card scan · **CSV/Excel 取込** · **GoHighLevel 連携/今すぐ同期** ·
**✨ AI返信案** reply drafting · **🎤 音声メモ** · **🔔 push notifications** ·
**lead drawer actions** (営業に通知 / 来店対応する / 視察を案内する / 連絡日を設定 / フォローする) ·
**解説モード / Explain mode**.

---

## Roles — apply the SPEC.md matrix to every new screen

| screen | admin | marketing | office_manager | sales |
|---|:--:|:--:|:--:|:--:|
| Tracking (activity feed) | ✅ all | ✅ all | ✅ all | own leads only |
| Nurture (sequences) | ✅ | ✅ edit | view | ❌ no tab |
| SNS | ✅ | ✅ edit | view | ❌ no tab |
| Booking settings | ✅ | ❌ | ✅ | ❌ |
| Assignment rules | ✅ | ❌ | ✅ | ❌ |
| Card scan | ✅ | ✅ | ✅ | ✅ |
| CSV import | ✅ | ✅ | ❌ | ❌ |
| Integrations (arch) | ✅ | view | view | ❌ |

**Marketing still never sees contact details** for leads it does not own — that rule holds on every
new surface, including the activity feed and campaign recipient lists.

---

## 1 · Tracking — 反応検知・興味スコアリング
The scoring engine already exists (`app/scoring.py`, all 10 rules + decay + `explain()`). This is the
screen it never had.

- **Live activity feed** — reverse-chronological events: open / click / page view / booking-page view
  / reply / manual +30. Each row: lead name, event, points delta, time-ago, and the campaign or page.
  Role-filtered in SQL.
- **Scoring model panel** — the 10 rules with current point values, split into
  *behaviour (fades)* and *facts (permanent)* per D8, and the decay curve stated in words:
  halves at 60 days, zero at 120.
- **Threshold marker** — the 40-point line, and how many leads crossed it this week.
- Backend: `GET /api/activity?page=` (paginated, role-filtered) and `GET /api/scoring/model`.

## 2 · Nurture — ステップメール（自動ナーチャリング）
- **Sequence list**, and a **7-step sequence detail** (シーケンス：ドバイ・ロンボク再活性).
  Per step: number, send offset (day/week), subject, the *question it is really asking* (D9 — each
  mail is a question in disguise), and its stats (sent / opened / clicked).
- **Editorial rule shown on screen** — *not「ドバイの不動産を買いませんか？」but「ドバイは意外と住みやすい」*.
- **Exit conditions visible per sequence** (D17): STOP = unsubscribed · bounced.
  PAUSE = replied · booked · score ≥ 40. DORMANT = finished step 7 with no reaction.
  ⚠️ These are **proposed, not ratified** — label them `provisional` in the UI exactly as the exit
  states already are.
- **Nobody ever receives the same email twice** — enforced against the `sends` table in code.
- Backend: `GET /api/sequences`, `GET /api/sequences/{id}`, `PATCH /api/sequences/{id}/steps/{n}`,
  `GET /api/sequences/{id}/stats`.
- ⚠️ **Nothing actually sends** — no SendGrid account exists. Every send action must say so plainly
  rather than reporting success. Do not fake a send.

## 3 · SNS — SNS企画・コンテンツ導線
- **Four content patterns** as cards, verbatim from the demo:
  A ドバイの思い込みを壊す生活情報 · B ノマド・経営者向け移住導線 ·
  C ロンボク視察・開発ストーリー · D 海外不動産の失敗回避・相談導線
- **The funnel diagram**, as a real component:
  `SNS投稿 → LP / 無料資料 → メール取得 → ステップメール → 相談予約`, with the count at each step.
- Each pattern links to the leads it produced (`source = sns`).
- Backend: `GET /api/sns/patterns`, `GET /api/sns/funnel`.

## 4 · Booking — 予約フロー
Two distinct things; build both.
- **Preview of what the client sees** — the 3-step flow (①②③) and the完了 confirmation, rendered
  read-only inside the app so staff can see what is being sent out.
- **Booking settings** — available slots, meeting types (無料30分相談 / 視察 / 来店), which reps take
  which type, and the JST↔GST 5-hour gap stated explicitly (Tokyo UTC+9,
  Dubai UTC+4 — this file said 4 hours before FABLE-AUDIT.md's timezone
  finding was fixed; corrected here so the spec and the code agree).
- Backend: `GET/PATCH /api/booking/settings`, `GET /api/booking/slots`.
- ⚠️ The **public-facing booking page is out of scope** — it needs its own domain and public hosting.
  Say so on the screen rather than implying it is live.

## 5 · Assign — 自動アサイン・営業カレンダー
- **Rule list** (振り分けルール): by topic/region/language → rep, in priority order, editable, with an
  explicit fallback rep. Show which rule would fire for a sample lead.
- **Rep calendar week view** (カレンダー — チアキ（今週）) showing that rep's booked meetings.
- Backend: `GET/PUT /api/assignment/rules`, `GET /api/calendar?user=&week=`.
- ⚠️ **No Google Calendar connection exists.** The week view reads bookings from our own DB. Label the
  external-sync state honestly.

## 6 · Card scan — 名刺スキャン (AI-OCR)
The feature Balraj specifically asked about. D11 marked it *"I have a full plan"* and deferred it.
- **Capture** with `expo-camera` (or pick from library), keep the image, attach it to the lead.
- **Field extraction**: name / 読み / company / title / email / phone / address.
- 🔴 **No OCR provider is wired.** Build the flow so it is genuinely useful today:
  capture → **editable form pre-filled with whatever we can extract, empty if we cannot** → confirm →
  creates the lead through the existing dedupe/merge path (D13) with `channel = business_card`.
  The screen must state plainly when automatic extraction is unavailable and let the person type it.
  **Never show invented field values.**
- Backend: `POST /api/leads/scan` (multipart: image + optional parsed fields) → runs dedupe, returns
  the canonical `LeadDetail`. Store the image path on the lead.
- This is the **showroom iPad** flow from D11b#3 — put the consent checkbox on this form, since that
  is the one moment we can capture explicit, timestamped consent.

## 7 · Import — CSV / Excel 取込
D11 ⭐: *"the importer must interrogate the file on upload"* — an interactive wizard, not a drop-zone.
- Pick file → parse headers → **ask what each ambiguous column means** → preview the first rows with
  the mapping applied → show how many are new vs duplicates (dedupe runs **on import**, D13) → confirm.
- Consent state must be set explicitly per import; default `unknown` and say so — the 25,000 rows with
  `consent = unknown` are the largest legal exposure in the project.
- Backend: `POST /api/import/analyze` (returns columns + questions), `POST /api/import/commit`.

## 8 · GoHighLevel — 連携 / 今すぐ同期
- Connection status, last sync time, record counts, and a **Sync now** button.
- **Read-only until an audit and a conflict rule exist** (D11) — the UI must say it is read-only.
- No credentials exist yet. Show a clear "not connected" state with what is needed; do not fake a sync.
- Backend: `GET /api/integrations`, `POST /api/integrations/gohighlevel/sync`.

## 9 · AI reply — ✨ AI返信案
- On a lead with an inbound reply: show the reply, an AI-drafted response, and
  **承認して送信 (approve and send)** — human approval always required.
- The four-question classifier (D7) result shown as chips: human? · wants to meet? · partnership? ·
  budget? — with a one-tap **"this wasn't real"** that removes the points (D7 requires this).
- 🔴 **No model is wired** (`record_reply()` takes the verdict as an argument). The draft area must say
  the model is not connected and allow a typed reply. Never display a fabricated draft as if generated.
- Backend: `GET /api/leads/{id}/replies`, `POST /api/replies/{id}/draft`, `POST /api/replies/{id}/send`.

## 10 · Smaller items from the demo — all required
- **🎤 音声メモ** — record a voice note against a lead (`expo-audio`), store the file, list notes with
  duration and author. Transcription is not wired; do not pretend otherwise.
- **🔔 Push notifications** — real registration via `expo-notifications`, a settings screen for which
  events notify (D12b escalation level 2–3 notifies a manager), and a **send test notification**
  button that genuinely sends one. If push credentials are missing, say so.
- **Lead drawer actions** — 営業に通知 (notify rep) · 来店対応する (showroom visit) · 視察を案内する
  (site inspection) · 連絡日を設定 (set contact date) · フォローする (follow). Each creates a real task
  or event; none may be decorative.
- **解説モード / Explain mode** — the click-to-explain layer Balraj shipped on the demo (216 elements,
  EN/JA). Port it: a toggle that makes any element tappable to explain what it is and where its number
  comes from. This is his own addition and is a genuine differentiator for training the Japan office.
- **Integrations / arch screen** — the システム連携 reference: which external tools connect
  (GoHighLevel, HubSpot, Gmail, Google Calendar, WhatsApp, Google Drive), each with a real
  connected/not-connected state. Not a picture — a live status board.

---

---

## 11 · iPadOS — treat it as a first-class platform, not a stretched phone
Balraj: *"make sure it works on iPad as well, make use of iPadOS capability fully."* The Japan
showroom device (D11b#3) is an iPad in a stand, in landscape, used by whoever walks up to it.

**Navigation changes shape.** On a regular-width iPad the bottom tab bar is wrong.
- Use a **permanent left sidebar** (~260pt) listing the same role-derived destinations, with the
  Exceed wordmark at the top and the signed-in user at the bottom. Bottom tabs stay on iPhone and on
  compact widths (Slide Over, narrow Split View).
- Drive it off width class at runtime, not off `Platform.isPad` — the app must re-flow *live* when the
  user resizes a Split View or moves through Stage Manager. Test at 1/3, 1/2 and 2/3 widths.

**Master–detail everywhere it earns it.** Leads already does this; extend the same pattern to
Pipeline (board + selected card), Team (roster + that rep's tasks), Tracking (feed + selected lead)
and Nurture (sequence list + step detail). Selection persists when the pane resizes.

**Hardware keyboard** — a showroom iPad usually has one.
- `⌘K` global search · `⌘1…⌘6` jump to destination · `⌘N` new lead · `⌘F` focus filter ·
  `Esc` closes any sheet · `↑/↓` move selection in a list · `↵` opens it.
- Visible focus rings, a sane tab order through every form, and `⌘↵` to submit.

**Pointer and trackpad** — real hover states on rows, cards and buttons; correct pointer cursors.

**Drag and drop** — the one place it genuinely beats tapping:
- drag a lead card between Pipeline columns to change stage (role-gated exactly as the picker is),
- drag a lead onto a rep in Team to reassign.
Keep the existing tap→picker path as well; drag is an addition, never the only way.

**Context menus** — long-press / right-click on a lead row or pipeline card for the same actions
available in the drawer.

**Layout discipline** — content columns still cap (never a 1366pt-wide input); multi-column grids on
Dashboard; the card-scan and import flows use the extra space rather than centring a phone form in an
ocean of charcoal.

**Both orientations, all sizes** — iPad mini through 13", portrait and landscape, plus Slide Over.
Nothing may be clipped or unreachable at any of them.

---

## Rules that apply to everything here
1. **No dead controls, no fake success.** Where a provider is missing (SendGrid, OCR, the AI model,
   GoHighLevel, Google Calendar, push), the UI states it plainly and offers the manual path.
   This is the client's explicit acceptance bar.
2. **Every state**: loading (skeleton) · empty (human words) · error (with working retry) · populated.
3. **English primary, small grey Japanese** on every label. Terse — he asked for less text.
4. **iPad**: capped content columns, no stretched forms, works in both orientations.
5. **Role gating in SQL server-side**, mirrored in the UI. New tabs appear only for permitted roles.
6. **Contract tests** extended to cover every new endpoint, so app and backend cannot drift again.
7. Visual language per `exceed-box-ios/design/UI-DIRECTION.md`; `LoginScreen.tsx` stays locked.
