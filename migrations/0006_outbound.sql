-- 0006 — outbound email: enrolment, a durable job queue, and suppression.
--
-- Everything before this could store a sequence and count what had been sent.
-- Nothing could actually enrol a lead, decide what was due, or send it.
--
-- Three design commitments, each of which is a row-level guarantee rather than
-- application discipline:
--
--   1. Nobody receives the same step twice. `sends` already carries
--      UNIQUE(lead_id, step_id), so enqueueing is idempotent by insertion — a
--      duplicate enqueue raises rather than queues. A retry storm, a
--      double-clicked button and two workers racing all collapse to one row.
--   2. A crash never silently loses or duplicates a send. A job is claimed by
--      moving it to 'sending' with a claim stamp; a claim older than the
--      reclaim window returns to 'queued'. The provider is called with an
--      idempotency key equal to send_id, so a reclaim that races a slow
--      provider still cannot deliver twice.
--   3. Permission to contact is separate from sales stage. Suppression is keyed
--      on the ADDRESS, not the lead, because one person can arrive as three
--      leads and an unsubscribe must stop all of them.

-- ── enrolment ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nurture_enrolments (
  id            BIGSERIAL PRIMARY KEY,
  lead_id       BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  sequence_id   BIGINT NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
  enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- active   → due steps are queued as their offset comes round
  -- paused   → reversible: replied, booked, or crossed the score threshold.
  --            D17 decision 1: replied is a PAUSE, not a STOP, so a lead a rep
  --            failed to convert comes back months later instead of being lost.
  -- stopped  → permanent: unsubscribed or unreachable.
  -- completed→ reached the last step with no reaction.
  state         TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','paused','stopped','completed')),
  state_reason  TEXT,
  state_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Set when a pause should lift by itself (decay dropping a lead back under
  -- the threshold re-enters nurture, D8). NULL means "until something changes".
  resume_after  TIMESTAMPTZ,
  UNIQUE (lead_id, sequence_id)
);
CREATE INDEX IF NOT EXISTS idx_enrol_due
  ON nurture_enrolments(state, enrolled_at);
CREATE INDEX IF NOT EXISTS idx_enrol_lead ON nurture_enrolments(lead_id);

-- ── the job queue lives on `sends` ───────────────────────────────────────────
-- A separate jobs table would let a job and its send row disagree about whether
-- an email went out. One row, one truth.
ALTER TABLE sends ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS claimed_at    TIMESTAMPTZ;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS claimed_by    TEXT;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS attempts      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS last_error    TEXT;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS provider_message_id TEXT;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS subject       TEXT;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS body_text     TEXT;
ALTER TABLE sends ADD COLUMN IF NOT EXISTS updated_at    TIMESTAMPTZ NOT NULL DEFAULT now();

-- 'sending' (claimed) and 'failed' (gave up) join the existing states.
ALTER TABLE sends DROP CONSTRAINT IF EXISTS sends_status_check;
ALTER TABLE sends ADD CONSTRAINT sends_status_check CHECK (
  status IN ('queued','sending','sent','delivered','bounced','dropped','failed','cancelled'));

CREATE INDEX IF NOT EXISTS idx_sends_claimable
  ON sends(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_sends_claimed
  ON sends(status, claimed_at);

-- ── suppression, keyed on the address ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS email_suppressions (
  email       TEXT PRIMARY KEY,
  reason      TEXT NOT NULL
              CHECK (reason IN ('unsubscribed','bounced','complained','manual','invalid')),
  detail      TEXT,
  source      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── one row per worker run, so "did the sweep run?" is answerable ────────────
CREATE TABLE IF NOT EXISTS worker_runs (
  id          BIGSERIAL PRIMARY KEY,
  job         TEXT NOT NULL,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  ok          BOOLEAN,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_worker_runs_job ON worker_runs(job, started_at DESC);
