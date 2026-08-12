"""The four passive signals (D1).

  open              +1   invisible 1px image in the email
  click             +3   every link redirects through here first
  page_view         +3   the redirect carries the lead ID onto the website
  booking_page_view +5   same machinery, one specific URL

All four are the same trick: each person's email carries a unique send_id, and
that string is what makes every later signal attributable to a human being.

Also here: the SendGrid webhook receiver, and the rules layer that drops
machine replies before the AI is ever asked to read them (D7).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import scoring

# a 1×1 transparent GIF, the smallest legal one
PIXEL = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100"
    "0100000202444c003b")

BOOKING_PATHS = {"/book", "/booking", "/reserve", "/予約"}


def _lead_for_send(con: sqlite3.Connection, send_id: str):
    row = con.execute("SELECT lead_id FROM sends WHERE send_id=?", (send_id,)).fetchone()
    return row["lead_id"] if row else None


def _record_and_check(con, lead_id, kind, detail=None, send_id=None, source="sendgrid"):
    """Append the event, then report whether this is the moment the lead
    crossed the notify threshold. Only the crossing fires a notification —
    otherwise a hot lead pings the rep on every single click."""
    before = scoring.score(con, lead_id)
    scoring.record(con, lead_id, kind, detail=detail, send_id=send_id, source=source)
    after = scoring.score(con, lead_id)
    return {"lead_id": lead_id, "kind": kind, "before": before, "after": after,
            "crossed": scoring.crossed_threshold(con, lead_id, before, after)}


def open_pixel(con: sqlite3.Connection, send_id: str):
    """GET /o/<send_id>.gif — their mail app fetched the image.

    Weakest signal on the list and worth exactly +1 for a reason: Apple Mail
    Privacy Protection pre-loads this before a human sees anything, so a real
    share of these are a machine. Never let anyone score opens higher.
    """
    lead_id = _lead_for_send(con, send_id)
    if lead_id:
        _record_and_check(con, lead_id, "open", send_id=send_id)
    return PIXEL


def click(con: sqlite3.Connection, send_id: str, dest: str):
    """GET /c/<send_id>?u=<dest> — log, then redirect. A machine does not click.

    Returns the URL to redirect to, with the lead key appended so the website
    knows who arrived (that is what makes page_view possible at all).
    """
    lead_id = _lead_for_send(con, send_id)
    if lead_id:
        _record_and_check(con, lead_id, "click", detail=dest, send_id=send_id)
    joiner = "&" if "?" in dest else "?"
    return "%s%sk=%s" % (dest, joiner, send_id)


def page_view(con: sqlite3.Connection, send_id: str, path: str):
    """POST /e — the tag on your own site, carrying the k= from the redirect.

    Only ever works for someone who arrived from your email. Anyone who finds
    the same page through Google is unknown and unscoreable until they hand
    over an address — which is the entire reason the LP form matters.
    """
    lead_id = _lead_for_send(con, send_id)
    if not lead_id:
        return None
    kind = "booking_page_view" if any(path.startswith(p) for p in BOOKING_PATHS) else "page_view"
    return _record_and_check(con, lead_id, kind, detail=path, send_id=send_id, source="web")


# ── SendGrid webhook ─────────────────────────────────────────────────────────

SENDGRID_MAP = {
    "open": "open",
    "click": "click",
    "bounce": "bounce",
    "dropped": "bounce",
    "unsubscribe": "unsubscribe",
    "spamreport": "unsubscribe",
}


def sendgrid_webhook(con: sqlite3.Connection, payload: list) -> dict:
    """POST /webhooks/email — SendGrid pushes; we never poll."""
    handled, crossed = 0, []
    for ev in payload or []:
        kind = SENDGRID_MAP.get(ev.get("event"))
        send_id = (ev.get("send_id") or ev.get("custom_args", {}).get("send_id"))
        if not kind or not send_id:
            continue
        lead_id = _lead_for_send(con, send_id)
        if not lead_id:
            continue

        if kind == "bounce":
            con.execute("UPDATE sends SET status='bounced' WHERE send_id=?", (send_id,))
            con.execute("""UPDATE lead_identities SET status='bounced'
                            WHERE lead_id=? AND kind='email'""", (lead_id,))
            # D10: a dead address is a real end state, not a lead sitting in limbo
            con.execute("""UPDATE leads SET stage='unreachable', stage_at=datetime('now'),
                           stage_reason='email bounced' WHERE id=?""", (lead_id,))
            scoring.record(con, lead_id, "bounce", detail=ev.get("reason"),
                           send_id=send_id, source="sendgrid")
        elif kind == "unsubscribe":
            # not optional — 特定電子メール法. A hard state the system enforces.
            con.execute("""UPDATE leads SET stage='unsubscribed', stage_at=datetime('now')
                            WHERE id=?""", (lead_id,))
            con.execute("""UPDATE lead_consent SET basis='withdrawn',
                           withdrawn_at=datetime('now'), updated_at=datetime('now')
                            WHERE lead_id=?""", (lead_id,))
            scoring.record(con, lead_id, "unsubscribe", send_id=send_id, source="sendgrid")
        else:
            r = _record_and_check(con, lead_id, kind, detail=ev.get("url"), send_id=send_id)
            if r["crossed"]:
                crossed.append(r)
        handled += 1
    con.commit()
    return {"handled": handled, "crossed_threshold": crossed}


# ── reply detection, layer 1 (D7) ────────────────────────────────────────────
# Rules first, free, deterministic. Only what survives is worth paying an AI to
# read — and the AI answers four questions in the same pass anyway.

AUTO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond",
                "precedence: bulk", "precedence: auto_reply", "x-auto-response-suppress")
AUTO_SENDERS = ("mailer-daemon", "postmaster", "noreply", "no-reply", "donotreply")
AUTO_SUBJECTS = ("out of office", "automatic reply", "auto:", "自動応答", "不在",
                 "休暇", "自動返信", "undeliverable", "delivery status notification")


def looks_automated(headers: dict = None, sender: str = "", subject: str = "") -> tuple:
    """(is_machine, why). Catches roughly 90% for nothing.

    The Japanese corporate auto-responders that carry no headers at all and read
    like a real message are exactly what the AI layer is for.
    """
    blob = " ".join("%s: %s" % (k.lower(), v) for k, v in (headers or {}).items()).lower()
    for h in AUTO_HEADERS:
        if h in blob:
            return True, "header %s" % h
    s = (sender or "").lower()
    for a in AUTO_SENDERS:
        if a in s:
            return True, "sender %s" % a
    sub = (subject or "").lower()
    for a in AUTO_SUBJECTS:
        if a in sub:
            return True, "subject %s" % a
    return False, ""


def record_reply(con: sqlite3.Connection, lead_id: int, *, subject: str = "",
                 sender: str = "", headers: dict = None, body: str = "",
                 ai_verdict: dict = None) -> dict:
    """Layer 1 then layer 2.

    ai_verdict is what the AI returns after reading a surviving message —
    one pass, four questions (D7):
        {"human": bool, "wants_meeting": bool,
         "partnership": bool, "high_budget": bool}
    Nothing is scored until the AI says a human wrote it.
    """
    machine, why = looks_automated(headers, sender, subject)
    if machine:
        return {"scored": False, "reason": "filtered by rules: %s" % why}

    if ai_verdict is None:
        return {"scored": False, "reason": "awaiting AI read", "queued": True}
    if not ai_verdict.get("human"):
        return {"scored": False, "reason": "AI: not a human reply"}

    fired = []
    r = _record_and_check(con, lead_id, "reply", detail=subject, source="ai")
    fired.append("reply")

    # the three 30-pointers, from the same single pass
    for flag, kind in (("wants_meeting", "wants_meeting"),
                       ("partnership", "partnership"),
                       ("high_budget", "high_budget")):
        if ai_verdict.get(flag):
            scoring.record(con, lead_id, kind, detail="AI read of reply", source="ai")
            fired.append(kind)

    # D5a — relationship is derived from scoring, never hand-picked
    if ai_verdict.get("partnership"):
        con.execute("UPDATE leads SET relationship='partner' WHERE id=?", (lead_id,))
    con.execute("""UPDATE leads SET stage='responded', stage_at=datetime('now')
                    WHERE id=? AND stage IN ('new','nurturing')""", (lead_id,))
    con.commit()

    after = scoring.score(con, lead_id)
    return {"scored": True, "events": fired, "score": after,
            "crossed": r["crossed"] or after >= scoring.threshold(con)}
