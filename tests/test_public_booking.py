"""The public booking flow — the only part of this system a stranger touches.

Two categories of test, and the second matters more than the first:

  * it works: list topics, list times, confirm, view, cancel, reschedule
  * it does not leak, and it cannot be raced: no staff identity in any public
    response, no enumeration, no double booking, no confirming whether an
    address is already a customer

The concurrency test is the one that justifies the partial unique index. Two
people confirming the same slot in the same instant is the case that
"check the calendar, then write" always loses.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import api, booking, db  # noqa: E402

client = TestClient(api.app)


@pytest.fixture()
def con():
    db.reset()
    c = db.connect()
    _reps(c, 2)
    yield c
    c.close()


def _reps(con, n=2):
    """Two reps who both take consult_30, so the overflow path has somewhere to
    overflow to.

    Meeting types are reference data seeded by scripts/bootstrap_reference.py,
    not by schema.sql, so a fresh test database has none and the FK on
    booking_rep_meeting_types.meeting_type fails. Seed the ones this file uses.
    """
    for key, en, ja, mins in (("consult_30", "Free 30-min consultation", "無料30分相談", 30),
                              ("site_inspection", "Site inspection", "視察", 120),
                              ("showroom_visit", "Showroom visit", "来店", 60)):
        con.execute("INSERT OR IGNORE INTO booking_meeting_types"
                    " (key, label_en, label_ja, duration_minutes) VALUES (?,?,?,?)",
                    (key, en, ja, mins))
    ids = []
    for i in range(n):
        uid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "booking-rep-%d" % i))
        con.execute("INSERT INTO app_user (id, email, display_name, role, is_active)"
                    " VALUES (?,?,?,?,TRUE)",
                    (uid, "rep%d@exceed-re.ae" % i, "Rep %d" % i, "sales"))
        con.execute("INSERT INTO booking_rep_meeting_types (user_id, meeting_type)"
                    " VALUES (?, 'consult_30')", (uid,))
        ids.append(uid)
    con.execute("INSERT OR IGNORE INTO booking_settings (id) VALUES (1)")
    con.commit()
    return ids


def _next_slot(meeting_type="consult_30"):
    r = client.get("/api/public/booking/slots", params={"meeting_type": meeting_type})
    assert r.status_code == 200, r.text
    slots = r.json()["slots"]
    assert slots, "expected at least one free slot next week"
    return slots[0]["starts_at"]


def _confirm(starts_at, email="hopeful@example.invalid", name="Hopeful Buyer",
             request_id=None):
    payload = {"meeting_type": "consult_30", "starts_at": starts_at,
               "name": name, "email": email, "phone": "090-1111-2222",
               "note": "I would like to know about Dubai."}
    if request_id:
        payload["request_id"] = request_id
    return client.post("/api/public/booking/confirm", json=payload)


# ── it works ─────────────────────────────────────────────────────────────────

def test_topics_are_listed_without_a_login(con):
    r = client.get("/api/public/booking/meeting-types")
    assert r.status_code == 200
    keys = [t["key"] for t in r.json()["data"]]
    assert "consult_30" in keys


def test_a_topic_nobody_covers_is_not_offered(con):
    """Offering a topic no rep takes produces a page where every slot is empty
    and no explanation is given."""
    r = client.get("/api/public/booking/meeting-types")
    keys = [t["key"] for t in r.json()["data"]]
    assert "site_inspection" not in keys      # no rep assigned to it in this fixture


def test_confirm_then_view_then_cancel(con):
    slot = _next_slot()
    r = _confirm(slot)
    assert r.status_code == 200, r.text
    bk = r.json()
    assert bk["status"] == "booked" and bk["starts_at"] == slot
    ref = bk["ref"]

    got = client.get("/api/public/booking/%s" % ref)
    assert got.status_code == 200 and got.json()["ref"] == ref

    cancelled = client.post("/api/public/booking/%s/cancel" % ref)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_cancel_is_idempotent(con):
    ref = _confirm(_next_slot()).json()["ref"]
    client.post("/api/public/booking/%s/cancel" % ref)
    again = client.post("/api/public/booking/%s/cancel" % ref)
    assert again.status_code == 200 and again.json()["status"] == "cancelled"


def test_reschedule_keeps_the_same_reference(con):
    r = client.get("/api/public/booking/slots")
    slots = r.json()["slots"]
    first, second = slots[0]["starts_at"], slots[1]["starts_at"]
    ref = _confirm(first).json()["ref"]
    moved = client.post("/api/public/booking/%s/reschedule" % ref,
                        json={"starts_at": second})
    assert moved.status_code == 200, moved.text
    assert moved.json()["ref"] == ref
    assert moved.json()["starts_at"] == second
    assert moved.json()["status"] == "booked"


def test_booking_creates_a_lead_scores_it_and_makes_a_task(con):
    slot = _next_slot()
    _confirm(slot, email="scored@example.invalid", name="Scored Person")
    c2 = db.connect()
    lead = db.one(c2, "SELECT * FROM leads WHERE name=?", ("Scored Person",))
    assert lead is not None
    assert lead["stage"] == "meeting_booked"
    # D2: a completed booking is +20 and it is a FACT, so it never decays
    assert db.scalar(c2, "SELECT count(*) FROM events WHERE lead_id=? AND kind=?",
                     (lead["id"], "booking_completed")) == 1
    assert db.scalar(c2, "SELECT count(*) FROM tasks WHERE lead_id=? AND type=?",
                     (lead["id"], "prepare")) == 1
    c2.close()


def test_a_double_click_makes_one_booking(con):
    """Idempotency is keyed on the caller's own random token, so the same
    submission arriving twice is one appointment."""
    slot = _next_slot()
    rid = "req-" + uuid.uuid4().hex
    a = _confirm(slot, email="doubleclick@example.invalid", request_id=rid)
    b = _confirm(slot, email="doubleclick@example.invalid", request_id=rid)
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["ref"] == b.json()["ref"]
    c2 = db.connect()
    assert db.scalar(c2, "SELECT count(*) FROM calendar_events WHERE status='booked'") == 1
    c2.close()


def test_knowing_an_email_and_a_slot_does_not_reveal_a_booking(con):
    """The vulnerability this replaced.

    Idempotency used to match on (email, slot, type) and return the existing
    booking — including `public_ref`, which lets whoever holds it CANCEL the
    appointment. Every one of those inputs is guessable: meeting types are
    listed publicly, the slot endpoint publishes ~270 exact start times, and an
    email address is not a secret. Guess an address, walk the slots, cancel
    somebody's meeting.
    """
    slot = _next_slot()
    victim = _confirm(slot, email="victim@example.invalid", name="Victim",
                      request_id="victims-own-token")
    assert victim.status_code == 200
    victim_ref = victim.json()["ref"]

    # The attacker knows the victim's email and can read the slot list. They
    # supply no token, or their own.
    attacker = _confirm(slot, email="victim@example.invalid", name="Attacker")
    assert attacker.status_code != 200 or attacker.json()["ref"] != victim_ref, \
        "handed the attacker the victim's booking reference"

    attacker2 = _confirm(slot, email="victim@example.invalid", name="Attacker",
                         request_id="attackers-own-token")
    assert attacker2.status_code != 200 or attacker2.json()["ref"] != victim_ref, \
        "handed the attacker the victim's booking reference"

    # and the victim's booking is still theirs, still live
    still = client.get("/api/public/booking/%s" % victim_ref)
    assert still.status_code == 200 and still.json()["status"] == "booked"
    assert still.json()["name"] == "Victim"


def test_one_persons_token_cannot_be_reused_to_reach_another_booking(con):
    """A token identifies ONE submission. It is not a password for a slot."""
    slots = client.get("/api/public/booking/slots").json()["slots"]
    a = _confirm(slots[0]["starts_at"], email="a@example.invalid", name="A",
                 request_id="shared-token")
    assert a.status_code == 200
    # the same token, a different slot: returns the FIRST booking (it is the
    # same submission as far as the server can tell) — and critically never a
    # different customer's.
    b = _confirm(slots[1]["starts_at"], email="b@example.invalid", name="B",
                 request_id="shared-token")
    assert b.json()["ref"] == a.json()["ref"]
    assert b.json()["name"] == "A"


# ── it does not leak ─────────────────────────────────────────────────────────

def test_slots_never_reveal_who_works_here(con):
    r = client.get("/api/public/booking/slots")
    body = r.text
    assert "available_rep_user_ids" not in body
    assert "rep0@exceed-re.ae" not in body
    for slot in r.json()["slots"]:
        assert set(slot.keys()) == {"starts_at", "ends_at"}


def test_a_booking_view_reveals_nothing_internal(con):
    ref = _confirm(_next_slot()).json()["ref"]
    body = client.get("/api/public/booking/%s" % ref).json()
    for forbidden in ("owner_user_id", "lead_id", "id", "created_by_user_id"):
        assert forbidden not in body


def test_an_unknown_reference_is_a_flat_404(con):
    """No hint that some references exist and others do not."""
    r = client.get("/api/public/booking/%s" % ("z" * 22))
    assert r.status_code == 404
    r2 = client.post("/api/public/booking/%s/cancel" % ("z" * 22))
    assert r2.status_code == 404


def test_one_customer_cannot_reach_another_customers_booking(con):
    slots = client.get("/api/public/booking/slots").json()["slots"]
    ref_a = _confirm(slots[0]["starts_at"], email="a@example.invalid",
                     name="Customer A").json()["ref"]
    ref_b = _confirm(slots[1]["starts_at"], email="b@example.invalid",
                     name="Customer B").json()["ref"]
    assert ref_a != ref_b
    body = client.get("/api/public/booking/%s" % ref_a).json()
    assert body["name"] == "Customer A"
    assert "Customer B" not in str(body)


def test_the_enquiry_form_does_not_reveal_whether_you_are_already_known(con):
    payload = {"name": "Someone", "email": "known@example.invalid",
               "message": "hello", "consent": True}
    first = client.post("/api/public/enquiry", json=payload)
    second = client.post("/api/public/enquiry", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_enquiry_without_the_consent_box_does_not_manufacture_consent(con):
    client.post("/api/public/enquiry", json={
        "name": "No Consent", "email": "noconsent@example.invalid",
        "message": "just asking", "consent": False})
    c2 = db.connect()
    lead = db.one(c2, "SELECT * FROM leads WHERE name=?", ("No Consent",))
    assert lead is not None, "still a lead the sales team may call"
    assert db.scalar(c2, "SELECT count(*) FROM lead_consent WHERE lead_id=?",
                     (lead["id"],)) == 0, "but NOT someone the sequence may email"
    c2.close()


# ── it cannot be raced or fooled ─────────────────────────────────────────────

def test_two_people_cannot_take_the_same_slot_with_only_one_rep(con):
    """With a single rep the second booking must be refused, not double-booked."""
    c2 = db.connect()
    c2.execute("DELETE FROM booking_rep_meeting_types WHERE user_id <> "
               "(SELECT min(user_id) FROM booking_rep_meeting_types)")
    c2.commit()
    c2.close()
    slot = _next_slot()
    first = _confirm(slot, email="first@example.invalid", name="First")
    second = _confirm(slot, email="second@example.invalid", name="Second")
    assert first.status_code == 200
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "slot_taken"


def test_a_second_booker_overflows_to_the_other_rep(con):
    """Two reps, one slot, two customers — both get an appointment."""
    slot = _next_slot()
    a = _confirm(slot, email="one@example.invalid", name="One")
    b = _confirm(slot, email="two@example.invalid", name="Two")
    assert a.status_code == 200 and b.status_code == 200
    c2 = db.connect()
    rows = db.all_(c2, "SELECT owner_user_id FROM calendar_events WHERE status='booked'")
    assert len(rows) == 2
    assert len({r["owner_user_id"] for r in rows}) == 2, "must be different reps"
    c2.close()


def test_the_database_refuses_a_double_booking_even_if_code_asks_for_one(con):
    """The guarantee is an index, not a code path. Insert straight past the
    application to prove it."""
    slot = _next_slot()
    ref = _confirm(slot).json()["ref"]
    c2 = db.connect()
    row = db.one(c2, "SELECT * FROM calendar_events WHERE public_ref=?", (ref,))
    with pytest.raises(db.IntegrityError):
        c2.execute("""INSERT INTO calendar_events
            (owner_user_id, lead_id, type, starts_at, ends_at, status, public_ref)
            VALUES (?,?,?,?,?, 'booked', ?)""",
            (row["owner_user_id"], row["lead_id"], row["type"], row["starts_at"],
             row["ends_at"], booking.new_ref()))
    c2.close()


def test_a_time_in_the_past_is_refused(con):
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    r = _confirm(past)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "in_the_past"


def test_a_nonsense_time_is_refused(con):
    r = _confirm("not-a-date")
    assert r.status_code == 422


def test_a_bad_email_is_refused_before_anything_is_written(con):
    r = client.post("/api/public/booking/confirm", json={
        "meeting_type": "consult_30", "starts_at": _next_slot(),
        "name": "X", "email": "not-an-email"})
    assert r.status_code == 422
    c2 = db.connect()
    assert db.scalar(c2, "SELECT count(*) FROM calendar_events") == 0
    c2.close()


def test_absurdly_long_input_is_bounded(con):
    r = _confirm(_next_slot(), name="A" * 5000)
    assert r.status_code == 200
    assert len(r.json()["name"]) <= booking.MAX_NAME


def test_cancelling_frees_the_slot_again(con):
    c2 = db.connect()
    c2.execute("DELETE FROM booking_rep_meeting_types WHERE user_id <> "
               "(SELECT min(user_id) FROM booking_rep_meeting_types)")
    c2.commit()
    c2.close()
    slot = _next_slot()
    ref = _confirm(slot, email="leaving@example.invalid").json()["ref"]
    assert _confirm(slot, email="waiting@example.invalid").status_code == 409
    client.post("/api/public/booking/%s/cancel" % ref)
    assert _confirm(slot, email="waiting@example.invalid").status_code == 200


def test_the_confirmation_time_carries_an_offset(con):
    """A booking confirmation with no timezone offset is parsed by the browser
    as LOCAL time. A customer in Dubai confirming 09:00 Tokyo was shown 05:00 —
    found in a real browser walkthrough, not by a unit test."""
    slot = _next_slot()
    body = _confirm(slot).json()
    for field in ("starts_at", "ends_at"):
        v = body[field]
        assert v.endswith("+00:00") or v.endswith("Z"), \
            "%s=%r has no timezone offset" % (field, v)
    # and it must be the SAME instant the slot list offered
    assert dt.datetime.fromisoformat(body["starts_at"]) == \
        dt.datetime.fromisoformat(slot)


def test_view_and_reschedule_also_carry_an_offset(con):
    slots = client.get("/api/public/booking/slots").json()["slots"]
    ref = _confirm(slots[0]["starts_at"]).json()["ref"]
    got = client.get("/api/public/booking/%s" % ref).json()
    assert got["starts_at"].endswith("+00:00")
    moved = client.post("/api/public/booking/%s/reschedule" % ref,
                        json={"starts_at": slots[1]["starts_at"]}).json()
    assert moved["starts_at"].endswith("+00:00")
    assert dt.datetime.fromisoformat(moved["starts_at"]) == \
        dt.datetime.fromisoformat(slots[1]["starts_at"])
