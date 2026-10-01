# Exceed Box — build checklist (resume point for any session)

Started 2026-09-10. Owner: Claude session a44-e3. Keep this file current — it is
the handover if a session dies mid-build.

## Decisions locked (2026-09-10, from Balraj)

| Thing | Decision |
|---|---|
| Product | Exceed Box. Standalone CRM. GoHighLevel = future scope. |
| Database | Supabase project **`exceed-box`**, ref `mprvfdprioyhxsfjnqtk`, org Spicy Kiwi LLC (Pro), region Tokyo `ap-northeast-1`, compute Micro. +$10/mo. |
| DB connection | Session pooler `aws-0-ap-northeast-1.pooler.supabase.com:5432`, user `postgres.mprvfdprioyhxsfjnqtk`. The direct host `db.<ref>.supabase.co` is IPv6-only and does not resolve here. **`aws-1` is the wrong pooler for this project** (that note in the Constraints Ledger is about the Mumbai eSIM project). |
| Roles | admin / office_manager (= "manager") / sales. `marketing` kept as an optional least-privilege role, not granted by default. |
| Hosting | Not tied to one machine. Provider TBD — build portable, decide at deploy. Fly.io Tokyo is the current recommendation. |
| Domain | Japan office domain still being collected. Build on a temporary hostname; swapping later is a config change + one app rebuild. |
| Staff roster | Deferred. Users are DB rows, addable any time from the admin screen. |
| WhatsApp | Parked by Balraj on 2026-09-08 ("wait for now, tackle later"). |

## Verified baseline (2026-09-10, before any edit)

- `exceed-box-app`: 133 tests pass on SQLite. 32 dirty files, last commit `bac8953`.
- `exceed-box-ios`: `npm run typecheck` clean, `npm run check-contract` 47/47. 19 dirty files, last commit `6e2f5cd`.
- Supabase `exceed-box`: reachable, PostgreSQL 17.6, **0 tables in public**.

## The real gaps (from a full read of both repos, 2026-09-10)

Supersedes the more optimistic notes in FABLE-AUDIT-R4 and the decision log.

### Backend — Postgres port (blocks everything else)
1. `_PgCursor.lastrowid` is hardcoded `None`. 6+ insert-then-use sites break.
2. `datetime('now')` in ~40 live statements — syntax error on PG.
3. TIMESTAMPTZ returns `datetime`; code does `.replace("Z","")` then `fromisoformat` → AttributeError. Kills the scoring engine.
4. `WHERE active=1` / `decays=1` against real BOOLEAN columns.
5. `INSERT OR IGNORE` at 3 live sites.
6. `sqlite3.IntegrityError` caught in `ingest.py` — wrong class on PG, and the transaction is already aborted.
7. Unaliased derived table at `api.py:547` — `/api/leads` 500s.
8. No migration runner, no version table. `db.init()` would run SQLite DDL at PG.
9. Auth is HS256-shared-secret only, no JWKS, and `iss` is never verified.
10. `/webhooks/email` is unsigned and has no event idempotency.

### App
11. `eas.json` sets `EXPO_PUBLIC_USE_MOCKS=1` on **all three** profiles including `production`.
12. `USE_MOCKS` also turns on when `EXPO_PUBLIC_API_URL` is missing — a misconfigured release becomes a silent demo.
13. `EXPO_PUBLIC_DEV_AUTH` is undocumented and absent from `.env.example`.
14. BookingScreen's "What clients see" block renders a fabricated confirmation card.
15. The fieldGuesser regression suite (which encodes the OCR tagline bug) is not wired to any npm script.

### Not built at all
16. No email sending. No SendGrid client, no API key, nothing writes to `sends`.
17. No scheduler/worker of any kind. `scripts/sweep_decay.py` is load-bearing and unscheduled.
18. Google Calendar, push delivery, AI reply classification, GoHighLevel: all honest `configured:false` stubs.

### Already fine — do not "fix"
- The OCR tagline bug from FABLE-AUDIT-R4 **is fixed**; `isTaglineish` rejects it and test Case 8 covers it.
- No screen shows a success toast without an awaited API call. The four suspects are clean.
- i18n is complete: 533 keys in both `en.ts` and `ja.ts`.
- Dashboard metrics are all real SQL. `expected_revenue` is honestly `null` with a reason.

## Work order

- [x] 0. Discovery, baseline test run, Supabase connectivity
- [x] 1. Postgres port: adapter + dialect-neutral SQL edits
- [x] 2. Migration runner + apply 0001–0005 to Supabase + schema parity test
- [x] 3. Postgres integration test suite (CRUD, ids, transactions, dates, concurrency, restart)
- [x] 4. Auth: JWKS/asymmetric verification, issuer check, dev-auth locked out of prod
- [x] 5. App: kill mock fallback in production (Supabase client config still pending — see below)
- [x] 6. Media → Supabase Storage with owner checks preserved
- [x] 7. Email provider + scheduler/worker (built + tested; real sending awaits Balraj's approval and a SendGrid account)
- [ ] 8. Google Calendar + booking
- [x] 9. Scale test at 30k leads
- [x] 10. Deployment package + guides + costs (NOT deployed — no server exists)

## Port strategy (decided 2026-09-10)

Fix the **adapter**, not the 133 tested call sites. Specifically:
- Auto-append `RETURNING id` to INSERTs lacking one; capture into `lastrowid`.
- Translate `datetime('now')` → `now()` and `INSERT OR IGNORE` → `... ON CONFLICT DO NOTHING`.
- Coerce on read: `datetime` → ISO-8601 with `Z`; `bool` → `1`/`0`. Keeps every
  Python date and truth test working unchanged.
- Dialect-neutral source edits only where translation would be unsafe:
  `= 1` → `= TRUE` on real boolean columns (valid in both SQLite ≥3.23 and PG),
  alias the derived table, and catch a dialect-neutral IntegrityError.
- Savepoints around the dedupe path so a caught IntegrityError does not poison
  the rest of the transaction.

## Secrets

Never in this file. See `Master vault/Exceed Real Estate/Exceed Box/🔐 Exceed-Box-Secrets.md`.
Env var names only: `DATABASE_URL`, `SUPABASE_JWT_SECRET`, `SUPABASE_URL`,
`EXCEEDBOX_DEV_AUTH`, `EXCEEDBOX_DECAY_MODE`, `EXCEEDBOX_AUTO_EXIT_STATES`,
`EXCEEDBOX_READ_HEALS_PERSIST`, `EXCEEDBOX_UPLOAD_ROOT`, `EXCEEDBOX_ALLOWED_EMAIL_DOMAINS`.


## Progress log

### 2026-09-10 — steps 1–3 done

**Verified this session, with the command that proves it:**

| Check | Command | Result |
|---|---|---|
| SQLite suite unchanged | `pytest -q` (no DATABASE_URL) | 133 passed, 19 skipped |
| Postgres integration | `pytest tests/test_postgres_integration.py -q` | 19 passed in 2m15s |
| App typecheck | `npm run typecheck` | clean |
| App/backend contract | `npm run check-contract` | 47/47, 0 failures |
| Migrations on Supabase | `db.migrate()` | 0001–0005 applied, re-run returns `[]` |
| Schema parity | table-set diff | 32 tables both sides, no drift |
| Reference data | `scripts/bootstrap_reference.py` | 26 rows, re-run is a no-op |

**What changed**

- `app/db.py` rewritten. psycopg3 (not psycopg2, which was never installed).
  String-literal-aware `?`→`%s`; `datetime('now')`→`now()`; `INSERT OR IGNORE`
  →`ON CONFLICT DO NOTHING`; auto `RETURNING id` for `lastrowid`;
  `OVERRIDING SYSTEM VALUE` where code supplies an id to a GENERATED ALWAYS
  identity; read coercion of datetime→naive-UTC ISO text, bool→1/0, UUID→str,
  Decimal→float, jsonb→JSON text. Plus a migration runner with a
  `schema_migrations` version table, and `con.savepoint()` on both dialects.
- `db.reset()` now **refuses** when DATABASE_URL is set. It is called by
  `seed.py` and by the test suite, and neither should be able to wipe a real
  database.
- Six `active=1` → `active = TRUE` (valid in SQLite ≥3.23 and Postgres).
- Five int-boolean parameters → real Python bools (`api.py`, `auth.py`).
- `api.py` `/api/leads` count: derived table given an alias.
- `ingest.py` dedupe inserts wrapped in `con.savepoint()` and catching
  `db.IntegrityError` instead of `sqlite3.IntegrityError`.
- New `scripts/bootstrap_reference.py` — reference data only, idempotent,
  never touches leads or people. This is what a real deployment runs; `seed.py`
  stays a SQLite-only demo fixture.
- New `tests/test_postgres_integration.py` — 19 tests, opt-in via DATABASE_URL,
  every row tagged and deleted, nothing dropped or truncated.

**Notes for whoever is next**

- The Tokyo pooler costs about a second per connection. The integration tests
  share one connection per module for that reason; a per-test connection took
  9 minutes instead of 2.
- A failed statement aborts the whole Postgres transaction, so the per-test
  fixture rolls back before cleanup. Without it one failure cascades into
  every later test and hides its own cause.
- `tasks.created_by` is CHECK-constrained to `ai|rule|human|system`.

### 2026-09-10 — step 4 done (auth)

**Verified**

| Check | Result |
|---|---|
| SQLite suite | 146 passed, 19 skipped (was 133 — 13 new auth tests) |
| Postgres integration | 19 passed |
| Order independence | passes with the auth file forced last |
| Live JWKS fetch, real project | 1 key, `ES256`, kid `75eca87e…` |

**The finding that mattered.** The exceed-box Supabase project signs with
**ES256** and publishes one key at `/auth/v1/.well-known/jwks.json`. There is no
legacy HS256 secret on it. `app/auth.py` understood only HS256, so it would have
rejected **every real login** — the product could not have been signed into at
all. Checked against the live endpoint, not assumed from docs.

**What changed**

- `app/auth.py` verifies JWKS/asymmetric first (ES256, RS256, EdDSA), then the
  legacy HS256 secret, then dev tokens. A project mid-migration issuing both
  shapes still works. An unreachable JWKS endpoint is a 503, not a 401 — it is
  a server outage, not a bad password.
- **Issuer is now verified.** It was not before, so a validly-signed token from
  *any other Supabase project* was accepted on its signature alone. Both issuer
  and JWKS URL derive from `SUPABASE_URL`, so there is one thing to configure.
- **Dev auth is refused whenever any real verification path is configured**, not
  just the legacy secret. Previously, pointing the service at a real project
  while `EXCEEDBOX_DEV_AUTH` lingered would have left the unauthenticated token
  minter live in production.
- Config derivation extracted into a pure `resolve_auth_config()` so it is
  testable without reloading a module FastAPI has bound route dependencies to.
- `/api/health` now reports which of the three modes is actually live.
- Added `cryptography` (ES256) and `psycopg[binary]` to requirements.

**A latent bug found while doing it — worth knowing about**

`app.db` and `app.auth` resolve configuration once, at import. Whichever test
module pytest imported first therefore decided the database path and the auth
mode for the entire run. Every existing test file set the same variables at its
own top, so the alphabetically-first one silently won and it happened to work.
Adding a file that sorted earlier pointed the whole suite at the **real**
`data/exceedbox.db` — which `db.reset()` deletes. The dev database was not
harmed (still dated Aug 31), but it was one test run away.

Fixed with `tests/conftest.py`, which sets the temp DB path, dev-auth flag and
login domains before any test module is imported, and strips any real Supabase
variables from the environment. New test files can no longer reintroduce it.

**Still not done, not claimed:** no email sending, no scheduler, mocks still on
in the app's production profile, media still on local disk.

**Not yet configured, needs Balraj:** Google sign-in is `false` on the Supabase
project (checked live). D20 wants Google as the primary door, which needs a GCP
OAuth client id and secret pasted into Supabase Auth.

### 2026-09-10 — step 5 (mock fallback killed)

**Verified by building and loading two real production bundles, not by reading code.**

| Build | Result |
|---|---|
| production env + `EXPO_PUBLIC_API_URL` set | real Login screen, **no** demo banner |
| production env, **no** API URL | "This build isn't configured" screen — **not** fabricated leads |
| `npm run verify` (typecheck + contract + release) | passes |
| `npm run test:ocr` | 118 passed, 0 failed |

**What changed**

- `src/api/client.ts`: demo data now needs positive permission —
  `MOCKS_ALLOWED = __DEV__ || EXPO_PUBLIC_ALLOW_MOCKS === '1'`. An unset variable
  inlines as `undefined`, so a release that forgot to configure anything fails
  closed to ConfigErrorScreen. The old dev-convenience fallback (no API URL →
  mocks) still works on the Metro dev server and on dev/preview builds, which is
  why it existed: a TestFlight build once shipped unusable because Xcode's
  bundling phase does not inherit exported shell variables.
- `eas.json`: `EXPO_PUBLIC_ALLOW_MOCKS` on development and preview only.
  **Production's `env` block is now empty** — it previously set
  `EXPO_PUBLIC_USE_MOCKS=1`, so every release binary served invented leads.
- New `scripts/check-release-config.js` + `npm run check-release`: fails the
  build if the production profile sets any mock/dev-auth flag, if the gate is
  removed from `client.ts`, or (with `--env`) if the API URL is missing,
  loopback, or not https.
- `npm run test:ocr` — the field-guesser regression suite already existed and
  encoded the "tagline read as a person's name" bug, but was wired to no script
  and had therefore never run in CI. It passes.
- `npm run verify` chains typecheck + contract + release config.

**Gotcha worth keeping:** `expo export` reuses a cached bundle, so a second
export with different env silently produced a byte-identical file and a
misleading pass. `--clear` is required when testing env inlining. The first
"no API URL" run looked correct and was not.

**Not done in this step:** the app has no Supabase client config yet
(`EXPO_PUBLIC_SUPABASE_URL` / `ANON_KEY`), so real sign-in still cannot be
exercised end to end. That is next, together with media moving to Storage.

**Flag for Balraj, not acted on:** D26 says "the app stays dark", and Login is
dark, but `src/theme.ts` is a light palette (`page: #F4F5F7`, `card: #FFFFFF`)
and `app.json` sets `userInterfaceStyle: "light"`. Nine of twenty screens draw
from that light palette. So the app today is a dark login followed by light
screens. Changing it is a whole-app visual decision and D26 was his call, so it
is surfaced rather than fixed.

### 2026-09-11 — the web app runs against the real stack

**Proved by driving the real UI in a browser, and by 29 HTTP checks against a
running backend with real Supabase tokens. Not by unit tests.**

| Check | Result |
|---|---|
| `scripts/walkthrough.py` (29 checks over real HTTP) | 29 passed, 0 failed |
| Backend suite (SQLite) | 146 passed, 19 skipped |
| Postgres integration | 19 passed |
| Web app signed in as admin, real token | Today / Leads / Lead detail / Dashboard all render live data |

**What now works end to end**
- Sign-in: the app gets a real ES256 token from GoTrue, the backend verifies it
  against JWKS, and `/api/me` returns the right role. Five roles tested.
- Access control, against the live database: unauthenticated 401; manager
  refused `/api/users`; sales refused the company dashboard; an inactive user
  403s on everything; a non-owning rep cannot fetch another rep's card photo.
- Lead intake: created, de-duplicated onto one person by email, and a card scan
  that stores a real image.
- Scoring: the in-person signal moved a lead 0 → 30, attributably, and the
  explanation agrees with the lead detail.
- Pipeline and tasks: stage change persists; a completed task leaves Today.
- Dashboard: six tiles, a 14-day trend, funnel, per-rep table, region and source
  panels. Expected revenue prints its reason instead of a number. Bookings by
  source reconcile with the tile.
- Every integration reports disconnected. Nothing claims a false green.

**Performance bug found and fixed.** `/api/dashboard` issued **57** statements
for one page — 42 of them a 14-day loop at 3 scalar counts per day. Against a
managed Postgres each statement is a network round trip (271 ms measured from
Dubai to the Tokyo pooler), so the endpoint took **14.35 s**. Grouping the trend
into three queries took it to **18 statements / 5.74 s**, and the cost no longer
scales with the window. Output is byte-identical: still 14 points, same dates.
Most of the remaining 5.7 s is distance; co-locating the backend with the
database in Tokyo removes it.

**The gap that matters most, found by testing rather than reading.** There is
**no way to enrol a lead into a sequence**. The sequence editor exists, the
tracking endpoints exist, the scoring rules exist — but nothing writes a `sends`
row, and opens, clicks, replies and bounces all hang off one. So the entire
nurture half of the product is unreachable today. This is the missing middle,
and it is the next thing to build.

**Two things that looked like bugs and were not** — recorded so the next session
does not re-investigate: the dashboard tile animates up from zero, so a
screenshot taken on the first frame shows `0`; and `/api/leads` returns its rows
under `data`, not `items`.

**Also fixed:** the Postgres test file's cleanup only matched its own run, so two
runs that died before teardown left six orphan rows behind. It now sweeps by the
shared `zzz-pgtest-` prefix. Those six were removed.

**Test data now in the database:** two leads prefixed `WALK`, plus six `qa-*`
accounts. Both are obviously non-real and both have a documented removal path
(`scripts/walkthrough.py --clean`, `scripts/provision_test_users.py --delete`).

**Still not done, not claimed:** no scheduler or email delivery, no calendar, no
WhatsApp, no push, media still on local disk rather than Supabase Storage, no
hosting, and the app has not been run at 30,000-lead scale.

**Gotchas worth keeping**
- `expo export` reuses a cached bundle; `--clear` is required when testing which
  env values actually got inlined. A cached bundle produced a false pass.
- React Native Web's pressables do not respond to a plain CDP click on the
  wrapper. Dispatching `pointerdown/mousedown/pointerup/mouseup/click` works.

### 2026-09-12 — items 6, 7 and 9

#### 6. Media moved to private cloud storage
`app/media.py` now has two backends behind one interface: local disk for tests
and a laptop, a **private** Supabase Storage bucket (`exceedbox-media`) for a
deployment. Selected automatically by the presence of a service key.

Bytes are fetched server-side and re-served by FastAPI **after** the existing
owner check. Deliberately no signed URLs: a signed URL is a bearer token for one
file that outlives the permission check that minted it and travels through logs
and chat history. The extra hop keeps exactly one place deciding who sees a
customer's face.

| Check | Result |
|---|---|
| Bucket public? | `public: false`; anonymous, no-key and client-key reads all HTTP 400 |
| Owner (sales A) reads own card | 200, bytes match |
| Other rep (sales B) | **403** |
| Marketing | **403** |
| Admin | 200 |
| No token | 401 |
| Media tests | 19 passed (2 against live storage) |

#### 7. Outbound email — built, tested, deliberately not sending
Migration `0006_outbound.sql`: `nurture_enrolments`, suppression keyed on the
**address** (one person can be three leads), `worker_runs`, and the job queue
carried on `sends` itself so a job and its send can never disagree.

`app/mailer.py` + `scripts/worker.py`. **The default provider is `dryrun` and
delivers nothing.** Real sending needs `EXCEEDBOX_MAIL_PROVIDER=sendgrid` AND a
key; a missing key is a refusal, not a fallback. 26 tests cover: sweep run three
times queues once, worker run twice sends once, crash mid-send releases the job,
a merely-slow worker keeps it, transient failure backs off exponentially rather
than burning every attempt in one pass, permanent failure suppresses the
address, an unsubscribe cancels a job **already queued**, and one bad lead does
not stop the sweep for the other 29,999.

#### 9. Scale at 30,000 leads
Run in a throwaway `bench` schema inside the same database, then
`DROP SCHEMA ... CASCADE`. 30,000 leads, 30,000 identities, 75,099 events,
seeded in 101s. Server-side times from EXPLAIN ANALYZE:

| Query | Server ms |
|---|---|
| leads list, page 200 (deep offset) | 33.9 |
| leads list, page 1, hot first | 21.1 |
| hot leads, score >= 40 | 15.3 |
| dashboard by stage / source / rep | 9-10 |
| search by name | 0.6 |

Nothing needs optimising. Wall-clock was 240-775 ms because this laptop is in
Dubai and the database is in Tokyo; co-located in production that disappears.

### Bugs found and fixed while doing the above

1. 🔴 **A literal `%` inside a SQL string broke every query on Postgres.**
   `db._qmark_to_pyformat` doubled `%` outside quotes but not inside, so
   `LIKE 'zzz%'` raised "only '%s','%b','%t' are allowed as placeholders". Passed
   on SQLite, failed on the deploy target. Found while deleting test rows.
2. 🔴 **Identity counters were behind their tables.** Postgres does not advance
   an identity when a row is inserted with an explicit id, and the reference
   bootstrap does exactly that for singleton config rows. The counter still
   pointed at 1 while a row with id 1 existed, so **the first nurture sequence a
   user created would have died with a duplicate-key error.** Added
   `db.resync_identities()`, called at the end of the bootstrap, plus two
   Postgres regression tests.
3. 🔴 **A literal `1` bound into a boolean column** in the assignment-rules
   insert. "column is of type boolean but expression is of type integer" on
   Postgres, silently fine on SQLite. Same class in two bench scripts.
4. 🟠 **The test suite ran against the live database after `source .env`.**
   `db.reset()` refusing on Postgres turned that into 127 loud failures rather
   than a wiped schema, but `conftest.py` now strips `DATABASE_URL` (preserving
   it for the opt-in file), so plain `pytest` is simply correct.
5. 🟠 **Live storage tests were skipping silently**, which reads as a pass —
   conftest was stripping the very variables they opt in on.

### Gotchas worth keeping

- **Supabase's pooler ignores `?options=-csearch_path%3D...`** — it reports the
  default path and every statement runs against `public`. Only an explicit
  `SET search_path` on the session works, and it needs a `current_schema()`
  check afterwards or 30,000 synthetic rows land in the live tables.
- **`citext` is installed in `public`**, not in the `extensions` schema, so
  `public` must stay on the search path or every migration fails.
- **`expo export` reuses a cached bundle** — `--clear` is required when testing
  what got inlined, or two different configurations produce a byte-identical
  file and a misleading pass.

### Still open

- **8. Booking + Google Calendar** — needs the Google workspace.
- **10. Deploy + guides + costs** — Vultr account exists, no server on it, and
  the API key is locked to a different IP.
- Real email delivery — needs a SendGrid account, a sending subdomain, and an
  explicit go-ahead. Everything up to the provider call is done and tested.

### 2026-09-13 — the eight build items

Tests: **261 offline, 22 Postgres.** Backend live on Supabase, storage private,
mail in dry run.

| # | Item | State |
|---|---|---|
| 1 | Public booking page | **done** — built, walked in a browser |
| 2 | AI reply reading | **done** — needs only the key |
| 3 | Integrations screen | **done** |
| 4 | Website / landing-page enquiries | **done** |
| 5 | Referral form | **done** |
| 6 | Voice transcription | **done** — needs only the key |
| 7 | Push notifications | **done** — needs only a device build |
| 8 | Light/dark mismatch | **NOT done — see below** |

#### 1 · The public booking page
`/book`, no login. Topic → time → details → confirmation, Japanese and English,
cancel and reschedule by an unguessable reference. `app/booking.py` +
`app/booking_page.html`. 23 tests.

Security decisions worth keeping:
- **Slot listings carry times and nothing else.** A stranger may learn that
  Thursday 10:00 is free; they may not learn who works here or how many people
  do. Rep assignment happens server-side and never appears in a public response.
- **Double booking is prevented by a partial unique index**, not by reading the
  calendar before writing. Two people confirming the same slot in the same
  second is the one race that read-then-write always loses. The loser overflows
  to the next free rep instead of seeing an error.
- **The enquiry form never manufactures consent.** `lp_form` carries an
  'explicit' consent default that assumes the checkbox was ticked; when it was
  not, that default is corrected. An unticked form is a lead the sales team may
  call, never someone the sequence may email.

#### 2 · Reading replies
`app/replyai.py`. Rules layer first (bounces, out-of-office, mailer-daemon, both
languages) which runs even with AI off. Then a model answering four booleans and
a quote. 37 tests, of which the injection set is the point:
- the reply body is passed as delimited DATA and never reaches the system prompt
- the schema has no field an instruction could act through — extra keys like
  `score` or `stage` are ignored, not applied
- anything not the exact shape produces NO classification, so an attack that
  talks the model into prose scores zero rather than +90

Corporate buyer is deliberately not an AI decision (D5a).

#### 8 · Why the theme was NOT changed
`src/theme.ts` documents the light palette as deliberate: *"matching the client
reference exactly (design/dashboard/target-dashboard.png + the demo's index.html
:root palette)"*. That is later and more specific evidence than D26's "the app
stays dark", and flipping it would restyle nineteen screens on my reading of
which note wins. Surfaced for Balraj instead.

### Bugs found and fixed

1. 🔴 **The booking confirmation showed the wrong time.** A 09:00 booking
   confirmed as 05:00. The adapter coerces Postgres timestamps to a naive
   string, and a browser parses a naive string as local time. Found by walking
   the page in a real browser, not by a test. Confirmations now always carry an
   offset, with a regression test.
2. 🔴 **Post-booking bookkeeping failed silently.** `tasks.owner_id` is a staff
   id, not an app_user id, and `type` is a closed vocabulary. Getting either
   wrong threw inside a swallowed except, which rolled back the stage move too:
   the booking existed, the lead stayed 'new', nobody had a task.
3. 🟠 **A booking page on a Saturday offered nothing** — the grid showed the
   current calendar week, which by then is entirely in the past. Public view is
   now a rolling window.
4. 🟠 **The push test returned success without contacting anything.** It now
   attempts delivery and reports what the provider said, including every
   blocker rather than the first.

### What is still missing

- Google Calendar, WhatsApp, hosting, real email, the guides and the costed
  estimate.
- Item 8, pending Balraj's ruling on dark versus light.

### 2026-09-14 — deployment package and documentation

**Deployment.** `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`,
`.env.example`, and four scripts: `deploy.sh`, `rollback.sh`, `backup.sh`,
`restore.sh`.

Three containers: api, worker, Caddy for HTTPS. No database container — Supabase
is backed up and patched by someone else, which is the right trade for a 10-15
person tool. API and worker share **one image** so the worker can never run
different scoring rules from the API that displays them.

Verified by actually building and running it, not by reading it:

| Check | Result |
|---|---|
| Image builds | yes |
| Container against real Supabase | healthy; auth `supabase-jwks`, media in the private bucket |
| `/book` from the container | 200, topics listed |
| `/api/devtoken` in a container | **404** |
| Unauthenticated `/api/leads` | **401** |
| `scripts/worker.py --once` in the container | full pass, decay recomputed |
| All four shell scripts | parse cleanly |

Dev auth is blocked three ways — the app refuses it when a real project is
configured, compose forces it off, Caddy 404s the route. One mistake cannot
open it.

`deploy.sh` **refuses to start without a `.env`** and leaves a template instead.
A half-configured deployment that appears to work is worse than one that will
not start.

**Documentation.** `docs/USER-GUIDE.md`, `docs/ADMIN-GUIDE.md`,
`docs/OPERATIONS.md`, `docs/COSTS.md`.

**Costs**, checked against provider pages on 2026-09-14:

| | Per month |
|---|---|
| Pilot | ~$16 |
| Running, 10k emails | ~$48 |
| Peak of a 30k campaign | ~$76 |

Supabase's incremental cost is **$10, not $35** — the $25 base is already paid
for Kiki eSIM. Verified: supabase.com/pricing, fly.io pricing, claude.com/pricing.
**SendGrid could NOT be verified** — its pricing page redirect-loops; that figure
comes from the decision log and is the largest variable line.

### Not done, and not pretended otherwise

- **Never deployed to a real server.** Every check is local or in a container on
  this laptop. `deploy.sh` has never run against a live host.
- **No email has ever been delivered** to anyone. The provider path is tested
  against a recording double.
- **`backup.sh` has never produced a real dump**, because there is no server.
- Backups land beside the application; copying them off-site is not automated,
  because the destination is Balraj's to choose. Biggest gap in that story.
- No calendar, no WhatsApp, no real push, no real transcription.
- The native app has not been rebuilt since these backend changes.
- Item 8 (dark versus light) still waits on a ruling.
