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
| Schema | 18 tables (15 original + `app_user`, `lead_stage_history`, `audit_log`), every one traceable to a decision |
| Scoring engine | all 10 rules, decay, threshold crossing, and `explain()` — the *why* behind a score |
| Deduplication | one person, many channels; email / phone / name+company matching |
| Consent | per-channel defaults, upgrade-only, withdrawal is final |
| Tracking | open pixel, click redirect, page view, booking-page view |
| SendGrid webhook | opens, clicks, bounces → unreachable, unsubscribes → hard stop |
| Reply detection | rules layer, then a single AI pass answering four questions |
| Tasks | six fields including a mandatory `reason`; escalation by score × lateness |
| Accountability | per-rep view, idle detection, daily digest |
| **Auth** | Supabase JWT (HS256) verification, `app_user` identity, fail-closed auto-provisioning, dev-token fallback for local/tests (`app/auth.py`, `app/devauth.py`) |
| **RBAC** | the SPEC.md permission matrix — four roles, filtered in SQL, real 403s, contact-detail redaction server-side |
| **Postgres** | `migrations/0001_init.sql` — full DDL port, runnable via `supabase db push` or `psql` |
| API | SPEC.md's full route list — `/api/me`, `/api/today`, leads CRUD + assign/stage/signal/score, `/api/pipeline`, `/api/dashboard`, `/api/team`, `/api/users` — every list endpoint paginated `{data,page,page_size,total}` |
| Tests | 51 (28 decision tests + 23 permission-matrix/pagination/redaction tests) |

**Not built**

No real SendGrid account, so nothing sends. No AI call — `record_reply()` takes
the verdict as an argument, so the shape is right and the model is not wired. No UI: the
demo's HTML still runs on its own hardcoded arrays and has not been pointed at this API
(`exceed-box-ios` is the real client and is a separate repo/agent).
No CSV file parser (the wizard's *questions* exist, the reader does not). No GoHighLevel
connection. Postgres is provisioned (`migrations/`) but never connected to in this
sandbox — no `psycopg2` install, no live Supabase project to point at; SQLite is what the
tests and `python3 seed.py` actually exercise.

---

## Run it

```bash
cd ~/code/exceed-box-app
python3 seed.py                                    # build + fill the database
python3 tests/test_decisions.py                    # 28 decision tests
python3 tests/test_permissions.py                  # 23 auth/RBAC tests
EXCEEDBOX_DEV_AUTH=1 python3 -m uvicorn app.api:app --port 8912   # http://127.0.0.1:8912
```

Every `/api/*` route now needs `Authorization: Bearer <token>` except `/api/health`.
`EXCEEDBOX_DEV_AUTH=1` (with no `SUPABASE_JWT_SECRET` set) turns on `/api/devtoken`, a
local dev-token minter — it 404s otherwise, so it does nothing in anything resembling
production. In real Supabase, don't set `EXCEEDBOX_DEV_AUTH`; set `SUPABASE_JWT_SECRET`
instead and the app verifies real Supabase JWTs.

Try it:

```bash
TOKEN=$(curl -s "localhost:8912/api/devtoken?email=balraj@exceed-re.ae" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -s localhost:8912/api/me -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s localhost:8912/api/leads/1/score -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s localhost:8912/api/team -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -sI localhost:8912/o/e_7Kq2mB.gif                            # fire an open  → +1, still unauth
curl -sI "localhost:8912/c/e_7Kq2mB?u=/dubai-cost"                # fire a click  → +3, still unauth
```

The score moves. That is the difference from the demo, where 92 is typed into the HTML.
`seed.py`'s output prints the full login roster (all four roles, one deliberately
inactive) and this same recipe.

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

## Auth + RBAC — how the pieces fit

- **`app_user`** (SPEC.md) is who can log in and what role they hold — separate from
  **`staff`**, the older six-name roster `tasks.py`/`scoring.py` were built against before
  login existed. `app_user.staff_id` bridges the two: the first time a login needs to act
  as a "staff" actor (own a task, be `set_by` on a manual +30), `auth.ensure_staff_bridge()`
  creates the staff row once and remembers it. Nothing in `tasks.py`, `scoring.py` or the
  28 decision tests changed to make this work.
- **`leads.owner_user_id`** (D14, new) is what RBAC filters on — `leads.owner_id` (→
  `staff`) stays too, for the older accountability view, and the two are kept in step by
  the assignment code rather than one replacing the other.
- **Every capability check lives in one place**, `app/auth.py`'s `ROLE_CAPS` — the SPEC.md
  matrix, verbatim, as a dict. `app/api.py` only ever calls `auth.require(user, "cap")` or
  checks per-row ownership; it never re-derives policy.
- **Redaction is server-side and per-lead**, not per-role: `marketing` sees full contact
  details on the handful of leads they happen to own, and nothing on any other lead —
  tested in `tests/test_permissions.py::test_matrix_contact_details_redaction`.
- **The `@exceed-re.ae` domain lock (D20) is enforced server-side, not left to Supabase
  project config** (FABLE-AUDIT-R2.md N1). `EXCEEDBOX_ALLOWED_EMAIL_DOMAINS`
  (comma-separated, default `exceed-re.ae`) is checked in `current_user()` on every token,
  before the database is even touched — an out-of-domain token gets a real 403
  (`domain_not_allowed`), never an auto-provisioned row. Accounts are identified by the
  token's `sub`, never by `email` alone: `email` only ever adopts a `sub` onto a row that
  has never had one (a real first login, recorded in `audit_log`); a row already bound to a
  *different* `sub` is a 403 (`account_conflict`), not a silent takeover. `aud` is verified
  too (`EXCEEDBOX_SUPABASE_AUD`, default `authenticated`) — see `tests/test_c1_media_
  security.py`'s `test_n1_*`/`test_n8_*` for the live proof, including the takeover attempt
  failing.

---

## Two bugs the tests caught during the build

1. **Phone matching was broken.** "last 11 digits" made `03-1234-5678` (10 digits) and
   `+81 3 1234 5678` (11 digits) different people. Now normalised to national form.
2. **Escalation compared banded levels.** Two different urgencies shared a level, so
   "hot beats cold" wasn't provable. `urgency()` is now exposed separately from the band.

---

## Before this goes anywhere near production

- **Point it at real Supabase.** Set `SUPABASE_JWT_SECRET` and `DATABASE_URL`, run
  `migrations/0001_init.sql`, and never set `EXCEEDBOX_DEV_AUTH` — it is gated so a real
  `SUPABASE_JWT_SECRET` overrides it, but the honest state is "not deployed anywhere yet",
  so this has never been run against a live Postgres.
- **`psycopg2-binary` is not installed.** `app/db.py`'s Postgres path (placeholder
  translation, row wrapper) is written but only exercised by inspection, not by a live
  connection — there is no Postgres in this sandbox to test it against.
- **Webhook verification.** `/webhooks/email` trusts its payload. SendGrid signs requests;
  verify the signature or anyone can post fake events and inflate scores.
- **Domain warm-up.** A new sending domain cannot take 10,000 in one go — that is why the
  batches are 10,000 × 3, and it is a hard constraint, not caution.
- **The 25,000 CSV rows have `consent = unknown`.** Deliberately honest. It is also the
  largest legal exposure in the project and should be resolved before the first send.
- **Schedule `scripts/sweep_decay.py`.** It is the bulk half of score materialization
  (FABLE-AUDIT-R2.md N3) — pure time-based decay (D8) only reconciles `leads.score` when
  this runs; every read path self-heals the rows it actually returns (bounded to a page,
  see N3 below), but a lead nobody reads or writes for a long stretch only gets fixed by
  the sweep. Not wired to a scheduler by this repo:
  ```
  # crontab -e
  17 3 * * * cd /path/to/exceed-box-app && python3 scripts/sweep_decay.py
  ```
  or a `launchd` plist calling the same command on the same cadence.

### Round-2 audit fixes (FABLE-AUDIT-R2.md) — new env vars

| var | default | what it does |
|---|---|---|
| `EXCEEDBOX_ALLOWED_EMAIL_DOMAINS` | `exceed-re.ae` | comma-separated login domain allow-list (N1), checked server-side on every token |
| `EXCEEDBOX_SUPABASE_AUD` | `authenticated` | expected JWT `aud` claim, now actually verified (N8) |
| `EXCEEDBOX_SCORE_STALE_SECONDS` | `300` | how old `leads.score_updated_at` may get before a read self-heals it via `scoring.fresh_score()` (N3) |

N3's self-heal is deliberately bounded to the rows an endpoint actually returns (a list
page, a pipeline column, one lead's own tasks) — `tasks.team_status()` (`/api/team`,
`/api/dashboard`'s `by_rep`) is the one exception, kept on the raw materialized column on
purpose, because it is not paginated and must touch every open task company-wide; see the
comment at its `sc = t["lead_score"]` line for why self-healing there would silently
reintroduce H1's original per-task `scoring.score()` storm at scale.

N4's `score_threshold_crossings` table (`scripts/bench_n4.py` has the 30k-lead timing) is
append-only and only ever written by `scoring.recompute_lead()` — nothing prunes it. Fine
at this scale; revisit if it ever needs to be bounded (e.g. a rolling 90-day window) before
a real multi-year deployment.

## Still blocked on Balraj

- Staff roster + roles → lead ownership (D14)
- Lombok and Japan-showroom sub-categories (D9a)
- Stage close-probability percentages → expected revenue (D15)
- Confirm the high-budget threshold with the Japan office chief; AED 2,000,000 is a
  placeholder (D6)
