# FABLE-AUDIT-R4 — Exceed Box, round 4 (confirmation pass)

Date: 2026-08-19 · Auditor: Fable 5 (report only — nothing changed but this file).
Backend run live on :8919 (dev auth, seeded DB; poisoned rows restored via `seed.py` after).
Field-guesser probes run against the compiled `.test-build` (deleted after).

## Verdict

**All three round-3 LOW fixes are real; nothing they touched broke; the one thing left is a
narrower rerun of NEW-1's own residual — everything else actionable is external or a ruling.**

## Confirmation table

| Item | Verdict | Evidence |
|---|---|---|
| **NEW-1** name fallback needs positional evidence | **Real — closes the R3 repro; residual narrowed, not eliminated** (see LOW-1) | R3's exact card ("Smart Living" as last remaining bottom block) → `name` blank. Classic mid-card names still fill (Case 1/6 + probe). JP furigana path is position-independent — a kana-paired 田中 太郎 at y=0.68 still fills at 0.76. |
| **NEW-2** NaN-safe confidence gate | **Real and complete** | `!(confidence >= MIN_CONFIDENCE)` clears NaN. Probed `confidence: undefined` on a phone block and `NaN` on an otherwise-winning name block → both blank. R3's repro (value surfaced with `confidence: null`) no longer reproducible. |
| **NEW-3** `fresh_score()` no longer writes on read | **Real and complete — no endpoint serves a stale number** | Live: lead 1 poisoned to stored `score=999, ts=2020-01-01`. `/api/leads`, `/api/pipeline`, `/api/leads/1`, `/api/leads/1/score`, `/api/dashboard` hot_leads, and the embedded task rows on detail **all served 52** (= `explain()`), while the stored row stayed `999|2020-01-01` and `score_threshold_crossings` gained no row. `EXCEEDBOX_READ_HEALS_PERSIST=1` verified on a DB copy: same 52 served, column persisted with fresh timestamp. `/api/today` covered by test_fable_fixes (stale-lead `lead_score` asserted = truth). |

**Legit-card cost of NEW-1, checked as asked:** the positional rule declines three legit shapes it
used to fill — a single-line card (just a name), a two-block company+name card (name is the final
block), and a name at y ≥ 0.6. All come back **blank, never wrong** — the safe direction under the
"never invent" rule, and `name` is a required field the rep looks at anyway. JP cards are unaffected
(furigana pairing has no position gate). I call this accepted degradation, not a break — but it is a
real behaviour change on minimal EN cards.

## Test counts

- Backend `tests/`: **123 passed** (c1_media 9 · contract 28 · decisions 28 · fable_fixes 18 · permissions 40).
- Field-guesser suite (ios repo): **118 passed, 0 failed** (incl. the N5/N6 regression cases).
- DB reseeded after probes; `.test-build` deleted.

## New findings

### LOW

**LOW-1 · The NEW-1 residual survives in one narrower shape: a mid-card Title-Case tagline.**
`fieldGuesser.ts:265-279`. The position gate (y < 0.6, not final) catches a slogan at the bottom —
but a tagline sitting directly under the logo (a common layout) passes both: probed
`[ACME Corp. 0.05 / "Smart Living" 0.13 / email / phone / address]` → `name = "Smart Living"` at
0.57, above the fill threshold. Position is evidence of *where* a line is, not of *what* it names —
so this mechanism cannot fully close the case R3 flagged. Same blast radius as R3 judged LOW.
R3's original recommendation stands as the complete fix: drop the no-furigana fallback entirely, or
cap its confidence below 0.5 so it never auto-fills.
Sub-note (latent, unreachable today): the "not the final block" guard compares the **input** index
(`only.index < ordered.length - 1`) rather than the sorted position — an out-of-visual-order caller
defeats it (probed). The device path (`recognizeText`) returns ordered blocks, so not live; the
robust form is `ordered[ordered.length - 1].index !== only.index`.

**LOW-2 (consequence of NEW-3, accepted trade-off — deployment note, not a bug).** With persist off,
read traffic no longer heals the stored column at all, so the two places that still read it raw —
`team_status`'s aggregate badges (the documented R3 exception) and `/api/scoring/model`'s
`l.score >= thr` filter (LOW-4 carried) — stay stale until `sweep_decay` runs. Scheduling
`scripts/sweep_decay.py` (already the R3 to-do) is now load-bearing rather than nice-to-have.
Also: a stale row is now recomputed on *every* read rather than once — bounded by page size, fine at
this scale. `tasks.py:263`'s comment ("kept reasonably fresh … by every other read path self-healing")
is now slightly out of date — doc drift only.

## Anything actionable not blocked on an external dependency?

**One item: LOW-1** — a two-line change in the ios repo's fallback block (cap its confidence < 0.5 or
delete it), which finally retires the invented-name class for good. Nothing else. LOW-2 is the
already-known "schedule the sweep" deployment step.

## Blocked on external (unchanged from R3 — the remaining work to hand back)

- **Supabase project** — real JWT round-trip, signup/domain config, `email_verified` semantics, real invites, end-to-end `sub` binding.
- **SendGrid** — outbound sends + `/webhooks/email` producer.
- **OCR provider** — server-side card extraction (on-device Vision needs a physical device to exercise fully).
- **Transcription provider** — voice-note transcripts.
- **GoHighLevel** — no credentials; sync honestly stubbed.
- **Google Calendar** — `/api/calendar` reads own DB only.
- **APNs/FCM** — push stubbed; notify-rep degrades to a task.
- **Public domain + hosting** — booking page.
- **Scale** — behaviour at 30k rows (63 seeded here).
- **Deployment step** — schedule `scripts/sweep_decay.py` (cron/launchd).
- **Balraj's unmade rulings** — D8 decay mode, D15 close-probabilities, D17 sequence exits, H4 auto exit-states, audio-memo contact-class.
