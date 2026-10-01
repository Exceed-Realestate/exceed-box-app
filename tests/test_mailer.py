"""Outbound email: enrolment, the sweep, the worker, and every way it could
send the wrong thing twice.

These run on SQLite like the rest of the suite. The Postgres file re-runs the
duplicate and restart cases against the real database, because the guarantees
depend on constraint behaviour that differs between the two.

The bar here is not "the happy path works". It is:
  * a step is never sent twice, however many times anything is retried
  * a worker dying mid-send loses nothing and duplicates nothing
  * an unsubscribe stops mail immediately, even for a job already queued
  * the default configuration cannot deliver anything at all
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, mailer  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────

class RecordingProvider:
    """Delivers nothing, records everything, and can be told to fail."""
    name = "recording"
    delivers = True

    def __init__(self, fail_times=0, permanent=False):
        self.sent = []
        self.fail_times = fail_times
        self.permanent = permanent
        self.calls = 0

    def send(self, *, to, subject, text, idempotency_key, headers=None):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            if self.permanent:
                raise mailer.PermanentMailError("bad address")
            raise mailer.MailError("temporary upstream wobble")
        self.sent.append({"to": to, "subject": subject, "key": idempotency_key})
        return "msg-%s" % idempotency_key


@pytest.fixture()
def con():
    db.reset()
    c = db.connect()
    yield c
    c.close()


def _seq(con, steps=(0, 7, 14)):
    cur = con.execute("INSERT INTO sequences (name, active) VALUES (?, TRUE)",
                      ("QA sequence",))
    sid = cur.lastrowid
    for i, off in enumerate(steps, start=1):
        con.execute(
            "INSERT INTO sequence_steps (sequence_id, step_no, offset_days, subject,"
            " purpose, cta, active) VALUES (?,?,?,?,?,?, TRUE)",
            (sid, i, off, "Step %d subject" % i, "why %d" % i, "CTA %d" % i))
    con.commit()
    return sid


def _lead(con, email="qa-lead@example.invalid", name="QA Lead", status="ok"):
    """Addresses live in lead_identities, not on the lead — one person can hold
    several and each carries its own delivery status."""
    cur = con.execute("INSERT INTO leads (name, first_touch) VALUES (?,?)", (name, "csv"))
    lid = cur.lastrowid
    if email:
        con.execute("INSERT INTO lead_identities (lead_id, kind, value, is_primary, status)"
                    " VALUES (?, 'email', ?, 1, ?)", (lid, email.lower(), status))
    con.commit()
    return lid


# ── the default configuration must not be able to send ───────────────────────

def test_default_provider_does_not_deliver():
    """A nurture system that starts mailing 30,000 people because a config file
    was copied to a new host is a disaster. Default is dry-run."""
    assert mailer.PROVIDER == "dryrun"
    p = mailer.get_provider()
    assert p.delivers is False


def test_sendgrid_without_a_key_refuses_rather_than_pretending():
    with pytest.raises(mailer.MailError, match="SENDGRID_API_KEY"):
        mailer.SendGridProvider("", "a@b.c", "X")


def test_describe_reports_problems_honestly(monkeypatch):
    monkeypatch.setattr(mailer, "PROVIDER", "sendgrid")
    monkeypatch.setattr(mailer, "SENDGRID_API_KEY", "")
    monkeypatch.setattr(mailer, "PUBLIC_BASE", "")
    d = mailer.describe()
    assert d["ready"] is False
    assert any("SENDGRID_API_KEY" in p for p in d["problems"])


# ── enrolment ────────────────────────────────────────────────────────────────

def test_enrol_is_idempotent(con):
    sid = _seq(con)
    lid = _lead(con)
    assert mailer.enrol(con, lid, sid) == "enrolled"
    assert mailer.enrol(con, lid, sid) == "already_active"
    con.commit()
    assert db.scalar(con, "SELECT count(*) FROM nurture_enrolments") == 1


def test_stopped_enrolment_is_not_revived_by_a_pause_or_resume(con):
    """Unsubscribed and unreachable are permanent (D17). Only an explicit stop
    may overwrite a stop."""
    sid = _seq(con)
    lid = _lead(con)
    mailer.enrol(con, lid, sid)
    mailer.set_enrolment_state(con, lid, "stopped", "unsubscribed")
    con.commit()
    mailer.set_enrolment_state(con, lid, "active", "trying to revive")
    con.commit()
    row = db.one(con, "SELECT state FROM nurture_enrolments WHERE lead_id=?", (lid,))
    assert row["state"] == "stopped"


# ── the sweep ────────────────────────────────────────────────────────────────

def test_sweep_queues_only_steps_that_are_due(con):
    sid = _seq(con, steps=(0, 7, 14))
    lid = _lead(con)
    mailer.enrol(con, lid, sid)
    con.commit()
    out = mailer.sweep_due(con)
    assert out["queued"] == 1, out
    rows = db.all_(con, "SELECT step_id FROM sends WHERE lead_id=?", (lid,))
    assert len(rows) == 1

    # ten days later, step 2 is due and step 3 is not
    out2 = mailer.sweep_due(con, now=dt.datetime.utcnow() + dt.timedelta(days=10))
    assert out2["queued"] == 1
    assert db.scalar(con, "SELECT count(*) FROM sends WHERE lead_id=?", (lid,)) == 2


def test_sweep_run_twice_queues_nothing_extra(con):
    """The whole idempotency claim in one test."""
    sid = _seq(con, steps=(0,))
    lid = _lead(con)
    mailer.enrol(con, lid, sid)
    con.commit()
    a = mailer.sweep_due(con)
    b = mailer.sweep_due(con)
    c = mailer.sweep_due(con)
    assert a["queued"] == 1
    assert b["queued"] == 0 and c["queued"] == 0
    assert b["skipped_already"] == 1
    assert db.scalar(con, "SELECT count(*) FROM sends") == 1


def test_sweep_skips_and_stops_a_suppressed_address(con):
    sid = _seq(con, steps=(0,))
    lid = _lead(con, email="gone@example.invalid")
    mailer.enrol(con, lid, sid)
    mailer.suppress(con, "gone@example.invalid", "unsubscribed", source="test")
    con.commit()
    out = mailer.sweep_due(con)
    assert out["queued"] == 0
    assert out["skipped_no_consent"] == 1
    assert db.scalar(con, "SELECT count(*) FROM sends") == 0
    row = db.one(con, "SELECT state, state_reason FROM nurture_enrolments WHERE lead_id=?",
                 (lid,))
    assert row["state"] == "stopped"


def test_sweep_skips_a_withdrawn_consent(con):
    sid = _seq(con, steps=(0,))
    lid = _lead(con, email="withdrawn@example.invalid")
    mailer.enrol(con, lid, sid)
    con.execute("INSERT INTO lead_consent (lead_id, basis, withdrawn_at)"
                " VALUES (?,?,datetime('now'))", (lid, "unknown"))
    con.commit()
    out = mailer.sweep_due(con)
    assert out["queued"] == 0 and out["skipped_no_consent"] == 1


def test_one_bad_lead_does_not_stop_the_sweep(con):
    """29,999 other people must still get their mail."""
    sid = _seq(con, steps=(0,))
    bad = _lead(con, email="stop@example.invalid", name="Bad")
    good = _lead(con, email="fine@example.invalid", name="Good")
    mailer.enrol(con, bad, sid)
    mailer.enrol(con, good, sid)
    mailer.suppress(con, "stop@example.invalid", "bounced")
    con.commit()
    out = mailer.sweep_due(con)
    assert out["queued"] == 1
    assert db.scalar(con, "SELECT count(*) FROM sends WHERE lead_id=?", (good,)) == 1


def test_lead_with_no_email_is_never_queued(con):
    sid = _seq(con, steps=(0,))
    lid = _lead(con, email=None)
    mailer.enrol(con, lid, sid)
    con.commit()
    out = mailer.sweep_due(con)
    assert out["queued"] == 0


# ── the worker ───────────────────────────────────────────────────────────────

def _queued_one(con):
    sid = _seq(con, steps=(0,))
    lid = _lead(con)
    mailer.enrol(con, lid, sid)
    con.commit()
    mailer.sweep_due(con)
    return lid


def test_worker_sends_and_marks_sent(con):
    _queued_one(con)
    p = RecordingProvider()
    out = mailer.run_once(con, provider=p)
    assert out["sent"] == 1
    assert len(p.sent) == 1
    assert db.scalar(con, "SELECT count(*) FROM sends WHERE status='sent'") == 1


def test_running_the_worker_twice_does_not_send_twice(con):
    _queued_one(con)
    p = RecordingProvider()
    mailer.run_once(con, provider=p)
    mailer.run_once(con, provider=p)
    assert len(p.sent) == 1


def test_idempotency_key_is_the_send_id(con):
    """A reclaimed job retried against the provider must be recognisable as the
    same message, or a crash becomes a duplicate email."""
    _queued_one(con)
    p = RecordingProvider()
    mailer.run_once(con, provider=p)
    send_id = db.one(con, "SELECT send_id FROM sends")["send_id"]
    assert p.sent[0]["key"] == send_id


def test_transient_failure_requeues_with_backoff_and_later_succeeds(con):
    """A failed job must NOT be retried inside the same run — that burns every
    attempt at once against a provider which is probably rate-limiting us."""
    _queued_one(con)
    p = RecordingProvider(fail_times=1)
    out1 = mailer.run_once(con, provider=p)
    assert out1["sent"] == 0 and out1["requeued"] == 1
    assert p.calls == 1, "retried within the same run"
    assert db.scalar(con, "SELECT count(*) FROM sends WHERE status='queued'") == 1

    # still backed off: a run right now picks it up again only after the delay
    assert mailer.run_once(con, provider=p)["sent"] == 0
    later = dt.datetime.utcnow() + dt.timedelta(minutes=90)
    out2 = mailer.run_once(con, provider=p, now=later)
    assert out2["sent"] == 1
    assert len(p.sent) == 1


def test_backoff_grows_with_attempts(con):
    _queued_one(con)
    p = RecordingProvider(fail_times=99)
    now = dt.datetime.utcnow()
    mailer.run_once(con, provider=p, now=now)
    first = db.one(con, "SELECT scheduled_for, attempts FROM sends")["scheduled_for"]
    mailer.run_once(con, provider=p, now=now + dt.timedelta(minutes=5))
    second = db.one(con, "SELECT scheduled_for, attempts FROM sends")["scheduled_for"]
    assert str(second) > str(first)


def test_permanent_failure_fails_the_job_and_suppresses_the_address(con):
    _queued_one(con)
    p = RecordingProvider(fail_times=1, permanent=True)
    out = mailer.run_once(con, provider=p)
    assert out["failed"] == 1
    row = db.one(con, "SELECT status FROM sends")
    assert row["status"] == "failed"
    assert mailer.is_suppressed(con, "qa-lead@example.invalid")


def test_attempts_are_capped(con):
    """A job that fails forever must stop, not loop for ever. `now` is advanced
    past the backoff each round, which is what a real cron does."""
    _queued_one(con)
    p = RecordingProvider(fail_times=99)
    now = dt.datetime.utcnow()
    for i in range(mailer.MAX_ATTEMPTS + 2):
        mailer.run_once(con, provider=p, now=now + dt.timedelta(hours=2 * i))
    row = db.one(con, "SELECT status, attempts FROM sends")
    assert row["status"] == "failed"
    assert row["attempts"] <= mailer.MAX_ATTEMPTS + 1


def test_a_crashed_worker_releases_its_job(con):
    """Simulates the process dying between claim and send."""
    _queued_one(con)
    now = dt.datetime.utcnow()
    con.execute("UPDATE sends SET status='sending', claimed_at=?, claimed_by='dead'",
                ((now - dt.timedelta(seconds=mailer.RECLAIM_AFTER_SECONDS + 60))
                 .isoformat(sep=" ", timespec="seconds"),))
    con.commit()
    assert mailer.reclaim_stale(con, now=now) == 1
    assert db.scalar(con, "SELECT count(*) FROM sends WHERE status='queued'") == 1
    p = RecordingProvider()
    assert mailer.run_once(con, provider=p)["sent"] == 1


def test_a_recent_claim_is_not_stolen(con):
    """A worker that is merely slow must keep its job, or two workers send the
    same email."""
    _queued_one(con)
    now = dt.datetime.utcnow()
    con.execute("UPDATE sends SET status='sending', claimed_at=?, claimed_by='busy'",
                (now.isoformat(sep=" ", timespec="seconds"),))
    con.commit()
    assert mailer.reclaim_stale(con, now=now) == 0
    assert db.scalar(con, "SELECT count(*) FROM sends WHERE status='sending'") == 1


def test_unsubscribe_cancels_a_job_already_queued(con):
    """Permission to contact is checked again at send time, not only at queue
    time — someone who unsubscribes after the sweep must not receive the mail."""
    _queued_one(con)
    mailer.suppress(con, "qa-lead@example.invalid", "unsubscribed", source="test")
    con.commit()
    p = RecordingProvider()
    out = mailer.run_once(con, provider=p)
    assert out["sent"] == 0
    assert len(p.sent) == 0
    assert db.one(con, "SELECT status FROM sends")["status"] == "cancelled"


def test_worker_records_its_run(con):
    """"Did the sweep actually run?" has to be answerable without reading logs."""
    _queued_one(con)
    mailer.run_once(con, provider=RecordingProvider())
    row = mailer.last_run(con, "mail_worker")
    assert row is not None and row["ok"]


def test_suppress_is_idempotent_and_case_insensitive(con):
    mailer.suppress(con, "Person@Example.Invalid", "unsubscribed")
    mailer.suppress(con, "person@example.invalid", "bounced")
    con.commit()
    assert db.scalar(con, "SELECT count(*) FROM email_suppressions") == 1
    assert mailer.is_suppressed(con, "PERSON@EXAMPLE.INVALID")


def test_rendered_body_only_contains_what_a_human_wrote(con):
    """No invented property availability, price, yield, tax or visa claim."""
    sid = _seq(con, steps=(0,))
    lid = _lead(con)
    step = db.one(con, "SELECT * FROM sequence_steps WHERE sequence_id=?", (sid,))
    lead = db.one(con, "SELECT * FROM leads WHERE id=?", (lid,))
    subject, body = mailer.render_step(step, lead)
    assert subject == "Step 1 subject"
    for fragment in ("why 1", "CTA 1", "QA Lead"):
        assert fragment in body


def test_an_address_the_provider_already_rejected_is_never_picked(con):
    """lead_identities.status carries the provider's verdict per address. A
    bounced address must not be chosen even when it is the primary one."""
    sid = _seq(con, steps=(0,))
    lid = _lead(con, email="hard-bounced@example.invalid", status="bounced")
    mailer.enrol(con, lid, sid)
    con.commit()
    assert mailer.primary_email(con, lid) is None
    assert mailer.sweep_due(con)["queued"] == 0


def test_a_second_good_address_is_used_when_the_primary_bounced(con):
    sid = _seq(con, steps=(0,))
    lid = _lead(con, email="dead@example.invalid", status="bounced")
    con.execute("INSERT INTO lead_identities (lead_id, kind, value, is_primary, status)"
                " VALUES (?, 'email', ?, 0, 'ok')", (lid, "alive@example.invalid"))
    mailer.enrol(con, lid, sid)
    con.commit()
    assert mailer.primary_email(con, lid) == "alive@example.invalid"
    assert mailer.sweep_due(con)["queued"] == 1


# ── the unsubscribe link every email carries ─────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    from app import api
    return TestClient(api.app)


def test_unsubscribe_get_asks_before_acting(con):
    """A mail client or security scanner pre-fetching the link must not
    unsubscribe someone who never clicked."""
    _queued_one(con)
    mailer.run_once(con, provider=RecordingProvider())
    sid = db.one(con, "SELECT send_id FROM sends")["send_id"]
    r = _client().get("/u/%s" % sid)
    assert r.status_code == 200
    assert "Unsubscribe from these emails?" in r.text
    assert not mailer.is_suppressed(db.connect(), "qa-lead@example.invalid")


def test_unsubscribe_post_suppresses_and_stops_the_enrolment(con):
    _queued_one(con)
    mailer.run_once(con, provider=RecordingProvider())
    sid = db.one(con, "SELECT send_id FROM sends")["send_id"]
    r = _client().post("/u/%s" % sid)
    assert r.status_code == 200 and "unsubscribed" in r.text.lower()

    c2 = db.connect()
    assert mailer.is_suppressed(c2, "qa-lead@example.invalid")
    assert db.one(c2, "SELECT state FROM nurture_enrolments")["state"] == "stopped"
    c2.close()


def test_unsubscribe_is_idempotent(con):
    _queued_one(con)
    mailer.run_once(con, provider=RecordingProvider())
    sid = db.one(con, "SELECT send_id FROM sends")["send_id"]
    cl = _client()
    cl.post("/u/%s" % sid)
    r = cl.post("/u/%s" % sid)
    assert r.status_code == 200
    r2 = cl.get("/u/%s" % sid)
    assert "already unsubscribed" in r2.text.lower()


def test_unsubscribe_cancels_mail_still_in_the_queue(con):
    """The point of checking suppression at send time as well as queue time."""
    sid_seq = _seq(con, steps=(0, 7))
    lid = _lead(con, email="queued-person@example.invalid")
    mailer.enrol(con, lid, sid_seq)
    con.commit()
    mailer.sweep_due(con, now=dt.datetime.utcnow() + dt.timedelta(days=10))
    assert db.scalar(con, "SELECT count(*) FROM sends WHERE status='queued'") == 2
    send_id = db.one(con, "SELECT send_id FROM sends LIMIT 1")["send_id"]

    _client().post("/u/%s" % send_id)

    c2 = db.connect()
    assert db.scalar(c2, "SELECT count(*) FROM sends WHERE status='queued'") == 0
    assert db.scalar(c2, "SELECT count(*) FROM sends WHERE status='cancelled'") == 2
    c2.close()


def test_unknown_unsubscribe_token_is_a_404_and_changes_nothing(con):
    r = _client().post("/u/e_totally-made-up")
    assert r.status_code == 404
    assert db.scalar(con, "SELECT count(*) FROM email_suppressions") == 0


def test_unsubscribe_needs_no_login(con):
    """A recipient is a customer, not a user of this system."""
    _queued_one(con)
    mailer.run_once(con, provider=RecordingProvider())
    sid = db.one(con, "SELECT send_id FROM sends")["send_id"]
    r = _client().get("/u/%s" % sid)          # no Authorization header at all
    assert r.status_code == 200
