# Exceed Box — build contract v1

The single source of truth both the backend and the app are built against. If the two disagree,
this file wins. Decisions behind it: `Master vault/Exceed Real Estate/Exceed Box/Exceed-Box-Build-Decisions.md`.

## Who this is for
The **Exceed Japan office** (Tokyo) plus Dubai desk. ~10–15 people. Internal only, never public.
One place to track every lead and everything each person has to do today.

## Architecture

```
Expo app (iOS/iPadOS)  ──auth──▶  Supabase Auth   (Google SSO @exceed-re.ae + email/password)
        │                              │  issues JWT
        └──────data/logic──────▶  FastAPI (app/)  ──▶  Supabase Postgres
                                   verifies the JWT, owns scoring/dedupe/tasks
```

- **Supabase** = Postgres + Auth. **FastAPI** = business logic already built and tested
  (scoring, decay, dedupe, consent, tasks, escalation). We are not rewriting that into SQL.
- FastAPI verifies Supabase JWTs (`SUPABASE_JWT_SECRET`) and derives the caller's role from
  `app_user`. **No endpoint may read the role from the request body or a header.**
- SQLite stays only as the local test fixture. Postgres is the target.

---

## Roles — four, fixed

| role | who | one-line mandate |
|---|---|---|
| `admin` | Balraj, Teruo | everything, including user management |
| `marketing` | campaign/SNS staff | owns campaigns, imports and content; sees lead data in aggregate |
| `office_manager` | Japan office manager | owns the office's operations — assignment, all tasks, all leads |
| `sales` | agents | own leads and own tasks only |

### Permission matrix — enforced server-side, and the app hides what the role cannot do

| capability | admin | marketing | office_manager | sales |
|---|:--:|:--:|:--:|:--:|
| see own tasks | ✅ | ✅ | ✅ | ✅ |
| see **everyone's** tasks | ✅ | ❌ | ✅ | ❌ |
| see own leads | ✅ | ✅ | ✅ | ✅ |
| see **all** leads | ✅ | ✅ | ✅ | ❌ |
| edit a lead they own | ✅ | ✅ | ✅ | ✅ |
| edit **any** lead | ✅ | ❌ | ✅ | ❌ |
| assign / reassign an owner | ✅ | ❌ | ✅ | ❌ |
| move a lead's pipeline stage | ✅ | ❌ | ✅ | ✅ *(own only)* |
| grant the manual +30 "wants to meet" | ✅ | ❌ | ✅ | ✅ *(own only)* |
| dashboard — company-wide KPIs | ✅ | ✅ | ✅ | ❌ |
| dashboard — **per-rep performance** | ✅ | ❌ | ✅ | ❌ |
| import CSV / create campaigns | ✅ | ✅ | ❌ | ❌ |
| see consent + contact details | ✅ | ❌ *(aggregate only)* | ✅ | ✅ *(own leads)* |
| manage users + roles | ✅ | ❌ | ❌ | ❌ |

**Rules that must hold:**
1. `sales` never receives another rep's lead or task in **any** response — filtered in SQL, not in the app.
2. `marketing` sees lead **counts and behaviour**, never a lead's phone/email, unless they own it.
3. Every write records `actor_user_id` — D4 requires the manual +30 be attributable.
4. A role a caller does not have returns **403**, never an empty 200.

---

## Data model additions to the existing 15 tables

```sql
app_user(id uuid pk, supabase_uid uuid unique, email citext unique, display_name text,
         role text check (role in ('admin','marketing','office_manager','sales')),
         office text,               -- 'tokyo' | 'dubai'
         is_active bool default true, created_at timestamptz)

lead.owner_user_id  uuid null references app_user(id)   -- D14, was missing entirely
audit_log(id, actor_user_id, action, entity, entity_id, before jsonb, after jsonb, at timestamptz)
```

`lead_stage` uses the six D10 stages: `new → nurturing → engaged → meeting_booked → in_negotiation → won`,
plus the four exit states `lost / too_early / unreachable / unsubscribed` (⚠️ **still unratified by
Balraj** — implemented, flagged in the UI as provisional).

---

## API — every route below must exist, be authed, and be role-filtered

Auth: `Authorization: Bearer <supabase jwt>` on everything except `/api/health`, the tracking
pixels (`/o/…`, `/c/…`, `/e`) and `/webhooks/email`.

```
GET    /api/me                        → { id, email, name, role, office, permissions[] }
GET    /api/today                     → the caller's tasks, ordered by urgency (score × lateness)
POST   /api/tasks                     → create (type, owner, due, reason REQUIRED)
PATCH  /api/tasks/{id}                → complete / snooze / reassign / change due
GET    /api/leads?stage=&owner=&q=&region=&purpose=&page=
GET    /api/leads/{id}                → detail incl. timeline, channels, consent, score breakdown
POST   /api/leads                     → create (dedupe on import — D13)
PATCH  /api/leads/{id}                → edit fields the role is allowed to edit
POST   /api/leads/{id}/assign         → { owner_user_id }   (admin/office_manager only)
POST   /api/leads/{id}/stage          → { stage }           (with history row)
POST   /api/leads/{id}/signal         → { type:'wants_meeting', note }  → +30, records actor (D4)
GET    /api/leads/{id}/score          → scoring.explain() — the full why
GET    /api/pipeline                  → kanban buckets, counts + leads per stage
GET    /api/dashboard                 → six KPI tiles + trend + funnel + by-source (D15/D16)
GET    /api/team                      → per-rep accountability (admin/office_manager only)
GET    /api/users                     → admin only
POST   /api/users                     → admin only (invite + role)
PATCH  /api/users/{id}                → admin only (role, active)
```

**Every list endpoint is paginated and returns `{ data, page, page_size, total }`.**
Errors are `{ error: { code, message } }` with correct HTTP status. Never a 200 carrying an error.

---

## App screens — and the role rules for each

| screen | who sees it | notes |
|---|---|---|
| **Login** | everyone | D20/D21: Google primary, email+password secondary. Built. |
| **Today** | everyone | the caller's tasks only. `sales` lands here by default. |
| **Leads** | everyone | `sales` sees only their own; list is filtered server-side. |
| **Lead detail** | everyone with access | timeline, score breakdown, channels, consent, actions |
| **Pipeline** | everyone | `sales` sees only their own cards; drag limited by role |
| **Dashboard** | admin · marketing · office_manager | `sales` never sees the tab at all |
| **Team** | admin · office_manager | per-rep accountability (D12a) |
| **Admin/Users** | admin | role assignment |

**The tab bar itself is role-derived.** A `sales` user has no Dashboard tab, no Team tab, no Admin
tab — not disabled, absent. The server still enforces it independently.

Language: **English primary, Japanese secondary in small grey**, everywhere. The Japan office is the
primary user; every label needs its 日本語.

---

## Definition of done — non-negotiable, this is the acceptance test
Balraj's words: *"I don't want to spend time giving you feedback about this button not working and
layout is messy."*

1. **Every button does something.** No dead controls, no `onPress={() => {}}`, no "coming soon".
   If a feature is out of scope it is not on screen.
2. **Every screen has all four states**: loading, empty, error (with retry), and populated.
3. **Every list**: pull-to-refresh, pagination, and an empty state written in words a human would use.
4. **Every destructive or role-gated action** is either absent for that role or returns a real 403
   the UI shows properly.
5. **Both iPhone and iPad**: no stretched full-width forms on iPad; content columns are capped.
6. **Tests**: existing 28 keep passing; new tests cover the permission matrix — one test per role per
   capability that must be denied. A denial that is not tested does not count as implemented.
7. **Seeded demo data** so every screen is populated on first run.
