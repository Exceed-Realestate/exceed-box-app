"""Intake and deduplication.

D13 — Balraj: "merged them together, and put all three in a sequence, how you
got the lead". So a second arrival of the same person NEVER creates a second
lead. It attaches a channel and, if new, an identity.

D11 — source is two things:
  first_touch  never changes; what the dashboard attributes a booking to (D16)
  channels[]   grows; what stops you emailing the same person three times

Matching, in order of confidence:
  1. email exact           — near-certain
  2. phone exact           — near-certain
  3. name + company exact  — good enough for a business card with no email
Nothing fuzzier than that is done automatically. Anything less certain should
land in a review queue rather than silently merging two real people together —
an unwanted merge is much harder to undo than an unwanted duplicate.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

CHANNELS = {"business_card", "csv", "gohighlevel", "lp_form", "sns", "gmail",
            "referral", "whatsapp", "line", "showroom", "property_finder"}

# consent defaults per channel (D11 — the table that decides who you may email)
CONSENT_BY_CHANNEL = {
    "lp_form":        ("explicit",  "LP form checkbox"),
    "showroom":       ("explicit",  "iPad opt-in at the showroom"),
    "gmail":          ("implied",   "they contacted Exceed first"),
    "business_card":  ("ambiguous", "card exchanged in person"),
    "referral":       ("ambiguous", "introduced by a third party"),
    "gohighlevel":    ("unknown",   "check during the CRM audit"),
    "csv":            ("unknown",   "no record of when or where obtained"),
    "sns":            ("unknown",   "a DM is not permission to email"),
    "whatsapp":       ("unknown",   ""),
    "line":           ("unknown",   ""),
    "property_finder": ("implied",  "portal enquiry"),
}


def norm_email(v):
    if not v:
        return None
    v = v.strip().lower()
    return v if "@" in v and "." in v.split("@")[-1] else None


def norm_phone(v):
    """Normalise to the national form so the same person written three ways
    is one person.

        03-1234-5678       → 0312345678
        +81 3 1234 5678    → 0312345678
        +971 50 123 4567   → 0501234567

    Naively keeping "the last 11 digits" does NOT work: the +81 form is 11
    digits and the 0 form is 10, so they never matched. A real bug, found by
    test_d13_phone_matches_across_formats.
    """
    if not v:
        return None
    digits = re.sub(r"\D", "", str(v))
    if digits.startswith("0081"):
        digits = digits[4:]
    if digits.startswith("81") and len(digits) >= 11:      # Japan
        digits = "0" + digits[2:]
    elif digits.startswith("971") and len(digits) >= 12:   # UAE
        digits = "0" + digits[3:]
    if len(digits) < 8:
        return None
    return digits


def find_existing(con: sqlite3.Connection, *, email=None, phone=None,
                  name=None, company=None):
    """Return (lead_id, how_matched) or (None, None)."""
    e, p = norm_email(email), norm_phone(phone)
    if e:
        row = con.execute(
            """SELECT l.id FROM lead_identities i JOIN leads l ON l.id=i.lead_id
                WHERE i.kind='email' AND i.value=? AND l.merged_into IS NULL""", (e,)).fetchone()
        if row:
            return row["id"], "email"
    if p:
        row = con.execute(
            """SELECT l.id FROM lead_identities i JOIN leads l ON l.id=i.lead_id
                WHERE i.kind='phone' AND i.value=? AND l.merged_into IS NULL""", (p,)).fetchone()
        if row:
            return row["id"], "phone"
    if name and company:
        row = con.execute(
            """SELECT id FROM leads
                WHERE name=? AND company=? AND merged_into IS NULL""", (name, company)).fetchone()
        if row:
            return row["id"], "name+company"
    return None, None


def _add_identity(con, lead_id, kind, value):
    if not value:
        return
    try:
        con.execute(
            "INSERT INTO lead_identities (lead_id, kind, value) VALUES (?,?,?)",
            (lead_id, kind, value))
    except sqlite3.IntegrityError:
        pass                      # already known, on this lead or another


def _add_channel(con, lead_id, channel, note=None, seen_at=None):
    if channel not in CHANNELS:
        raise ValueError("unknown channel: %s" % channel)
    try:
        con.execute(
            "INSERT INTO lead_channels (lead_id, channel, note, seen_at) VALUES (?,?,?,?)",
            (lead_id, channel, note,
             (seen_at or datetime.utcnow()).isoformat(sep=" ", timespec="seconds")))
    except sqlite3.IntegrityError:
        pass                      # already seen through this channel


def _set_consent_if_stronger(con, lead_id, channel, when=None):
    """Never downgrade. If a person arrived by CSV (unknown) and later fills in
    an LP form (explicit), consent improves. The reverse must not happen."""
    rank = {"withdrawn": -1, "unknown": 0, "ambiguous": 1, "implied": 2, "explicit": 3}
    basis, via = CONSENT_BY_CHANNEL.get(channel, ("unknown", ""))
    cur = con.execute("SELECT basis FROM lead_consent WHERE lead_id=?", (lead_id,)).fetchone()
    if cur and cur["basis"] == "withdrawn":
        return                    # a withdrawal is final until the person says otherwise
    if cur and rank.get(cur["basis"], 0) >= rank.get(basis, 0):
        return
    ts = (when or datetime.utcnow()).isoformat(sep=" ", timespec="seconds")
    con.execute(
        """INSERT INTO lead_consent (lead_id, basis, obtained_at, obtained_via, updated_at)
           VALUES (?,?,?,?,datetime('now'))
           ON CONFLICT(lead_id) DO UPDATE SET
             basis=excluded.basis, obtained_at=excluded.obtained_at,
             obtained_via=excluded.obtained_via, updated_at=datetime('now')""",
        (lead_id, basis, ts if basis != "unknown" else None, via))


def upsert_lead(con: sqlite3.Connection, *, name, channel, company=None, title=None,
                email=None, phone=None, note=None, seen_at=None,
                owner_id=None) -> dict:
    """The single entry point for every source. Returns what happened."""
    e, p = norm_email(email), norm_phone(phone)
    lead_id, matched = find_existing(con, email=e, phone=p, name=name, company=company)
    created = lead_id is None
    when = seen_at or datetime.utcnow()

    if created:
        cur = con.execute(
            """INSERT INTO leads (name, company, title, first_touch, first_touch_at,
                                  first_touch_note, owner_id)
               VALUES (?,?,?,?,?,?,?)""",
            (name, company, title, channel,
             when.isoformat(sep=" ", timespec="seconds"), note, owner_id))
        lead_id = cur.lastrowid
        con.execute("INSERT INTO events (lead_id, kind, detail, source) VALUES (?,?,?,?)",
                    (lead_id, "imported", "via %s" % channel, "system"))
    else:
        # enrich, never overwrite something with nothing
        if company:
            con.execute("UPDATE leads SET company=COALESCE(NULLIF(company,''),?) WHERE id=?",
                        (company, lead_id))
        if title:
            con.execute("UPDATE leads SET title=COALESCE(NULLIF(title,''),?) WHERE id=?",
                        (title, lead_id))
        con.execute("UPDATE leads SET updated_at=datetime('now') WHERE id=?", (lead_id,))
        con.execute("INSERT INTO events (lead_id, kind, detail, source) VALUES (?,?,?,?)",
                    (lead_id, "merged", "second arrival via %s (matched on %s)" % (channel, matched),
                     "system"))

    _add_identity(con, lead_id, "email", e)
    _add_identity(con, lead_id, "phone", p)
    _add_channel(con, lead_id, channel, note, when)
    _set_consent_if_stronger(con, lead_id, channel, when)
    con.commit()

    return {"lead_id": lead_id, "created": created, "merged": not created,
            "matched_on": matched, "channel": channel}


def channels(con: sqlite3.Connection, lead_id: int) -> list:
    """D13: 'this person arrived from three different channels', in order."""
    return [dict(r) for r in con.execute(
        "SELECT channel, note, seen_at FROM lead_channels WHERE lead_id=? ORDER BY seen_at",
        (lead_id,))]


# ── CSV import wizard (D11) ──────────────────────────────────────────────────
# Balraj: the importer must interrogate the file, not silently guess. This is
# the question generator; the answers get stored on the imports row so that
# "where did these 2,000 people come from" is answerable a year later.

FIELD_HINTS = {
    "name":    ["name", "氏名", "名前", "お名前", "full name", "担当者"],
    "company": ["company", "会社", "会社名", "法人名", "勤務先", "organisation", "organization"],
    "title":   ["title", "役職", "position", "肩書"],
    "email":   ["email", "e-mail", "mail", "メール", "メールアドレス"],
    "phone":   ["phone", "tel", "電話", "電話番号", "携帯"],
}


def guess_mapping(headers: list) -> dict:
    """Best guess per column — offered to the human, never applied silently."""
    out = {}
    for h in headers:
        key = (h or "").strip().lower()
        for field, hints in FIELD_HINTS.items():
            if any(hint in key for hint in hints):
                out[h] = field
                break
        else:
            out[h] = None
    return out


def import_questions(headers: list, sample_rows: list) -> list:
    """What the wizard asks before a single row is written."""
    mapping = guess_mapping(headers)
    qs = []

    unmapped = [h for h, f in mapping.items() if f is None]
    if unmapped:
        qs.append({
            "id": "unmapped_columns",
            "question": "I could not work out what these columns are: %s. What is in them?"
                        % ", ".join(unmapped),
            "why": "Guessing wrong here silently corrupts every row.",
            "required": True,
        })

    if "email" not in mapping.values():
        qs.append({
            "id": "no_email",
            "question": "There is no email column. These people cannot be sent a "
                        "sequence — import them anyway as card-only contacts?",
            "why": "The whole nurture engine needs an email address.",
            "required": True,
        })

    qs.append({
        "id": "consent",
        "question": "Where did this list come from, and do you know when these "
                    "people agreed to be contacted?",
        "why": "特定電子メール法 requires prior consent. 'Unknown' is an acceptable "
               "answer and will be recorded as such — but it must be recorded.",
        "required": True,
    })
    qs.append({
        "id": "region_purpose",
        "question": "Is this list all one thing — all Dubai, all investors, one "
                    "campaign — or mixed?",
        "why": "If it is all one thing, region and purpose can be set on import "
               "instead of waiting for the sequence to discover them.",
        "required": False,
    })
    qs.append({
        "id": "age",
        "question": "Roughly how old is this data?",
        "why": "Anything over two years will have dead addresses. They get cleaned "
               "before the first send, not after — bounces damage the sending domain.",
        "required": True,
    })
    return qs
