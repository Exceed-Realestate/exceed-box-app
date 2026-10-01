# Exceed Box — running it

Architecture, deployment, backups, and what to do when something breaks.

---

## What it is made of

```
                    ┌──────────────┐
   customers ──────▶│    Caddy     │  HTTPS, certificates, rate limits
   staff     ──────▶│  (container) │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐      ┌──────────────┐
                    │     api      │      │    worker    │
                    │  FastAPI     │      │  every 5 min │
                    │  gunicorn ×2 │      │  same image  │
                    └──────┬───────┘      └──────┬───────┘
                           │                     │
                           └──────────┬──────────┘
                                      ▼
                         ┌────────────────────────┐
                         │  Supabase (Tokyo)      │
                         │  Postgres · Auth       │
                         │  private media bucket  │
                         └────────────────────────┘
```

Three containers on one small server. No database of our own: Supabase is
backed up, patched and monitored by somebody else, which for a 10-15 person
tool is the right trade.

**Production today is not this diagram.** It runs on a private host without
Docker or Caddy — see [Production](#production). The container
setup stays valid for any rented Linux server.

The API and the worker are **the same image**, run with different commands. One
image means the worker can never be running different scoring rules from the
API that displays them — drift nobody notices until the numbers disagree.

| Repository | What it is |
|---|---|
| `~/code/exceed-box-app` | backend, worker, booking page, deployment |
| `~/code/exceed-box-ios` | the app (Expo, iOS and web) |
| `~/code/exceed-box` | the original static demo, reference only |

---

## Production

Live at **https://exceedbox.app** since 2026-09-14. The production host, its
service definitions and its tunnel configuration are kept outside this
repository. Ask the maintainer for access; do not guess at them from here.

### Two traps worth knowing on any host

- **Clock drift.** Token checks allow 30 s of drift
  (`EXCEEDBOX_JWT_LEEWAY_SECONDS`); a host clock running behind made the first
  request after sign-in fail at random.
- **Cloudflare rejects Python's default User-Agent** with `403 error code: 1010`.
  That is not a permissions bug. Scripts must send a browser User-Agent.

### Why speed needs watching

When the server is far from the Tokyo database, every statement is a
round trip of a few hundred milliseconds. What keeps screens usable: a
connection pool (`app/db.py`), a 30-second user cache in `auth.current_user`,
and few statements per screen — run `scripts/query_counts.py` before shipping
a screen.

---

## Deploying to a rented server (Docker)

First time and every time after:

```bash
./deploy/deploy.sh root@<server-ip>
```

It installs Docker if missing, copies the code, **stops if there is no `.env`**
and leaves you a template, applies database migrations, seeds reference data,
then swaps the containers and waits for health.

Migrations run **before** the new containers take traffic, so a request never
reaches code expecting a column that does not exist yet.

Rolling back:

```bash
./deploy/rollback.sh root@<server-ip>
```

That reverts the application only. The database is not rolled back, and does not
need to be: migrations are additive — new tables, new nullable columns — so
older code runs against a newer schema. This is why there is no "down" script to
get wrong at three in the morning.

---

## Configuration

Everything is in `/srv/exceed-box/.env`, mode 600, on the server only. Never in
git, never on a laptop. `.env.example` documents every setting and the
consequence of getting it wrong.

The ones that matter most:

| Setting | If it is wrong |
|---|---|
| `DATABASE_URL` | Nothing works. Use the **session pooler** host, not `db.<ref>.supabase.co` — that stopped resolving |
| `SUPABASE_URL` | Nobody can sign in. Issuer and signing keys are derived from it |
| `SUPABASE_SECRET_KEY` | Customer business cards are silently written to the container's disk and lost on the next deploy |
| `EXCEEDBOX_PUBLIC_BASE_URL` | Emails go out with no unsubscribe link |
| `EXCEEDBOX_MAIL_PROVIDER` | `dryrun` means nothing is delivered. This is the default and it is deliberate |
| `EXCEEDBOX_DEV_AUTH` | Must be `0`. It mints tokens without a password |

Dev auth is blocked three ways: the application refuses to enable it when a real
project is configured, compose forces it off, and Caddy returns 404 for the
route. One mistake cannot open it.

---

## Is it working?

```bash
curl https://<domain>/api/health
```

Reports the truth rather than a reassuring OK: which database dialect, which
auth mode, where media actually lives, whether mail **actually delivers**, how
many messages are queued and failed, and when the worker last ran.

`mail.delivers: false` means the dry-run provider is live and no customer is
receiving anything. That is correct until you deliberately switch it on.

The Integrations screen in the app shows the same facts in words, each with the
exact next action.

---

## The worker

Runs every five minutes inside its own container. Four jobs per pass:

1. **reclaim** — return jobs whose worker died back to the queue
2. **sweep** — queue nurture steps that have come due
3. **send** — hand queued jobs to the mail provider
4. **decay** — recompute scores so behaviour fades

Step 4 is load-bearing, not cosmetic. Without it, team badges and the
score-threshold filter serve stale numbers indefinitely.

```bash
docker compose logs -f worker
```

It handles SIGTERM by finishing the pass it is in, so a deploy never interrupts
a half-written send. That is why it is a loop rather than a cron entry: a cron
job killed mid-run leaves a claimed job that only the reclaim sweep recovers,
fifteen minutes later.

---

## Backups

```bash
./deploy/backup.sh
```

Supabase Pro already takes daily backups with point-in-time recovery. This is
not a duplicate of that. It covers the two things Supabase does not: somebody
deleting rows **on purpose**, and losing the `.env`, which is the only
unrecoverable artefact here.

The script verifies the dump is readable and contains the core tables before
declaring success. A dump nobody has opened is a hope, not a backup.

The media bucket is deliberately **not** dumped. Supabase Storage is replicated,
and a copy of every customer's business card sitting on a VPS is a liability
rather than a backup.

**These dumps live on the same server as the application.** That protects
against a bad import, not against losing the server. Copying them somewhere else
is not automated, because the destination and credentials are Balraj's to
choose. This is the single biggest gap in the backup story.

### Restoring

```bash
./deploy/restore.sh backups/exceedbox-<stamp>.dump '<new DATABASE_URL>'
```

Into a **new** Supabase project, never over a live one — the script refuses if
the target looks like the database currently in use. Restore, repoint
`DATABASE_URL`, redeploy, check a lead you recognise. The old project stays
untouched until somebody is sure.

---

## When something breaks

**Nobody can sign in.** Check `auth` in `/api/health`. If it says `none`,
`SUPABASE_URL` is missing. If tokens are rejected, the Supabase project may have
rotated its signing key — the app picks up a new key by itself, so a persistent
failure means the URL is wrong or the project is paused.

**Email is not going out.** `mail.delivers` is almost certainly `false`, which
is the default. If it is `true` and nothing arrives, check `mail.failed` and
then `sends.last_error` in the database. A worker that has not run recently is
the other candidate.

**The booking page shows no times.** No salesperson is assigned to any meeting
type. It is not a bug; the page correctly offers nothing rather than slots
nobody can take.

**Business card photos 404.** `SUPABASE_SECRET_KEY` is missing, so media went to
the container disk and the container has since been replaced. The database rows
survive; the files do not.

**Everything is slow.** On the production host, first suspect the number of statements a
screen runs (`scripts/query_counts.py`) — each costs ~0.33 s there. Then
Supabase compute. At 30,000 leads every query measured under 34 ms
server-side, so slowness is round trips or compute size, not the queries.

**The first request after signing in fails with invalid_token.** The server
clock is further behind than the 30 s leeway. Check with `sntp time.apple.com`.

---

## Verified, and not

Measured on 2026-09-14 unless stated.

| | |
|---|---|
| Backend test suite | 298 passed, 24 skipped (local and on the production host) |
| Live at exceedbox.app on the production host | yes — staff app at `/`, API, `/book` |
| Every screen's API, as each of 6 QA roles | yes — right screens open, the rest 403, inactive account locked out (`scripts/live_walk.py`) |
| Against real Supabase Postgres | 22 passed |
| Docker image builds | yes |
| Container runs against real Supabase | yes — auth via JWKS, media in the private bucket, database connected |
| Public booking page from the container | yes |
| Dev token route blocked in a container | yes, 404 |
| Unauthenticated API refused | yes, 401 |
| Worker runs in the container | yes, one full pass |
| 30,000 leads | slowest query 34 ms server-side |
| Media access control | owner 200, other rep 403, marketing 403, admin 200, no token 401 |
| Storage bucket private | anonymous, no-key and client-key reads all refused |

**Not verified, and worth saying plainly:**

- **`deploy.sh` (Docker) has never run against a live host.** Production is
  on the production host instead; the container rows above were checked on a laptop.
- **Screens are still slow on the production host:** 2–10 s per screen after the fixes on
  2026-09-14 (Dashboard is the slowest). Usable, not fast.
- **No email has ever been delivered.** The provider path is tested against a
  recording double, never against SendGrid.
- **The backup script has not been run against a real dump** — it was written
  for the Docker server and has not been adapted to the production host yet.
- **No real device push, no real transcription, no calendar.**
- The native app has not been rebuilt since these backend changes.
