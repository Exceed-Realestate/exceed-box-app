-- ═══════════════════════════════════════════════════════════════════════════
-- EXCEED BOX — schema
-- Every table here encodes a decision from Exceed-Box-Build-Decisions.md.
-- Decision references (D1..D16) are noted inline so the two stay in step.
--
-- SQLite for now. Deliberately: zero setup, one file, easy to throw away while
-- the model is still moving. Nothing here is SQLite-specific enough to make the
-- move to PostgreSQL painful — that swap is a day, and it should happen before
-- anything real is loaded.
-- ═══════════════════════════════════════════════════════════════════════════

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- PEOPLE (staff). Owners of leads and tasks.
-- D14: ownership accepted; the real roster + roles are still owed by Balraj,
-- so this is deliberately thin and seeded with the four names in the demo.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staff (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  name_en     TEXT,
  role        TEXT,                       -- free text until the roster arrives
  access      TEXT NOT NULL DEFAULT 'agent'
              CHECK (access IN ('agent','manager','ceo','admin')),
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- LEADS. One row per PERSON.
-- D13: duplicates merge into one person. A second arrival never creates a
-- second lead — it adds a channel and, if new, an identity.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leads (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  name_kana     TEXT,
  company       TEXT,
  title         TEXT,                     -- D1: on the business card, thrown away today

  -- D11: source is two things, not one.
  first_touch   TEXT,                     -- never changes; what the dashboard attributes to
  first_touch_at TEXT,
  first_touch_note TEXT,                  -- "6/20 展示会", who met them

  -- ③ 関係 / WHO (D9) — derived from scoring, never hand-picked
  relationship  TEXT NOT NULL DEFAULT 'individual'
                CHECK (relationship IN ('individual','corporate','partner')),

  -- ② 目的 / WHY (D9) — discovered over time, 'unknown' is a real answer
  purpose       TEXT NOT NULL DEFAULT 'unknown'
                CHECK (purpose IN ('investment','relocation','second_home','business_base','unknown')),

  -- Status (D10). Six forward stages + four exits. One status only —
  -- the free-text column is gone; see lead_activity_note below.
  stage         TEXT NOT NULL DEFAULT 'new'
                CHECK (stage IN ('new','nurturing','responded','booked','negotiating','won',
                                 'lost','too_early','unreachable','unsubscribed')),
  stage_at      TEXT NOT NULL DEFAULT (datetime('now')),
  stage_reason  TEXT,                     -- required by convention when stage='lost'
  revisit_at    TEXT,                     -- required by convention when stage='too_early'

  owner_id      INTEGER REFERENCES staff(id),   -- D14

  merged_into   INTEGER REFERENCES leads(id),   -- set when this row loses a merge
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_leads_stage  ON leads(stage) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_owner  ON leads(owner_id) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_merged ON leads(merged_into);

-- ─────────────────────────────────────────────────────────────────────────────
-- IDENTITIES. Emails and phones. This is what dedupe matches on (D13).
-- A person can have several. Kept separate from leads precisely so a second
-- arrival with a new address enriches rather than duplicates.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_identities (
  id        INTEGER PRIMARY KEY,
  lead_id   INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL CHECK (kind IN ('email','phone')),
  value     TEXT NOT NULL,                -- stored normalised (lowercased / digits only)
  is_primary INTEGER NOT NULL DEFAULT 0,
  status    TEXT NOT NULL DEFAULT 'ok'
            CHECK (status IN ('ok','bounced','invalid')),
  added_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(kind, value)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- CHANNELS. D13: "put all three in a sequence — this person arrived from three
-- different channels." Grows over time. first_touch on the lead is the first
-- of these and is what the dashboard counts (D16), so the source chart totals
-- the same number as the bookings tile.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_channels (
  id         INTEGER PRIMARY KEY,
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  channel    TEXT NOT NULL CHECK (channel IN (
               'business_card','csv','gohighlevel','lp_form','sns','gmail',
               'referral','whatsapp','line','showroom','property_finder')),
  note       TEXT,
  seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(lead_id, channel)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- REGIONS. ① 地域 / WHERE (D9) — MULTI-select, which is why it is its own table.
-- Sub-categories per region (D9a) live alongside because they are region-specific.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_regions (
  id        INTEGER PRIMARY KEY,
  lead_id   INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  region    TEXT NOT NULL CHECK (region IN ('dubai','lombok','japan')),
  -- Dubai sub-axes (D9a). Lombok and Japan sub-categories are still owed by
  -- Balraj, so they are simply left NULL rather than guessed at.
  prop_status TEXT CHECK (prop_status IN ('offplan','ready','secondary')),
  prop_type   TEXT CHECK (prop_type   IN ('apartment','penthouse','villa','townhouse')),
  bedrooms    TEXT CHECK (bedrooms    IN ('studio','1br','2br','3br','4br_plus')),
  inferred_from TEXT,                     -- which click/content revealed it
  set_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(lead_id, region)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- CONSENT. D1/D11: the field that does not exist anywhere today, and the one
-- with legal weight under 特定電子メール法.
-- 'unknown' is an allowed and honest value — 25,000 CSV rows will have it.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_consent (
  lead_id     INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
  basis       TEXT NOT NULL DEFAULT 'unknown'
              CHECK (basis IN ('explicit','implied','ambiguous','unknown','withdrawn')),
  obtained_at TEXT,
  obtained_via TEXT,                      -- 'lp_form checkbox', 'card exchanged 2026-06-20', ...
  evidence    TEXT,
  withdrawn_at TEXT,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- EVENTS. Append-only. THE asset (D8 discussion): the score is only a sum over
-- this, so scoring rules can change later and history can be recomputed.
-- Nothing in this table is ever updated or deleted.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY,
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN (
               -- behaviour (D8: these decay)
               'open','click','page_view','booking_page_view','reply',
               -- facts (D8: these are permanent)
               'booking_completed','wants_meeting','corporate_deal',
               'partnership','high_budget',
               -- bookkeeping, never scored
               'imported','merged','sent','bounce','unsubscribe','stage_change')),
  detail     TEXT,                        -- url, subject, campaign, free note
  send_id    TEXT,                        -- links back to the exact email
  source     TEXT NOT NULL DEFAULT 'system'
             CHECK (source IN ('system','sendgrid','web','ai','human')),
  set_by     INTEGER REFERENCES staff(id),-- D4: a human-granted +30 must be attributable
  occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
  voided_at  TEXT,                        -- D7: the rep's one-tap "this wasn't real"
  voided_by  INTEGER REFERENCES staff(id)
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_send ON events(send_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- SCORING RULES. D8: editable, not hardcoded — the card on the demo says
-- "tuned monthly against close rate", so it has to actually be tunable.
-- decays=1 → behaviour, halves at 60 days, zero at 120 (D8).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scoring_rules (
  event_kind TEXT PRIMARY KEY,
  points     INTEGER NOT NULL,
  decays     INTEGER NOT NULL DEFAULT 0,
  label_ja   TEXT,
  label_en   TEXT,
  active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  note  TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- TASKS. D12: six fields, not one sentence. `reason` is the field that decides
-- whether reps trust the system, so it is NOT NULL by design.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY,
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  type       TEXT NOT NULL CHECK (type IN (
               'call','email','send_material','send_booking_link','prepare',
               'visit','follow_up','set_next_date')),
  owner_id   INTEGER REFERENCES staff(id),
  due_at     TEXT,                        -- D12: a real timestamp. "today" was a word.
  state      TEXT NOT NULL DEFAULT 'open'
             CHECK (state IN ('open','done','skipped')),
  created_by TEXT NOT NULL DEFAULT 'system'
             CHECK (created_by IN ('ai','rule','human','system')),
  reason     TEXT NOT NULL,               -- "スコア92・予約ページ閲覧"
  done_at    TEXT,
  done_by    INTEGER REFERENCES staff(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(owner_id, due_at) WHERE state='open';

-- ─────────────────────────────────────────────────────────────────────────────
-- REFERRALS. D11b: a referral form, and the referrer recorded as a PERSON —
-- because someone who refers three clients is a パートナー, which is a 30-point
-- scoring rule. This table is how partners get discovered.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referrals (
  id              INTEGER PRIMARY KEY,
  lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  referrer_staff_id INTEGER REFERENCES staff(id),   -- internal agent, from a dropdown
  referrer_lead_id  INTEGER REFERENCES leads(id),   -- an external person, as a lead
  note            TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (referrer_staff_id IS NOT NULL OR referrer_lead_id IS NOT NULL)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SEQUENCES / SENDS. D1: SendGrid delivers; send_id is the string that ties an
-- email to a person and makes every passive signal attributable.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sequences (
  id     INTEGER PRIMARY KEY,
  name   TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sequence_steps (
  id           INTEGER PRIMARY KEY,
  sequence_id  INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
  step_no      INTEGER NOT NULL,
  offset_days  INTEGER NOT NULL,          -- relative to enrolment, never absolute dates
  subject      TEXT NOT NULL,
  purpose      TEXT,
  cta          TEXT,
  active       INTEGER NOT NULL DEFAULT 1,
  UNIQUE(sequence_id, step_no)
);

CREATE TABLE IF NOT EXISTS sends (
  send_id    TEXT PRIMARY KEY,            -- e_7Kq2mB — the ID stamped into the email
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  step_id    INTEGER REFERENCES sequence_steps(id),
  to_email   TEXT NOT NULL,
  sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
  provider   TEXT NOT NULL DEFAULT 'sendgrid',
  status     TEXT NOT NULL DEFAULT 'queued'
             CHECK (status IN ('queued','sent','delivered','bounced','dropped'))
);
CREATE INDEX IF NOT EXISTS idx_sends_lead ON sends(lead_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- IMPORTS. D11: the importer interrogates the file rather than guessing.
-- Every question it asked and every answer given is kept, because six months
-- later "where did these 2,000 people come from" is a real question.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS imports (
  id           INTEGER PRIMARY KEY,
  filename     TEXT NOT NULL,
  rows_seen    INTEGER NOT NULL DEFAULT 0,
  rows_created INTEGER NOT NULL DEFAULT 0,
  rows_merged  INTEGER NOT NULL DEFAULT 0,
  rows_skipped INTEGER NOT NULL DEFAULT 0,
  answers_json TEXT,                      -- the wizard Q&A, verbatim
  imported_by  INTEGER REFERENCES staff(id),
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
