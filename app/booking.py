"""The public booking flow — the part a customer actually touches.

Everything else in this system is internal. This module is the one place a
stranger reaches, so its rules are different and worth stating plainly.

## What a stranger may and may not learn

May: which meeting topics exist, and which half-hours are free next week.
May not: who works here, how many people work here, whether an address is
already a lead, or anything at all about another booking. Slot listings
therefore carry times and nothing else — rep assignment happens server-side at
confirmation, and the chosen rep's identity is never in a response a customer
sees.

## Why a booking is addressed by `public_ref`

A customer must be able to come back and cancel or move an appointment without
an account. The alternative to a token is emailing them a numeric id, which is
guessable, or making them log in, which nobody will do. `public_ref` is 22
random URL-safe characters, scoped to exactly one booking, and it grants exactly
three verbs on that booking. Holding it tells you nothing about any other
record, and it cannot be used to enumerate.

## Why double-booking is a database concern

Two people confirming the same 10:00 slot in the same second is the one race
that "read the calendar, then write" always loses. A partial unique index on
(owner, start) where status='booked' makes the second write fail, and the
caller retries onto the next free rep. Availability is also re-checked at
confirmation, because the customer may have had the page open for an hour.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
from typing import Optional

from . import db, ingest, scoring

log = logging.getLogger("exceedbox.booking")

REF_BYTES = 16                       # ~22 url-safe chars
MAX_NOTE = 2000
MAX_NAME = 200


class BookingError(Exception):
    """Carries a machine code so the API layer can pick the status."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def new_ref() -> str:
    return secrets.token_urlsafe(REF_BYTES)


# ── validation ───────────────────────────────────────────────────────────────

_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def clean_contact(name, email, phone, note) -> tuple:
    """Whatever a stranger typed, bounded and stripped.

    Length caps are not cosmetic: these strings go into a database, into an
    email to a rep, and onto an internal screen. Everything here is untrusted
    third-party text for ever after — the same rule the CSV importer follows.
    """
    name = (name or "").strip()[:MAX_NAME]
    email = (email or "").strip().lower()[:320]
    phone = (phone or "").strip()[:64]
    note = (note or "").strip()[:MAX_NOTE]
    if not name:
        raise BookingError("name_required", "Please tell us your name.")
    if not _EMAIL.match(email):
        raise BookingError("email_invalid", "That email address does not look right.")
    return name, email, phone, note


# ── topics ───────────────────────────────────────────────────────────────────

def public_meeting_types(con) -> list:
    """Only topics that at least one rep can actually take. Offering a topic
    nobody covers produces a booking page where every slot is empty and no
    explanation is given."""
    rows = db.all_(con, """
        SELECT t.key, t.label_en, t.label_ja, t.duration_minutes,
               count(r.user_id) AS reps
          FROM booking_meeting_types t
          LEFT JOIN booking_rep_meeting_types r ON r.meeting_type = t.key
      GROUP BY t.key, t.label_en, t.label_ja, t.duration_minutes
      ORDER BY t.duration_minutes""")
    return [{"key": r["key"], "label_en": r["label_en"], "label_ja": r["label_ja"],
             "duration_minutes": r["duration_minutes"]}
            for r in rows if (r["reps"] or 0) > 0]


# ── assignment ───────────────────────────────────────────────────────────────

def reps_for(con, meeting_type: str) -> list:
    return [r["user_id"] for r in db.all_(
        con, """SELECT b.user_id FROM booking_rep_meeting_types b
                  JOIN app_user u ON u.id = b.user_id
                 WHERE b.meeting_type = ? AND u.is_active = TRUE
              ORDER BY b.user_id""", (meeting_type,))]


def _busy_owner_ids(con, starts_at_utc: str, ends_at_utc: str) -> set:
    rows = db.all_(con, """
        SELECT owner_user_id FROM calendar_events
         WHERE status = 'booked' AND starts_at < ? AND ends_at > ?""",
        (ends_at_utc, starts_at_utc))
    return {r["owner_user_id"] for r in rows}


def choose_rep(con, meeting_type: str, starts_at_utc: str, ends_at_utc: str,
               exclude: set = None) -> Optional[str]:
    """Least-loaded free rep for this topic, or None.

    "Least loaded" rather than round-robin so one person does not collect every
    booking simply by being first alphabetically. The documented overflow
    fallback is the `exclude` set: the caller retries with the loser excluded
    when the database refuses a double booking.
    """
    exclude = exclude or set()
    candidates = [r for r in reps_for(con, meeting_type) if r not in exclude]
    if not candidates:
        return None
    busy = _busy_owner_ids(con, starts_at_utc, ends_at_utc)
    free = [r for r in candidates if r not in busy]
    if not free:
        return None
    loads = {r["owner_user_id"]: r["n"] for r in db.all_(con, """
        SELECT owner_user_id, count(*) AS n FROM calendar_events
         WHERE status = 'booked' AND starts_at >= ?
      GROUP BY owner_user_id""", (starts_at_utc[:10],))}
    free.sort(key=lambda uid: (loads.get(uid, 0), uid))
    return free[0]


# ── confirm ──────────────────────────────────────────────────────────────────

def confirm(con, *, meeting_type: str, starts_at_utc: str, name: str, email: str,
            phone: str = "", note: str = "", source: str = "booking_page",
            lead_id: int = None, request_id: str = None) -> dict:
    """Create the booking.

    A double-click produces one appointment, not two — but idempotency is keyed
    on `request_id`, a random token the booking page generates once per attempt,
    NOT on the customer's details.

    That distinction is the whole point. The first version matched on
    (email, slot, type) and returned the existing booking, which includes
    `public_ref` — the token that lets whoever holds it cancel or move the
    appointment. Every input to that match is guessable: meeting types are
    listed publicly, the slot endpoint publishes ~270 exact start times, and an
    email address is not a secret. Guess an address, walk the slots, collect
    somebody's reference, cancel their meeting. Caught by an automated security
    review on 2026-09-14.
    """
    name, email, phone, note = clean_contact(name, email, phone, note)

    mt = db.one(con, "SELECT * FROM booking_meeting_types WHERE key=?", (meeting_type,))
    if not mt:
        raise BookingError("invalid_meeting_type", "That appointment type is not available.")

    start = _parse_utc(starts_at_utc)
    if start is None:
        raise BookingError("invalid_time", "That appointment time is not valid.")
    if start <= dt.datetime.now(dt.timezone.utc):
        raise BookingError("in_the_past", "That time has already passed. Please pick another.")
    end = start + dt.timedelta(minutes=int(mt["duration_minutes"]))
    s_iso, e_iso = _iso(start), _iso(end)

    # The same submission arriving twice — a double-click, a flaky connection, a
    # retried request. Matched ONLY on the caller's own token, which a stranger
    # cannot produce and which reveals nothing if observed.
    if request_id:
        request_id = str(request_id)[:64]
        existing = db.one(con, "SELECT * FROM calendar_events WHERE request_id=?",
                          (request_id,))
        if existing:
            return _public_view(con, existing)

    if lead_id is None:
        lead_id = _lead_for(con, name, email, phone, source)

    # Try each free rep in turn. The unique index is the real guard; this loop
    # is what turns "someone beat you to it" into "you got the next person"
    # instead of an error the customer has to understand.
    tried = set()
    for _ in range(8):
        rep = choose_rep(con, meeting_type, s_iso, e_iso, exclude=tried)
        if rep is None:
            raise BookingError("slot_taken",
                               "That time was just taken. Please choose another.",
                               status=409)
        ref = new_ref()
        try:
            with con.savepoint():
                con.execute("""
                    INSERT INTO calendar_events
                      (owner_user_id, lead_id, type, title, starts_at, ends_at,
                       status, source, public_ref, booked_name, booked_email,
                       booked_phone, booked_note, request_id)
                    VALUES (?,?,?,?,?,?, 'booked', ?, ?,?,?,?,?,?)""",
                    (rep, lead_id, meeting_type,
                     "%s — %s" % (mt["label_en"], name), s_iso, e_iso,
                     source, ref, name, email, phone, note, request_id))
            con.commit()
        except db.IntegrityError:
            tried.add(rep)          # lost the race for this rep; try the next
            continue

        _after_booking(con, lead_id, meeting_type, s_iso, rep)
        row = db.one(con, "SELECT * FROM calendar_events WHERE public_ref=?", (ref,))
        return _public_view(con, row)

    raise BookingError("slot_taken", "That time was just taken. Please choose another.",
                       status=409)


def _after_booking(con, lead_id: int, meeting_type: str, starts_at: str, rep: str):
    """+20 for a completed booking (D2), the stage move, and the rep's task.

    Deliberately never raises into the customer's request: if scoring or task
    creation fails, the customer still has their appointment. A booking that
    exists but did not score is a reporting problem; a 500 on the booking page
    is a lost meeting.
    """
    try:
        scoring.record(con, lead_id, "booking_completed")
        con.execute(
            "UPDATE leads SET stage='meeting_booked', stage_at=datetime('now')"
            " WHERE id=? AND stage IN ('new','nurturing','engaged')", (lead_id,))
        # tasks.owner_id is a STAFF id, not an app_user id, and `type` is a
        # closed vocabulary — 'prepare', not an invented 'prepare_meeting'.
        # Getting either wrong threw inside this block, which silently rolled
        # back the stage move as well: the booking existed, the lead stayed
        # 'new', and nobody had a task.
        staff_row = db.one(con, "SELECT staff_id FROM app_user WHERE id=?", (rep,))
        owner_staff = staff_row["staff_id"] if staff_row else None
        con.execute("""
            INSERT INTO tasks (lead_id, owner_id, type, due_at, state,
                               created_by, reason)
            VALUES (?,?,?,?, 'open', 'rule', ?)""",
            (lead_id, owner_staff, "prepare", starts_at,
             "Booked from the public page — %s" % meeting_type))
        con.commit()
    except Exception:
        # Deliberately not re-raised: the customer keeps their appointment. But
        # it must be LOUD, because a booking that did not score is invisible in
        # every report that matters.
        log.exception("POST-BOOKING BOOKKEEPING FAILED for lead %s — the booking "
                      "exists but is unscored and has no task", lead_id)
        try:
            con.rollback()
        except Exception:
            pass


def _lead_for(con, name, email, phone, source) -> int:
    """Reuse the existing dedupe path so a booking from someone already in the
    system attaches to them instead of creating a twin — and so first-touch
    attribution survives (D13)."""
    res = ingest.upsert_lead(con, name=name, email=email, phone=phone or None,
                             channel=source)
    con.commit()
    return res["lead_id"]


# ── view / cancel / reschedule ───────────────────────────────────────────────

def by_ref(con, ref: str):
    if not ref or len(ref) < 12:
        return None
    return db.one(con, "SELECT * FROM calendar_events WHERE public_ref=?", (ref,))


def _public_view(con, row) -> dict:
    """What a customer is allowed to see about their own booking.

    Note what is absent: owner_user_id, lead_id, the internal event id, and any
    other person. A customer sees their appointment, not our organisation.
    """
    mt = db.one(con, "SELECT * FROM booking_meeting_types WHERE key=?", (row["type"],))
    return {
        "ref": row["public_ref"],
        "status": row["status"],
        "meeting_type": row["type"],
        "label_en": mt["label_en"] if mt else row["type"],
        "label_ja": mt["label_ja"] if mt else row["type"],
        # ALWAYS carry the offset. The adapter coerces Postgres timestamps to a
        # naive "YYYY-MM-DD HH:MM:SS" string for the scoring engine's benefit,
        # and a browser parses a naive string as LOCAL time. A customer in Dubai
        # confirming 09:00 Tokyo was shown 05:00 on the confirmation screen —
        # caught in a real browser walkthrough, and exactly the class of bug
        # D27b was about. The slots endpoint already returns an offset; this had
        # to match it or the two disagree by the reader's own UTC offset.
        "starts_at": _as_utc_iso(row["starts_at"]),
        "ends_at": _as_utc_iso(row["ends_at"]),
        "name": row["booked_name"],
        "email": row["booked_email"],
        "note": row["booked_note"],
    }


def cancel(con, ref: str, by: str = "customer") -> dict:
    row = by_ref(con, ref)
    if row is None:
        raise BookingError("not_found", "We could not find that appointment.", status=404)
    if row["status"] == "cancelled":
        return _public_view(con, row)          # idempotent
    con.execute("UPDATE calendar_events SET status='cancelled',"
                " cancelled_at=datetime('now'), cancelled_by=? WHERE public_ref=?",
                (by, ref))
    con.execute("UPDATE tasks SET state='cancelled'"
                " WHERE lead_id=? AND type='prepare_meeting' AND state='open'",
                (row["lead_id"],))
    con.commit()
    return _public_view(con, db.one(
        con, "SELECT * FROM calendar_events WHERE public_ref=?", (ref,)))


def reschedule(con, ref: str, new_starts_at_utc: str) -> dict:
    """Cancel then re-confirm, keeping the same person and the same ref.

    Done in that order deliberately: releasing the old slot first means a
    customer moving from 10:00 to 10:30 does not fail because they themselves
    are holding 10:00.
    """
    row = by_ref(con, ref)
    if row is None:
        raise BookingError("not_found", "We could not find that appointment.", status=404)
    if row["status"] == "cancelled":
        raise BookingError("cancelled",
                           "That appointment was cancelled. Please book a new one.")
    start = _parse_utc(new_starts_at_utc)
    if start is None:
        raise BookingError("invalid_time", "That appointment time is not valid.")
    if start <= dt.datetime.now(dt.timezone.utc):
        raise BookingError("in_the_past", "That time has already passed. Please pick another.")

    mt = db.one(con, "SELECT * FROM booking_meeting_types WHERE key=?", (row["type"],))
    end = start + dt.timedelta(minutes=int(mt["duration_minutes"]))
    s_iso, e_iso = _iso(start), _iso(end)
    old_start = row["starts_at"]

    con.execute("UPDATE calendar_events SET status='cancelled',"
                " cancelled_at=datetime('now'), cancelled_by='reschedule'"
                " WHERE public_ref=?", (ref,))
    con.commit()

    tried = set()
    for _ in range(8):
        rep = choose_rep(con, row["type"], s_iso, e_iso, exclude=tried)
        if rep is None:
            break
        try:
            with con.savepoint():
                con.execute("""
                    UPDATE calendar_events
                       SET owner_user_id=?, starts_at=?, ends_at=?, status='booked',
                           cancelled_at=NULL, cancelled_by=NULL, rescheduled_from=?
                     WHERE public_ref=?""",
                    (rep, s_iso, e_iso, old_start, ref))
            con.commit()
            return _public_view(con, db.one(
                con, "SELECT * FROM calendar_events WHERE public_ref=?", (ref,)))
        except db.IntegrityError:
            tried.add(rep)
            continue

    # Could not place it — put the original back rather than leaving the
    # customer with nothing.
    con.execute("UPDATE calendar_events SET status='booked', cancelled_at=NULL,"
                " cancelled_by=NULL WHERE public_ref=?", (ref,))
    con.commit()
    raise BookingError("slot_taken",
                       "That time is not available. Your original appointment is "
                       "unchanged.", status=409)


# ── time helpers ─────────────────────────────────────────────────────────────

def _parse_utc(value) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    s = str(value or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _as_utc_iso(value) -> Optional[str]:
    """Whatever shape the row came back in, hand out an unambiguous instant.

    A value with no offset is UTC by construction here (everything is stored
    that way), but saying so explicitly is the whole point: an ISO string
    without an offset is not an instant, it is a wish.
    """
    d = _parse_utc(value)
    return _iso(d) if d else (str(value) if value else None)
