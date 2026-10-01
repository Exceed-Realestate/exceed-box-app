"""Outbound email: providers, enrolment, the queue, and the worker.

The shape, in one paragraph. A lead is *enrolled* in a sequence. A sweep works
out which steps are due (enrolment date + the step's offset_days), skips
anything already sent, skips anything suppressed or without consent, and writes
one `sends` row per due step with status 'queued'. A worker claims queued rows,
hands each to a provider, and records the outcome. Every one of those stages is
safe to run twice.

## Why it cannot send twice

`sends` carries UNIQUE(lead_id, step_id). Enqueue is therefore idempotent *by
insertion*: a duplicate is a constraint violation, not a second email. Two
workers racing, a retried cron, a double-clicked button — all collapse to one
row. The claim step moves a row to 'sending' with a stamp, and only a row still
in 'queued' can be claimed, so two workers cannot both take the same job.

A crash between "claimed" and "provider accepted" is the genuinely hard case,
and it cannot be solved by bookkeeping alone: the process died without knowing
whether the mail left. Two things make it safe. Claims older than
RECLAIM_AFTER_SECONDS return to 'queued' so work is never lost, and every
provider call carries an idempotency key equal to send_id, so a provider that
already accepted that key does not accept it twice. Losing an email is bad;
sending it twice to a cold 30,000-address list is worse, so where the two
conflict this errs towards not sending.

## Why the default provider does not send

`EXCEEDBOX_MAIL_PROVIDER` defaults to **dryrun**. It records exactly what would
have gone out and delivers nothing. Real delivery requires deliberately setting
the provider to `sendgrid` AND supplying a key — no key is not "fall back to
pretending", it is a refusal. An automated nurture system that starts mailing a
30,000-person list the moment a config file is copied to a new host is a
disaster, and this is the guard against it.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import secrets
import socket
import urllib.error
import urllib.request

from . import db

log = logging.getLogger("exceedbox.mailer")

PROVIDER = (os.environ.get("EXCEEDBOX_MAIL_PROVIDER") or "dryrun").strip().lower()
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY") or ""
FROM_EMAIL = os.environ.get("EXCEEDBOX_MAIL_FROM") or ""
FROM_NAME = os.environ.get("EXCEEDBOX_MAIL_FROM_NAME") or "EXCEED"
REPLY_TO = os.environ.get("EXCEEDBOX_MAIL_REPLY_TO") or ""
PUBLIC_BASE = (os.environ.get("EXCEEDBOX_PUBLIC_BASE_URL") or "").rstrip("/")

MAX_ATTEMPTS = int(os.environ.get("EXCEEDBOX_MAIL_MAX_ATTEMPTS", "5"))
RECLAIM_AFTER_SECONDS = int(os.environ.get("EXCEEDBOX_MAIL_RECLAIM_SECONDS", "900"))
BATCH_LIMIT = int(os.environ.get("EXCEEDBOX_MAIL_BATCH", "200"))

WORKER_ID = "%s:%d" % (socket.gethostname(), os.getpid())


class MailError(RuntimeError):
    """Transient by default — the job is retried."""
    permanent = False


class PermanentMailError(MailError):
    """The provider refused in a way retrying cannot fix (bad address, blocked
    recipient). The job goes to 'failed' and the address is suppressed."""
    permanent = True


# ── providers ────────────────────────────────────────────────────────────────

class DryRunProvider:
    """Records, never delivers. The default, deliberately."""
    name = "dryrun"
    delivers = False

    def send(self, *, to, subject, text, idempotency_key, headers=None):
        log.info("[dryrun] would send to=%s subject=%r key=%s", to, subject,
                 idempotency_key)
        return "dryrun-%s" % idempotency_key


class SendGridProvider:
    name = "sendgrid"
    delivers = True
    ENDPOINT = "https://api.sendgrid.com/v3/mail/send"

    def __init__(self, api_key: str, from_email: str, from_name: str,
                 reply_to: str = ""):
        if not api_key:
            raise MailError("SENDGRID_API_KEY is not set")
        if not from_email:
            raise MailError("EXCEEDBOX_MAIL_FROM is not set")
        self.api_key, self.from_email = api_key, from_email
        self.from_name, self.reply_to = from_name, reply_to

    def send(self, *, to, subject, text, idempotency_key, headers=None):
        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": self.from_email, "name": self.from_name},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}],
            # Our own id travels with the message and comes back on every
            # webhook event, which is what ties an open or a click to a send.
            "custom_args": {"send_id": idempotency_key},
        }
        if self.reply_to:
            payload["reply_to"] = {"email": self.reply_to}
        if headers:
            payload["headers"] = headers
        req = urllib.request.Request(
            self.ENDPOINT, data=json.dumps(payload).encode(), method="POST",
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
                # SendGrid dedupes on this for 24h, which is what makes a
                # reclaimed job safe to retry.
                "Idempotency-Key": idempotency_key,
            })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.headers.get("X-Message-Id") or idempotency_key
        except urllib.error.HTTPError as e:
            body = (e.read() or b"")[:400].decode("utf-8", "replace")
            if e.code in (400, 401, 403, 413):
                raise PermanentMailError("sendgrid %s: %s" % (e.code, body))
            raise MailError("sendgrid %s: %s" % (e.code, body))
        except Exception as e:
            raise MailError("sendgrid unreachable: %s" % e)


def get_provider():
    if PROVIDER == "sendgrid":
        return SendGridProvider(SENDGRID_API_KEY, FROM_EMAIL, FROM_NAME, REPLY_TO)
    if PROVIDER == "dryrun":
        return DryRunProvider()
    raise MailError("unknown EXCEEDBOX_MAIL_PROVIDER: %r" % PROVIDER)


def describe() -> dict:
    """True state for /api/health and the Integrations screen. Never claims
    healthy when it would refuse to send."""
    d = {"provider": PROVIDER, "delivers": PROVIDER != "dryrun",
         "from": FROM_EMAIL or None}
    problems = []
    if PROVIDER == "sendgrid":
        if not SENDGRID_API_KEY:
            problems.append("SENDGRID_API_KEY missing")
        if not FROM_EMAIL:
            problems.append("EXCEEDBOX_MAIL_FROM missing")
    if not PUBLIC_BASE:
        problems.append("EXCEEDBOX_PUBLIC_BASE_URL missing — open/click tracking "
                        "and unsubscribe links cannot be built")
    d["problems"] = problems
    d["ready"] = not problems
    return d


# ── consent and suppression ──────────────────────────────────────────────────

def is_suppressed(con, email: str) -> bool:
    if not email:
        return True
    return db.scalar(con, "SELECT count(*) FROM email_suppressions WHERE lower(email)=?",
                     (email.strip().lower(),)) > 0


def suppress(con, email: str, reason: str, detail: str = None, source: str = None):
    """Idempotent. Keyed on the ADDRESS, not the lead: one person can arrive as
    three leads and an unsubscribe has to stop all of them."""
    if not email:
        return
    con.execute(
        "INSERT OR IGNORE INTO email_suppressions (email, reason, detail, source)"
        " VALUES (?,?,?,?)", (email.strip().lower(), reason, detail, source))


def primary_email(con, lead_id: int):
    """The address to write to, or None.

    Addresses live in `lead_identities`, not on the lead — one person can hold
    several, and each carries its own status. An address the provider has
    already rejected is never picked, however 'primary' it is flagged.
    """
    row = db.one(con, """
        SELECT value FROM lead_identities
         WHERE lead_id=? AND kind='email' AND status='ok'
      ORDER BY is_primary DESC, id ASC LIMIT 1""", (lead_id,))
    return row["value"] if row else None


def may_email(con, lead_id: int) -> tuple:
    """(allowed, reason, address). Consent is checked separately from sales
    stage — an unsubscribe stops the channel even while the deal is still open,
    and a lead can be mid-negotiation and unmailable at the same time."""
    email = primary_email(con, lead_id)
    if not email:
        return False, "no_email", None
    if is_suppressed(con, email):
        return False, "suppressed", email
    row = db.one(con, "SELECT basis, withdrawn_at FROM lead_consent WHERE lead_id=?",
                 (lead_id,))
    if row and row["withdrawn_at"]:
        return False, "consent_withdrawn", email
    return True, "ok", email


# ── enrolment ────────────────────────────────────────────────────────────────

def enrol(con, lead_id: int, sequence_id: int) -> str:
    """Idempotent: re-enrolling an active lead is a no-op, not a second run."""
    existing = db.one(
        con, "SELECT id, state FROM nurture_enrolments WHERE lead_id=? AND sequence_id=?",
        (lead_id, sequence_id))
    if existing:
        return "already_%s" % existing["state"]
    con.execute("INSERT INTO nurture_enrolments (lead_id, sequence_id) VALUES (?,?)",
                (lead_id, sequence_id))
    return "enrolled"


def set_enrolment_state(con, lead_id: int, state: str, reason: str = None,
                        sequence_id: int = None) -> int:
    """Pause, stop, resume or complete. Returns rows changed.

    A 'stopped' enrolment is never revived by this — unsubscribed and
    unreachable are permanent (D17 decision 1). Pause is reversible.
    """
    if state not in ("active", "paused", "stopped", "completed"):
        raise ValueError("bad enrolment state: %r" % state)
    sql = ("UPDATE nurture_enrolments SET state=?, state_reason=?,"
           " state_changed_at=datetime('now') WHERE lead_id=?")
    args = [state, reason, lead_id]
    if state != "stopped":
        sql += " AND state <> 'stopped'"
    if sequence_id is not None:
        sql += " AND sequence_id=?"
        args.append(sequence_id)
    cur = con.execute(sql, tuple(args))
    return cur.rowcount if cur.rowcount is not None else 0


# ── the sweep: work out what is due and queue it ─────────────────────────────

def _now():
    return _dt.datetime.utcnow()


def _new_send_id() -> str:
    # short, URL-safe, unguessable — it is stamped into tracking links
    return "e_" + secrets.token_urlsafe(9)


def render_step(step, lead) -> tuple:
    """(subject, text). Content comes from the editable sequence step, never
    invented here. No property availability, price, yield, tax or visa claim is
    generated — the step body is what a human wrote."""
    name = (lead["name"] if "name" in lead.keys() else None) or ""
    subject = (step["subject"] or "").strip()
    lines = []
    if name:
        lines.append("%s 様" % name)
        lines.append("")
    purpose = (step["purpose"] if "purpose" in step.keys() else None) or ""
    if purpose:
        lines.append(purpose)
        lines.append("")
    cta = (step["cta"] if "cta" in step.keys() else None) or ""
    if cta:
        lines.append(cta)
    return subject, "\n".join(lines).strip()


def sweep_due(con, *, now=None, limit: int = None) -> dict:
    """Queue every step that has come due. Safe to run as often as you like.

    Returns counts rather than raising on a single bad lead: one lead with a
    withdrawn consent must not stop the sweep for the other 29,999.
    """
    now = now or _now()
    limit = limit or BATCH_LIMIT
    queued = skipped_consent = skipped_dupe = 0

    rows = db.all_(con, """
        SELECT e.lead_id, e.sequence_id, e.enrolled_at,
               s.id AS step_id, s.step_no, s.offset_days
          FROM nurture_enrolments e
          JOIN sequence_steps s ON s.sequence_id = e.sequence_id
         WHERE e.state = 'active' AND s.active = TRUE
      ORDER BY e.lead_id, s.step_no
    """)

    for r in rows:
        if queued >= limit:
            break
        enrolled = _parse_ts(r["enrolled_at"])
        if enrolled is None:
            continue
        due_at = enrolled + _dt.timedelta(days=int(r["offset_days"] or 0))
        if due_at > now:
            continue

        lead = db.one(con, "SELECT id, name FROM leads WHERE id=?", (r["lead_id"],))
        if lead is None:
            continue
        allowed, why, address = may_email(con, r["lead_id"])
        if not allowed:
            skipped_consent += 1
            if why in ("suppressed", "consent_withdrawn"):
                set_enrolment_state(con, r["lead_id"], "stopped", why,
                                    sequence_id=r["sequence_id"])
            continue

        step = db.one(con, "SELECT * FROM sequence_steps WHERE id=?", (r["step_id"],))
        subject, body = render_step(step, lead)
        # INSERT OR IGNORE rather than catching IntegrityError: on Postgres a
        # constraint violation aborts the whole transaction, so a single
        # already-queued step would poison every later insert in this sweep.
        # The translator turns this into ON CONFLICT DO NOTHING, and rowcount
        # tells us which happened. The UNIQUE(lead_id, step_id) constraint is
        # still what guarantees it — this is just the non-throwing way to ask.
        cur = con.execute(
            "INSERT OR IGNORE INTO sends (send_id, lead_id, step_id, to_email, status,"
            " scheduled_for, subject, body_text, provider)"
            " VALUES (?,?,?,?, 'queued', ?, ?, ?, ?)",
            (_new_send_id(), r["lead_id"], r["step_id"], address,
             due_at.isoformat(sep=" ", timespec="seconds"), subject, body,
             PROVIDER))
        if cur.rowcount:
            queued += 1
        else:
            # Already queued or already sent — the "nobody receives the same
            # email twice" guarantee doing its job, not an error.
            skipped_dupe += 1
    con.commit()
    return {"queued": queued, "skipped_no_consent": skipped_consent,
            "skipped_already": skipped_dupe, "considered": len(rows)}


def _parse_ts(v):
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.replace(tzinfo=None)
    s = str(v).replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s.split("+")[0].strip(), fmt)
        except ValueError:
            continue
    return None


# ── the worker: claim, send, record ──────────────────────────────────────────

def reclaim_stale(con, *, now=None) -> int:
    """Return jobs whose worker died back to the queue. Bounded by attempts so a
    job that kills its worker every time eventually fails instead of looping."""
    now = now or _now()
    cutoff = (now - _dt.timedelta(seconds=RECLAIM_AFTER_SECONDS)).isoformat(
        sep=" ", timespec="seconds")
    cur = con.execute(
        "UPDATE sends SET status='queued', claimed_at=NULL, claimed_by=NULL,"
        " updated_at=datetime('now')"
        " WHERE status='sending' AND claimed_at < ? AND attempts < ?",
        (cutoff, MAX_ATTEMPTS))
    n = cur.rowcount or 0
    cur2 = con.execute(
        "UPDATE sends SET status='failed', last_error='exceeded max attempts after reclaim',"
        " updated_at=datetime('now')"
        " WHERE status='sending' AND claimed_at < ? AND attempts >= ?",
        (cutoff, MAX_ATTEMPTS))
    con.commit()
    return n + (cur2.rowcount or 0)


def _claim_one(con, *, now):
    """Claim exactly one queued job. The UPDATE ... WHERE status='queued'
    guarantees only one worker wins, on both SQLite and Postgres."""
    stamp = now.isoformat(sep=" ", timespec="seconds")
    row = db.one(con,
                 "SELECT send_id FROM sends WHERE status='queued'"
                 " AND (scheduled_for IS NULL OR scheduled_for <= ?)"
                 " ORDER BY scheduled_for LIMIT 1", (stamp,))
    if row is None:
        return None
    cur = con.execute(
        "UPDATE sends SET status='sending', claimed_at=?, claimed_by=?,"
        " attempts = attempts + 1, updated_at=datetime('now')"
        " WHERE send_id=? AND status='queued'",
        (stamp, WORKER_ID, row["send_id"]))
    con.commit()
    if not cur.rowcount:
        return None            # another worker took it between select and update
    return db.one(con, "SELECT * FROM sends WHERE send_id=?", (row["send_id"],))


def run_once(con, *, now=None, limit: int = None, provider=None) -> dict:
    """Drain up to `limit` queued jobs. Returns counts and never raises for a
    single bad job."""
    now = now or _now()
    limit = limit or BATCH_LIMIT
    provider = provider or get_provider()
    sent = failed = retried = 0
    started = _now()

    reclaimed = reclaim_stale(con, now=now)

    for _ in range(limit):
        job = _claim_one(con, now=now)
        if job is None:
            break
        try:
            if is_suppressed(con, job["to_email"]):
                con.execute(
                    "UPDATE sends SET status='cancelled', last_error='suppressed',"
                    " updated_at=datetime('now') WHERE send_id=?", (job["send_id"],))
                con.commit()
                continue
            msg_id = provider.send(
                to=job["to_email"],
                subject=job["subject"] or "",
                text=job["body_text"] or "",
                idempotency_key=job["send_id"],
                headers=_unsubscribe_headers(job["send_id"]))
            con.execute(
                "UPDATE sends SET status='sent', provider_message_id=?, last_error=NULL,"
                " sent_at=datetime('now'), updated_at=datetime('now'), provider=?"
                " WHERE send_id=?", (msg_id, provider.name, job["send_id"]))
            con.commit()
            sent += 1
        except PermanentMailError as e:
            con.execute(
                "UPDATE sends SET status='failed', last_error=?, updated_at=datetime('now')"
                " WHERE send_id=?", (str(e)[:500], job["send_id"]))
            suppress(con, job["to_email"], "invalid", str(e)[:200], "provider")
            con.commit()
            failed += 1
        except Exception as e:
            attempts = job["attempts"] or 0
            over = attempts >= MAX_ATTEMPTS
            # Exponential backoff, capped. Two reasons this matters more than it
            # looks: without it the loop below re-claims the job immediately and
            # burns every attempt in one run against a provider that is probably
            # rate-limiting us, and a retry storm against a mail provider is how
            # a sending reputation dies.
            delay = min(2 ** attempts, 60)
            retry_at = (now + _dt.timedelta(minutes=delay)).isoformat(
                sep=" ", timespec="seconds")
            con.execute(
                "UPDATE sends SET status=?, last_error=?, claimed_at=NULL, claimed_by=NULL,"
                " scheduled_for=?, updated_at=datetime('now') WHERE send_id=?",
                ("failed" if over else "queued", str(e)[:500],
                 job["scheduled_for"] if over else retry_at, job["send_id"]))
            con.commit()
            failed += 1 if over else 0
            retried += 0 if over else 1

    out = {"sent": sent, "failed": failed, "requeued": retried,
           "reclaimed": reclaimed, "provider": provider.name,
           "delivers": getattr(provider, "delivers", False)}
    _record_run(con, "mail_worker", started, True, json.dumps(out))
    return out


def _unsubscribe_headers(send_id: str) -> dict:
    """One-click unsubscribe. Without a public base URL there is no honest link
    to offer, so none is claimed."""
    if not PUBLIC_BASE:
        return {}
    url = "%s/u/%s" % (PUBLIC_BASE, send_id)
    return {"List-Unsubscribe": "<%s>" % url,
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def _record_run(con, job: str, started, ok: bool, detail: str):
    try:
        con.execute(
            "INSERT INTO worker_runs (job, started_at, finished_at, ok, detail)"
            " VALUES (?,?,datetime('now'),?,?)",
            (job, started.isoformat(sep=" ", timespec="seconds"), ok, detail))
        con.commit()
    except Exception:
        log.exception("could not record worker run")


def last_run(con, job: str = "mail_worker"):
    return db.one(con, "SELECT * FROM worker_runs WHERE job=?"
                       " ORDER BY started_at DESC LIMIT 1", (job,))
