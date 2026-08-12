# Exceed Box — backend

The layer that does not exist in the demo.

`exceed-realestate.github.io/exceed-box/` is **layer 1** — the screens. This repo is
**layer 2**, the custom backend on the システム連携 diagram: the data model, the scoring
engine, event tracking, tasks and the accountability view. JAI price that layer at roughly
48 person-days in their ¥8.1M proposal.

Nothing here is deployed. It runs on localhost.

---

## What is built, and what is not

**Built and tested**

| | |
|---|---|
| Schema | 15 tables, every one traceable to a decision in the design log |
| Scoring engine | all 10 rules, decay, threshold crossing, and `explain()` — the *why* behind a score |
| Deduplication | one person, many channels; email / phone / name+company matching |
| Consent | per-channel defaults, upgrade-only, withdrawal is final |
| Tracking | open pixel, click redirect, page view, booking-page view |
| SendGrid webhook | opens, clicks, bounces → unreachable, unsubscribes → hard stop |
| Reply detection | rules layer, then a single AI pass answering four questions |
| Tasks | six fields including a mandatory `reason`; escalation by score × lateness |
| Accountability | per-rep view, idle detection, daily digest |
| API | 16 endpoints, FastAPI |
| Tests | 28, each named for the decision it protects |

**Not built**

No auth. No real SendGrid account, so nothing sends. No AI call — `record_reply()` takes
the verdict as an argument, so the shape is right and the model is not wired. No UI: the
demo's HTML still runs on its own hardcoded arrays and has not been pointed at this API.
No CSV file parser (the wizard's *questions* exist, the reader does not). No GoHighLevel
connection. No Postgres.

---

## Run it

```bash
cd ~/code/exceed-box-app
python3 seed.py                                    # build + fill the database
python3 tests/test_decisions.py                    # 28 tests
python3 -m uvicorn app.api:app --port 8912         # http://127.0.0.1:8912
```

Try it:

```bash
curl -s localhost:8912/api/leads/1/score | python3 -m json.tool   # why is he a 50?
curl -s localhost:8912/api/team | python3 -m json.tool            # who is behind
curl -sI localhost:8912/o/e_7Kq2mB.gif                            # fire an open  → +1
curl -sI "localhost:8912/c/e_7Kq2mB?u=/dubai-cost"                # fire a click  → +3
```

The score moves. That is the difference from the demo, where 92 is typed into the HTML.

---

## Decisions this encodes

Full record: `Master vault/Exceed Real Estate/Exceed Box/Exceed-Box-Build-Decisions.md`.
Referenced inline in the code as D1…D16.

The ones with teeth:

- **D8 — behaviour decays, facts do not.** An open from June is worth less today; being a
  company is not. Behaviour halves at 60 days and is worth nothing at 120, so a lead who
  went quiet drops back under the threshold and re-enters nurture instead of rotting on
  someone's list. *Interpretation flagged in `scoring.py`: this decays each event by its
  own age, not by lead-level silence. Say if you meant the other one.*
- **D13 — duplicates merge.** Morita arrives as a card, a CSV row and a GoHighLevel
  contact and stays one person with three channels. `first_touch` never changes, which is
  what makes the source chart total the bookings tile (D16) instead of the demo's 55-vs-30.
- **D12 — a task is six fields, not a sentence.** `reason` is `NOT NULL` on purpose. Without
  it a rep cannot see why, so they use their gut, which is what killed Salesforce here.
- **D7 — rules before AI.** Free header checks drop the obvious machines; only survivors
  cost anything, and that one pass answers all four questions at once.
- **D4 — a human-granted +30 is attributable, not blocked.** `set_by` is recorded and shown.
- **D15 — expected revenue returns `null`.** It needs stage close-probabilities that nobody
  has supplied. It reports what it is blocked on rather than inventing a plausible number.

---

## Two bugs the tests caught during the build

1. **Phone matching was broken.** "last 11 digits" made `03-1234-5678` (10 digits) and
   `+81 3 1234 5678` (11 digits) different people. Now normalised to national form.
2. **Escalation compared banded levels.** Two different urgencies shared a level, so
   "hot beats cold" wasn't provable. `urgency()` is now exposed separately from the band.

---

## Before this goes anywhere near production

- **Auth.** Every `/api/*` route is open. Fine on localhost, not fine anywhere else.
- **PostgreSQL.** SQLite was chosen so the model could keep moving. Swap before real data.
- **Webhook verification.** `/webhooks/email` trusts its payload. SendGrid signs requests;
  verify the signature or anyone can post fake events and inflate scores.
- **Domain warm-up.** A new sending domain cannot take 10,000 in one go — that is why the
  batches are 10,000 × 3, and it is a hard constraint, not caution.
- **The 25,000 CSV rows have `consent = unknown`.** Deliberately honest. It is also the
  largest legal exposure in the project and should be resolved before the first send.

## Still blocked on Balraj

- Staff roster + roles → lead ownership (D14)
- Lombok and Japan-showroom sub-categories (D9a)
- Stage close-probability percentages → expected revenue (D15)
- Confirm the high-budget threshold with the Japan office chief; AED 2,000,000 is a
  placeholder (D6)
