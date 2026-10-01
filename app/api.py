"""HTTP surface.

Two kinds of endpoint:

  public tracking   /o /c /e /webhooks/email
                    these are hit by mail clients, browsers and SendGrid.
                    No auth, by necessity — the send_id is the credential.

  internal JSON     /api/*
                    what the app reads. Every route requires a Supabase JWT
                    (or, only when EXCEEDBOX_DEV_AUTH=1 and no
                    SUPABASE_JWT_SECRET is set, a local dev token — see
                    app/auth.py and app/devauth.py) except /api/health.

RBAC (SPEC.md "RBAC — the heart of this task") happens in two places, on
purpose:
  - row-level filtering lives in the SQL WHERE clause of each list query, so
    a role that cannot see another rep's rows never fetches them into memory
    in the first place;
  - capability checks (can this role even call this endpoint / act on this
    row) live in app/auth.py's ROLE_CAPS and are enforced with auth.require(),
    which raises a real 403 — never an empty 200.

Every response shape below is the SPEC.md contract, not whatever fell out of
the query first — see AUDIT.md for the drift this file fixes. Where the two
still leave a real design choice open (e.g. how a lead's pipeline stage and
its terminal exit_state interact on one endpoint), the choice and its
reasoning is written down inline, not left implicit.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import (auth, booking, csvimport, db, devauth, ingest, mailer, media,
               push as pushmod, replyai, scoring, tasks, tracking, voice)

log = logging.getLogger("exceedbox.api")

app = FastAPI(title="Exceed Box API", version="0.3.0")

# ── CORS ────────────────────────────────────────────────────────────────────
# The iOS/Android builds are native and never send an Origin header, so this
# matters for exactly one client: `expo start --web`, which is how the app gets
# demoed on a laptop. Without it the browser blocks every /api/* call and the
# app looks broken while the server logs look perfectly healthy.
#
# Deliberately an explicit allow-list, never `["*"]`: credentials are sent as an
# Authorization header, and a wildcard origin on an API that hands out lead
# contact details is the kind of default that quietly survives into production.
# Override with EXCEEDBOX_CORS_ORIGINS (comma-separated) when the web build is
# served from a real hostname; the localhost defaults are useless to an attacker
# on any machine but the developer's own.
_DEFAULT_CORS = "http://localhost:8081,http://localhost:8099,http://localhost:19006,http://127.0.0.1:8081,http://127.0.0.1:8099,http://127.0.0.1:19006"
CORS_ORIGINS = [o.strip() for o in os.environ.get("EXCEEDBOX_CORS_ORIGINS", _DEFAULT_CORS).split(",") if o.strip()]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def con():
    return db.connect()


# Content types we are willing to hand back from the media endpoint. An
# allow-list, not mimetypes.guess_type: a stored extension originates in a
# user-supplied filename, and echoing an arbitrary guessed type back (say
# text/html) would turn an uploaded "card" into stored XSS on our own origin.
_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".heic": "image/heic", ".webp": "image/webp", ".gif": "image/gif",
    ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".caf": "audio/x-caf", ".aac": "audio/aac",
    ".csv": "text/csv", ".xlsx":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _media_type_for(filename: str) -> str:
    import os as _os
    return _MEDIA_TYPES.get(_os.path.splitext(filename or "")[1].lower(),
                            "application/octet-stream")


def err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


async def _optional_json(req: Request) -> dict:
    """Several SPEC-V2 §10 lead-drawer actions are meaningful with an empty
    body (e.g. 'follow' with no note) — a plain `await req.json()` 500s on an
    empty body instead of 422ing cleanly, so callers that want an optional
    body use this instead."""
    try:
        body = await req.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


@app.exception_handler(HTTPException)
async def _http_exc_handler(_request, exc: HTTPException):
    """Every error is { error: { code, message } } — SPEC.md's shape, not
    FastAPI's default { detail: ... } wrapper."""
    body = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else \
        {"error": {"code": "http_error", "message": str(exc.detail)}}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(_request, exc: RequestValidationError):
    return JSONResponse(status_code=422,
                        content={"error": {"code": "validation_error", "message": exc.errors()}})


@app.on_event("startup")
def _log_auth_mode():
    # app/auth.py already warns loudly at import time; this just confirms
    # which mode is actually live once uvicorn is up.
    mode = ("supabase (jwks)" if auth.SUPABASE_JWKS_URL else
            "supabase (legacy hs256)" if auth.SUPABASE_JWT_SECRET else
            "dev" if auth.DEV_AUTH_ENABLED else
            "none — every /api/* call will 401")
    print("[exceedbox] auth mode: %s" % mode)


def paginate(items: list, page: int, page_size: int) -> dict:
    """SPEC.md: 'Every list endpoint is paginated and returns
    { data, page, page_size, total }.' Used for lists the scoring engine has
    to build in Python anyway (per-row decay math, not expressible in SQL) —
    role filtering for those still happens in the SQL that produced `items`,
    this only slices the page."""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    total = len(items)
    start = (page - 1) * page_size
    return {"data": items[start:start + page_size], "page": page, "page_size": page_size, "total": total}


# ══ public tracking ═════════════════════════════════════════════════════════

@app.get("/o/{send_id}.gif")
def track_open(send_id: str):
    c = con()
    try:
        return Response(content=tracking.open_pixel(c, send_id), media_type="image/gif",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    finally:
        c.close()


@app.get("/c/{send_id}")
def track_click(send_id: str, u: str = "/"):
    c = con()
    try:
        return RedirectResponse(tracking.click(c, send_id, u), status_code=302)
    finally:
        c.close()


@app.get("/u/{send_id}")
def unsubscribe_page(send_id: str):
    """The unsubscribe link every nurture email carries.

    Public and unauthenticated by necessity — a recipient is a customer, not a
    user of this system, and demanding a login to stop email would be both
    hostile and, for bulk mail, non-compliant.

    `send_id` is the only credential and it is deliberately weak-but-scoped: it
    is unguessable (secrets.token_urlsafe), it identifies exactly one message to
    one address, and the ONLY thing it can do is stop mail to that address.
    Knowing it reveals no customer record and grants no read access.

    GET shows a confirmation rather than acting, so that a mail client or
    security scanner pre-fetching the link cannot unsubscribe someone who never
    clicked. The POST below is what actually suppresses.
    """
    c = con()
    try:
        row = db.one(c, "SELECT to_email FROM sends WHERE send_id=?", (send_id,))
        if not row:
            return Response(content=_unsub_html(
                "This link is not valid.",
                "It may have already been used, or the message may be very old."),
                media_type="text/html", status_code=404)
        already = mailer.is_suppressed(c, row["to_email"])
        if already:
            return Response(content=_unsub_html(
                "You are already unsubscribed.",
                "No further emails will be sent to this address."),
                media_type="text/html")
        return Response(content=_unsub_html(
            "Unsubscribe from these emails?",
            "Press the button and we will stop emailing this address.",
            form_action="/u/%s" % send_id), media_type="text/html")
    finally:
        c.close()


@app.post("/u/{send_id}")
def unsubscribe(send_id: str):
    """Acts. Idempotent, and honoured immediately: the suppression is keyed on
    the ADDRESS, so it also stops mail to the same person arriving as a second
    or third lead, and `mailer.run_once` re-checks it before every send — a
    message already sitting in the queue is cancelled, not delivered.

    Also handles the RFC 8058 one-click POST that Gmail and Yahoo send directly
    from their own UI, which is why the List-Unsubscribe-Post header names it.
    """
    c = con()
    try:
        row = db.one(c, "SELECT lead_id, to_email FROM sends WHERE send_id=?", (send_id,))
        if not row:
            return Response(content=_unsub_html(
                "This link is not valid.", "Nothing was changed."),
                media_type="text/html", status_code=404)
        mailer.suppress(c, row["to_email"], "unsubscribed",
                        detail="via unsubscribe link", source="recipient")
        # Permission to contact is separate from sales stage: the enrolment
        # stops, the lead's stage is left exactly where the sales team put it.
        mailer.set_enrolment_state(c, row["lead_id"], "stopped", "unsubscribed")
        c.execute("UPDATE sends SET status='cancelled', last_error='unsubscribed',"
                  " updated_at=datetime('now')"
                  " WHERE lead_id=? AND status IN ('queued','sending')",
                  (row["lead_id"],))
        c.commit()
        return Response(content=_unsub_html(
            "You have been unsubscribed.",
            "We will not email this address again."), media_type="text/html")
    finally:
        c.close()


def _unsub_html(title: str, body: str, form_action: str = None) -> str:
    """Deliberately plain, self-contained and free of tracking. Escaped because
    everything on this page is ours, but the habit is what stops the next
    person interpolating a lead name in here."""
    from html import escape
    button = ""
    if form_action:
        button = ('<form method="post" action="%s">'
                  '<button type="submit">Unsubscribe</button></form>'
                  % escape(form_action))
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title><style>"
        "body{font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#15171C;color:#F4F5F7;display:flex;min-height:100vh;margin:0;"
        "align-items:center;justify-content:center;padding:24px}"
        "main{max-width:420px}h1{font-size:20px;margin:0 0 12px}"
        "p{color:#A9AFBA;margin:0 0 20px}"
        "button{background:#FFD84D;color:#15171C;border:0;border-radius:10px;"
        "padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer}"
        "</style></head><body><main><h1>%s</h1><p>%s</p>%s</main></body></html>"
        % (escape(title), escape(title), escape(body), button))


@app.post("/e")
async def track_page(req: Request):
    body = await req.json()
    c = con()
    try:
        r = tracking.page_view(c, body.get("k", ""), body.get("path", "/"))
        return JSONResponse(r or {"ignored": True, "reason": "unknown visitor"})
    finally:
        c.close()


@app.post("/webhooks/email")
async def sendgrid(req: Request):
    payload = await req.json()
    c = con()
    try:
        return tracking.sendgrid_webhook(c, payload)
    finally:
        c.close()


# ══ auth / identity ═════════════════════════════════════════════════════════

@app.get("/api/me")
def me(user: auth.CurrentUser = Depends(auth.current_user)):
    return user.as_dict()


@app.get("/api/devtoken")
def devtoken(email: str):
    """Dev convenience only — mints a token for `email` via the local dev
    signer. 404s unless EXCEEDBOX_DEV_AUTH=1 and no SUPABASE_JWT_SECRET is
    set, i.e. it does nothing at all in anything resembling production.
    Role/activation still come from the app_user row (seed.py or POST
    /api/users), not from this token.

    N1 (FABLE-AUDIT-R2.md) — `sub` is deterministic per email (uuid5, not a
    fresh uuid4 every call) so re-running the same curl for the same person
    keeps working after their first login: a real login keeps one stable
    `sub` across requests, and load_or_provision() now correctly rejects a
    SECOND, different `sub` claiming an email already bound to a first one
    (that rejection is the point of N1 — see tests/test_c1_media_security.py
    ::test_n1_sub_mismatch_cannot_take_over_an_existing_account) — a
    dev-convenience endpoint minting a random sub every call would trip its
    own security fix on the second curl."""
    if not auth.DEV_AUTH_ENABLED:
        raise err(404, "not_found", "Dev auth is disabled.")
    dev_sub = str(uuid.uuid5(uuid.NAMESPACE_DNS, "exceedbox-dev:%s" % email))
    return {"token": devauth.mint(sub=dev_sub, email=email)}


# ══ shared vocabulary ═══════════════════════════════════════════════════════
# SPEC.md D10 migration: six forward stages, moved through in order, plus a
# SEPARATE exit_state for the four ways a lead leaves the funnel without
# winning. The two used to be crammed into one `stage` column (AUDIT.md
# critical finding) — schema.sql/migrations now enforce the split with a
# CHECK constraint, so this vocabulary and the database can never drift
# silently again.
FORWARD_STAGES = ("new", "nurturing", "engaged", "meeting_booked", "in_negotiation", "won")
EXIT_STATES = ("lost", "too_early", "unreachable", "unsubscribed")

STAGE_LABELS = {
    "new":            ("New", "新規"),
    "nurturing":      ("Nurturing", "育成中"),
    "engaged":        ("Engaged", "反応あり"),
    "meeting_booked": ("Meeting booked", "商談予約済み"),
    "in_negotiation": ("In negotiation", "商談中"),
    "won":            ("Won", "成約"),
}

REGIONS = ("dubai", "lombok", "japan")
REGION_LABELS = {"dubai": ("Dubai", "ドバイ"), "lombok": ("Lombok", "ロンボク"), "japan": ("Japan", "日本")}

PURPOSES = ("investment", "relocation", "second_home", "business_base", "unknown")

SOURCE_LABELS = {
    "business_card":   ("Business card", "名刺"),
    "csv":             ("CSV import", "CSVインポート"),
    "gohighlevel":     ("GoHighLevel", "GoHighLevel"),
    "lp_form":         ("Landing page", "LPフォーム"),
    "sns":             ("SNS", "SNS"),
    "gmail":           ("Email reply", "メール返信"),
    "referral":        ("Referral", "紹介"),
    "whatsapp":        ("WhatsApp", "WhatsApp"),
    "line":            ("LINE", "LINE"),
    "showroom":        ("Showroom", "ショールーム"),
    "property_finder": ("Property Finder", "Property Finder"),
}

EDITABLE_LEAD_FIELDS = {"name", "name_kana", "company", "title", "purpose",
                        "first_touch_note", "stage_reason", "revisit_at"}
# email/phone are not raw columns — they are lead_identities rows, edited
# through dedupe/conflict rules (see _upsert_identity), never a blind UPDATE.
IDENTITY_FIELDS = {"email", "phone"}


# ══ timezone (FABLE-AUDIT.md "Timezone — real appointment bug") ═════════════
# Booking slots and task due dates used to be naive ISO strings — no
# timezone at all. Tokyo (JST, UTC+9) and Dubai (GST, UTC+4) are FIVE hours
# apart, not four (the decision log said four, twice — verified wrong and
# fixed alongside this). A Dubai-entered slot read in Tokyo landed in the
# wrong hour with no way to even detect it.
#
# Fix: everything that carries a real clock time is stored as timezone-aware
# UTC and returned as ISO-8601 with an explicit offset. A naive string
# coming FROM a client is interpreted in THAT CALLER's own office timezone —
# never silently assumed to be UTC, and never silently assumed to be Tokyo's
# — so a Dubai rep's naive "14:00" is read as 14:00 GST, not 14:00 JST.
OFFICE_TIMEZONES = {"tokyo": "Asia/Tokyo", "dubai": "Asia/Dubai"}
DEFAULT_OFFICE = "tokyo"   # SPEC.md: "The Japan office is the primary user"


def _office_zone(office: Optional[str]) -> ZoneInfo:
    return ZoneInfo(OFFICE_TIMEZONES.get(office or "", OFFICE_TIMEZONES[DEFAULT_OFFICE]))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    """Timezone-aware UTC, ISO-8601, explicit offset — never a naive string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_office_datetime(raw, office: Optional[str]) -> datetime:
    """A client-supplied timestamp. If it already carries an offset (or 'Z'),
    trust it. If it is naive, interpret it in the CALLER's own office
    timezone — never a blind UTC/JST guess. Always returns an aware UTC
    datetime."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_office_zone(office))
    return dt.astimezone(timezone.utc)


def _is_datetime_like(raw) -> bool:
    """set-contact-date's `date`/a lead's `revisit_at` are calendar DATES,
    not clock times — 'YYYY-MM-DD' has no hour to get wrong, so it is left
    alone rather than forced through office-timezone parsing. Anything
    longer (has a time component) goes through the parser above."""
    return isinstance(raw, str) and len(raw.strip()) > 10


# ══ helpers shared by the /api/leads/* family ═══════════════════════════════

def _lead_row(c, lead_id: int):
    return c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def _can_view_lead(user: auth.CurrentUser, lead) -> bool:
    return user.can("leads.all") or lead["owner_user_id"] == user.id


def _can_edit_lead(user: auth.CurrentUser, lead) -> bool:
    return user.can("leads.edit.any") or (user.can("leads.edit.own") and lead["owner_user_id"] == user.id)


def _can_stage_lead(user: auth.CurrentUser, lead) -> bool:
    return user.can("leads.stage.any") or (user.can("leads.stage.own") and lead["owner_user_id"] == user.id)


def _can_signal_lead(user: auth.CurrentUser, lead) -> bool:
    return user.can("leads.signal.any") or (user.can("leads.signal.own") and lead["owner_user_id"] == user.id)


def _can_see_contact(user: auth.CurrentUser, lead) -> bool:
    """SPEC.md rule 2: marketing gets counts/behaviour, never phone/email,
    unless they own the lead. Redaction happens here, server-side — never
    left to the client to hide a field it was handed anyway."""
    return user.can("consent.any") or (user.can("consent.own") and lead["owner_user_id"] == user.id)


def _owner_name(c, owner_user_id) -> Optional[str]:
    if not owner_user_id:
        return None
    row = c.execute("SELECT display_name FROM app_user WHERE id=?", (owner_user_id,)).fetchone()
    return row["display_name"] if row else None


def _owner_names(c, owner_user_ids) -> dict:
    """_owner_name for a whole page at once — one query, not one per row."""
    ids = [i for i in dict.fromkeys(owner_user_ids) if i]
    if not ids:
        return {}
    return {r["id"]: r["display_name"] for r in c.execute(
        "SELECT id, display_name FROM app_user WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}


def _region_list(c, lead_id: int) -> list:
    return [r["region"] for r in c.execute(
        "SELECT region FROM lead_regions WHERE lead_id=? ORDER BY set_at", (lead_id,))]


def activity_note(c, lead_id: int) -> str:
    """D10 — Balraj kept the free-text column but redefined it: not a status,
    a plain-English latest-activity line, written by the system from the event
    log and never typed by a human. That is what makes it safe."""
    r = c.execute(
        """SELECT kind, detail, occurred_at FROM events
            WHERE lead_id=? AND voided_at IS NULL
              AND kind NOT IN ('imported','merged','stage_change','sent')
            ORDER BY occurred_at DESC, id DESC LIMIT 1""", (lead_id,)).fetchone()
    if not r:
        return _NO_ACTIVITY
    return _ACTIVITY_WORDS.get(r["kind"], r["kind"])


_NO_ACTIVITY = "Imported, no activity yet"
_ACTIVITY_WORDS = {
    "open": "Opened an email", "click": "Clicked a link",
    "page_view": "Viewed a page", "booking_page_view": "Viewed the booking page",
    "reply": "Replied", "booking_completed": "Booked a consultation",
    "wants_meeting": "Wants to meet in person", "corporate_deal": "Corporate deal",
    "partnership": "Partnership offer", "high_budget": "High budget",
    "bounce": "Email bounced", "unsubscribe": "Unsubscribed",
}


def _lead_extras(c, lead_ids, identities_for=()) -> dict:
    """channels / regions / activity note / identities for a page of leads in
    four queries total. The list screens used to ask for each of these once
    PER ROW — /api/leads ran 95 statements for 20 leads, and on the production host every
    statement is a ~0.33 s round trip. Results match the single-lead helpers
    (ingest.channels, _region_list, activity_note) exactly."""
    ids = list(dict.fromkeys(lead_ids))
    out = {i: {"channels": [], "region": [], "activity_note": _NO_ACTIVITY, "identities": []}
           for i in ids}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    for r in c.execute(
            "SELECT lead_id, channel, note, seen_at FROM lead_channels "
            "WHERE lead_id IN (%s) ORDER BY lead_id, seen_at" % ph, ids):
        out[r["lead_id"]]["channels"].append(
            {"channel": r["channel"], "note": r["note"], "seen_at": r["seen_at"]})
    for r in c.execute(
            "SELECT lead_id, region FROM lead_regions "
            "WHERE lead_id IN (%s) ORDER BY lead_id, set_at" % ph, ids):
        out[r["lead_id"]]["region"].append(r["region"])
    for r in c.execute(
            """SELECT lead_id, kind FROM (
                   SELECT lead_id, kind, ROW_NUMBER() OVER (
                          PARTITION BY lead_id ORDER BY occurred_at DESC, id DESC) AS rn
                     FROM events
                    WHERE lead_id IN (%s) AND voided_at IS NULL
                      AND kind NOT IN ('imported','merged','stage_change','sent')
               ) AS latest WHERE rn = 1""" % ph, ids):
        out[r["lead_id"]]["activity_note"] = _ACTIVITY_WORDS.get(r["kind"], r["kind"])
    wanted = [i for i in dict.fromkeys(identities_for) if i in out]
    if wanted:
        for r in c.execute(
                "SELECT lead_id, kind, value, status FROM lead_identities "
                "WHERE lead_id IN (%s)" % ",".join("?" * len(wanted)), wanted):
            out[r["lead_id"]]["identities"].append(
                {"kind": r["kind"], "value": r["value"], "status": r["status"]})
    return out


def _lead_tasks_for(c, user: auth.CurrentUser, lead_id: int) -> list:
    """AUDIT.md critical leak #1 (api.py:269 originally): lead detail used to
    return EVERY task on the lead, regardless of who owned it — a sales
    caller received another rep's task, and marketing could read tasks
    embedded in a lead to bypass the /api/team 403 entirely.

    Filtered in SQL, not by trimming a Python list after the fact, so a
    caller without `tasks.all` never fetches another owner's task row into
    memory in the first place — the same principle SPEC.md rule 1 already
    requires for lead lists."""
    # N3 (FABLE-AUDIT-R2.md) — l.score_updated_at rides along so
    # tasks.canonical()/_canonical_task() can self-heal `lead_score` the
    # same way every other read path does, instead of trusting the
    # materialized column on a Task the way LeadDetail no longer does.
    if user.can("tasks.all"):
        rows = c.execute(
            """SELECT t.*, l.name, l.company, l.score AS lead_score,
                     l.score_updated_at AS lead_score_updated_at FROM tasks t
                 JOIN leads l ON l.id = t.lead_id
                WHERE t.lead_id=? ORDER BY t.due_at""", (lead_id,)).fetchall()
    else:
        rows = c.execute(
            """SELECT t.*, l.name, l.company, l.score AS lead_score,
                     l.score_updated_at AS lead_score_updated_at FROM tasks t
                 JOIN leads l ON l.id = t.lead_id
                 JOIN app_user au ON au.staff_id = t.owner_id
                WHERE t.lead_id=? AND au.id=?
                ORDER BY t.due_at""", (lead_id, user.id)).fetchall()
    return [tasks.canonical(c, r) for r in rows]


def _lead_detail(c, user: auth.CurrentUser, r) -> dict:
    """The canonical, FLAT LeadDetail (SPEC.md/AUDIT.md). Replaces the old
    {lead, identities, regions, score} nesting — every field the app reads
    (stage, region, score_breakdown, ...) lives at the top level."""
    lead_id = r["id"]
    may_see_contact = _can_see_contact(user, r)
    consent = c.execute("SELECT * FROM lead_consent WHERE lead_id=?", (lead_id,)).fetchone()
    if consent:
        consent_out = dict(consent) if may_see_contact else {"basis": consent["basis"]}
    else:
        consent_out = None
    ex = scoring.explain(c, lead_id)

    return {
        "id": r["id"],
        "name": r["name"],
        "name_kana": r["name_kana"],
        "company": r["company"],
        "title": r["title"],
        # SPEC-V2 §6 — path only; no OCR provider is wired, so this is never
        # more than "a photo exists", never a claim about what it contains.
        # C1 (FABLE-AUDIT.md) — a business-card photo is name+company+phone+
        # email+address in one file: the single densest PII object in the
        # system. It must be redacted exactly when the rest of the contact
        # block is (SPEC.md rule 2) — handing marketing the path defeated
        # the redaction one field up, since GET /api/media has no way to
        # know the path was supposed to be hidden.
        "card_image_path": r["card_image_path"] if may_see_contact else None,

        # D10 — stage and exit_state are separate columns, separate fields.
        "stage": r["stage"],
        "exit_state": r["exit_state"],
        # H4 (FABLE-AUDIT.md) — the four exit states are still unratified by
        # Balraj (decision log: "needs a yes or a no"); this stays true
        # regardless of whether auto-firing is currently switched on
        # (tracking.AUTO_EXIT_STATES_ENABLED), so the UI can keep badging it
        # `provisional` exactly as SPEC-V2 already does for nurture exits.
        "exit_state_provisional": r["exit_state"] is not None,
        "stage_reason": r["stage_reason"],
        "revisit_at": r["revisit_at"],

        # D11 — source is two things: first_touch never changes ("source"
        # here, matching SPEC's field name); channels[] grows over time.
        "source": r["first_touch"],
        "first_touch_at": r["first_touch_at"],
        "first_touch_note": r["first_touch_note"],
        "channels": ingest.channels(c, lead_id),

        "purpose": r["purpose"],
        "relationship": r["relationship"],

        "owner_user_id": r["owner_user_id"],
        "owner_name": _owner_name(c, r["owner_user_id"]),

        "region": _region_list(c, lead_id),
        "regions": [dict(x) for x in c.execute(
            "SELECT * FROM lead_regions WHERE lead_id=?", (lead_id,))],

        "score": ex["score"],
        "heat": scoring.heat(ex["score"]),
        "threshold": ex["threshold"],
        "hot": ex["hot"],
        "score_breakdown": ex["breakdown"],
        "decay_note": ex["decay_note"],

        "activity_note": activity_note(c, lead_id),

        "contact_redacted": not may_see_contact,
        "identities": [dict(x) for x in c.execute(
            "SELECT kind,value,status FROM lead_identities WHERE lead_id=?",
            (lead_id,))] if may_see_contact else [],
        "consent": consent_out,

        # AUDIT.md critical leak #1 fix — see _lead_tasks_for().
        "tasks": _lead_tasks_for(c, user, lead_id),

        "stage_history": [dict(x) for x in c.execute(
            "SELECT * FROM lead_stage_history WHERE lead_id=? ORDER BY at", (lead_id,))],
        "timeline": [dict(x) for x in c.execute(
            """SELECT kind, detail, source, occurred_at, voided_at
                 FROM events WHERE lead_id=? ORDER BY occurred_at""", (lead_id,))],

        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _upsert_identity(c, lead_id: int, kind: str, raw_value) -> None:
    """PATCH /api/leads/{id} email/phone edits: dedupe/audit semantics, never
    a silent overwrite (AUDIT.md high finding). Normalises the same way
    ingest.py does, so an edited identity still matches D13's dedupe rules,
    and refuses to steal an identity that already belongs to a different
    lead — that is a merge decision, not an edit."""
    norm = ingest.norm_email(raw_value) if kind == "email" else ingest.norm_phone(raw_value)
    if not norm:
        raise err(422, "invalid_identity", "'%s' is not a valid %s." % (raw_value, kind))
    existing = c.execute("SELECT lead_id FROM lead_identities WHERE kind=? AND value=?",
                         (kind, norm)).fetchone()
    if existing and existing["lead_id"] != lead_id:
        raise err(409, "identity_conflict",
                 "This %s already belongs to a different lead (#%s). That is a dedupe/merge "
                 "decision, not a silent edit." % (kind, existing["lead_id"]))
    if existing:
        return
    c.execute("INSERT INTO lead_identities (lead_id, kind, value) VALUES (?,?,?)",
             (lead_id, kind, norm))


# ══ leads ═══════════════════════════════════════════════════════════════════

@app.get("/api/leads")
def list_leads(user: auth.CurrentUser = Depends(auth.current_user),
               q: str = "", stage: str = "", region: str = "", purpose: str = "",
               owner: Optional[str] = None, page: int = 1, page_size: int = 20,
               sort: str = "id"):
    c = con()
    try:
        base = "SELECT DISTINCT l.* FROM leads l"
        joins, where, args = [], ["l.merged_into IS NULL"], []
        if region:
            joins.append("JOIN lead_regions r ON r.lead_id = l.id")
            where.append("r.region = ?")
            args.append(region)
        if q:
            # AUDIT.md high finding: search claimed "name, email, company"
            # but only matched name/company in SQL. Now matches identities
            # (email/phone) too, in the same query — never fetched into
            # Python first, so the existing role/redaction filtering below
            # still governs which ROWS can match at all.
            where.append("(l.name LIKE ? OR l.company LIKE ? OR EXISTS ("
                         "SELECT 1 FROM lead_identities li "
                         "WHERE li.lead_id = l.id AND LOWER(li.value) LIKE LOWER(?)))")
            args += ["%%%s%%" % q, "%%%s%%" % q, "%%%s%%" % q]
        if stage:
            where.append("l.stage = ?")
            args.append(stage)
        if purpose:
            where.append("l.purpose = ?")
            args.append(purpose)

        # RBAC rule 1 (SPEC.md): filtered in SQL, never in Python after the
        # fetch. A sales caller's `owner` query param, if any, is ignored —
        # they only ever get their own rows regardless of what they ask for.
        if not user.can("leads.all"):
            where.append("l.owner_user_id = ?")
            args.append(user.id)
        elif owner:
            where.append("l.owner_user_id = ?")
            args.append(owner)

        from_sql = base + ((" " + " ".join(joins)) if joins else "")
        where_sql = " AND ".join(where)
        # the alias is not optional: Postgres rejects an unaliased derived table
        total = db.scalar(
            c, "SELECT count(*) FROM (%s WHERE %s) AS _c" % (from_sql, where_sql), args)

        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size
        # H1/M4 (FABLE-AUDIT.md) — "hot leads first" needed the materialized
        # score column to be a real SQL ORDER BY instead of "sort in Python
        # after paging" (impossible — the page was already cut). `l.id` stays
        # the default so existing callers see no change.
        order_sql = "l.score DESC, l.id" if sort == "score" else "l.id"
        rows = c.execute(
            "%s WHERE %s ORDER BY %s LIMIT ? OFFSET ?" % (from_sql, where_sql, order_sql),
            args + [page_size, offset]).fetchall()

        extras = _lead_extras(c, [r["id"] for r in rows],
                              identities_for=[r["id"] for r in rows if _can_see_contact(user, r)])
        owners = _owner_names(c, [r["owner_user_id"] for r in rows])
        data = []
        for r in rows:
            # H1 — read the materialized column, never scoring.score() per
            # row (was one extra ~3-query round trip per lead on this list).
            # N3 (FABLE-AUDIT-R2.md) — but the column drifts stale between
            # decay sweeps, so fresh_score() self-heals it on read, bounded
            # to this page (commit batched once below), never a full scan.
            s = scoring.fresh_score(c, r["id"], r["score_updated_at"], r["score"], commit=False)
            x = extras[r["id"]]
            item = {
                "id": r["id"], "name": r["name"], "company": r["company"],
                "stage": r["stage"], "exit_state": r["exit_state"],
                "exit_state_provisional": r["exit_state"] is not None,   # H4
                "source": r["first_touch"],
                "channels": [ch["channel"] for ch in x["channels"]],
                "region": x["region"],
                "purpose": r["purpose"], "relationship": r["relationship"],
                "score": s, "heat": scoring.heat(s),
                "activity_note": x["activity_note"],
                "owner_user_id": r["owner_user_id"],
                "owner_name": owners.get(r["owner_user_id"]),
                "contact_redacted": not _can_see_contact(user, r),
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            }
            if _can_see_contact(user, r):
                item["identities"] = x["identities"]
            data.append(item)
        c.commit()   # N3 — one commit for whatever fresh_score() recomputed above, not per-row
        return {"data": data, "page": page, "page_size": page_size, "total": total}
    finally:
        c.close()


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: int, user: auth.CurrentUser = Depends(auth.current_user)):
    c = con()
    try:
        r = _lead_row(c, lead_id)
        if not r:
            raise err(404, "not_found", "No such lead.")
        if not _can_view_lead(user, r):
            raise err(403, "forbidden", "You do not have access to this lead.")
        return _lead_detail(c, user, r)
    finally:
        c.close()


@app.get("/api/leads/{lead_id}/score")
def why_score(lead_id: int, user: auth.CurrentUser = Depends(auth.current_user)):
    c = con()
    try:
        r = _lead_row(c, lead_id)
        if not r:
            raise err(404, "not_found", "No such lead.")
        if not _can_view_lead(user, r):
            raise err(403, "forbidden", "You do not have access to this lead.")
        return scoring.explain(c, lead_id)
    finally:
        c.close()


@app.post("/api/leads")
async def create_lead(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    # matrix row "import CSV / create campaigns" — admin + marketing only.
    # a lone POST is the same door a CSV importer or LP form integration
    # walks through row by row.
    auth.require(user, "import.csv")
    body = await req.json()

    name = body.get("name")
    if not name:
        raise err(422, "missing_field", "name is required.")
    # AUDIT.md high finding: the app sends `source` (SPEC's field name); the
    # backend only accepted `channel` and 500'd with a raw KeyError when it
    # was missing. Accept both spellings, and fail with a structured 422 —
    # never a 500 — when neither is present or valid.
    source = body.get("source") or body.get("channel")
    if not source:
        raise err(422, "missing_field", "source is required.")
    if source not in ingest.CHANNELS:
        raise err(422, "invalid_source", "source must be one of %s" % sorted(ingest.CHANNELS))

    purpose = body.get("purpose")
    if purpose is not None and purpose not in PURPOSES:
        raise err(422, "invalid_purpose", "purpose must be one of %s" % sorted(PURPOSES))

    regions = body.get("region") or []
    if isinstance(regions, str):
        regions = [regions]
    for rg in regions:
        if rg not in REGIONS:
            raise err(422, "invalid_region", "region must be one of %s" % sorted(REGIONS))

    c = con()
    try:
        res = ingest.upsert_lead(
            c, name=name, channel=source,
            company=body.get("company"), title=body.get("title"),
            email=body.get("email"), phone=body.get("phone"),
            note=body.get("note"))
        lead_id = res["lead_id"]
        # AUDIT.md high finding: region/purpose were silently ignored on
        # create. Now applied — purpose directly, region as one row per
        # value (a lead can be multi-region, D9).
        if purpose:
            c.execute("UPDATE leads SET purpose=? WHERE id=?", (purpose, lead_id))
        for rg in regions:
            c.execute(
                "INSERT OR IGNORE INTO lead_regions (lead_id, region, inferred_from) VALUES (?,?,?)",
                (lead_id, rg, "api"))
        c.commit()
        auth.audit(c, user, "create", "lead", lead_id, after=res)
        # AUDIT.md high finding: the response used to be the upsert marker
        # ({lead_id, created, merged, ...}), not a usable LeadDetail.
        return _lead_detail(c, user, _lead_row(c, lead_id))
    finally:
        c.close()


@app.patch("/api/leads/{lead_id}")
async def edit_lead(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await req.json()
    c = con()
    try:
        r = _lead_row(c, lead_id)
        if not r:
            raise err(404, "not_found", "No such lead.")
        if not _can_edit_lead(user, r):
            raise err(403, "forbidden", "You may not edit this lead.")

        # AUDIT.md high finding: email/phone used to be silently dropped —
        # any field outside EDITABLE_LEAD_FIELDS just vanished with no error.
        # Now: recognised identity edits are applied (with dedupe/conflict
        # rules below); anything else unrecognised is a real 422, never a
        # silent no-op.
        unknown = set(body) - EDITABLE_LEAD_FIELDS - IDENTITY_FIELDS
        if unknown:
            raise err(422, "not_editable",
                     "These fields cannot be edited here: %s. Editable: %s (plus email/phone, "
                     "handled as identities)." % (sorted(unknown), sorted(EDITABLE_LEAD_FIELDS)))

        fields = {k: v for k, v in body.items() if k in EDITABLE_LEAD_FIELDS}
        identity_edits = {k: v for k, v in body.items() if k in IDENTITY_FIELDS and v}
        if not fields and not identity_edits:
            raise err(422, "no_editable_fields",
                     "None of the supplied fields are editable. Allowed: %s (plus email/phone)."
                     % sorted(EDITABLE_LEAD_FIELDS))

        before = dict(r)
        if fields:
            set_sql = ", ".join("%s=?" % k for k in fields)
            c.execute("UPDATE leads SET %s, updated_at=datetime('now') WHERE id=?" % set_sql,
                      list(fields.values()) + [lead_id])
        for kind, value in identity_edits.items():
            _upsert_identity(c, lead_id, kind, value)
        c.commit()
        after_row = _lead_row(c, lead_id)
        auth.audit(c, user, "update", "lead", lead_id, before=before,
                  after={**dict(after_row), **identity_edits})
        return _lead_detail(c, user, after_row)
    finally:
        c.close()


@app.post("/api/leads/{lead_id}/assign")
async def assign_lead(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "assign")
    body = await req.json()
    new_owner = body.get("owner_user_id")
    if not new_owner:
        raise err(422, "missing_field", "owner_user_id is required.")
    c = con()
    try:
        r = _lead_row(c, lead_id)
        if not r:
            raise err(404, "not_found", "No such lead.")
        owner_row = c.execute("SELECT id, display_name FROM app_user WHERE id=?", (new_owner,)).fetchone()
        if not owner_row:
            raise err(404, "no_such_user", "owner_user_id does not match a user.")
        staff_id = auth.ensure_staff_bridge(c, new_owner, owner_row["display_name"])
        before = dict(r)
        c.execute("""UPDATE leads SET owner_user_id=?, owner_id=?, updated_at=datetime('now')
                     WHERE id=?""", (new_owner, staff_id, lead_id))
        c.commit()
        after_row = _lead_row(c, lead_id)
        auth.audit(c, user, "assign", "lead", lead_id, before=before, after=dict(after_row))
        # AUDIT.md critical finding: used to return the raw lead row/receipt;
        # lead detail stored that as its whole view model and crashed on the
        # fields it was missing. Now: the same canonical LeadDetail
        # GET /api/leads/{id} returns.
        return _lead_detail(c, user, after_row)
    finally:
        c.close()


@app.post("/api/leads/{lead_id}/stage")
async def move_stage(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await req.json()
    new_value = body.get("stage")
    if new_value not in FORWARD_STAGES and new_value not in EXIT_STATES:
        raise err(422, "invalid_stage",
                 "stage must be one of %s (forward) or %s (exit)." %
                 (list(FORWARD_STAGES), list(EXIT_STATES)))
    c = con()
    try:
        r = _lead_row(c, lead_id)
        if not r:
            raise err(404, "not_found", "No such lead.")
        if not _can_stage_lead(user, r):
            raise err(403, "forbidden", "You may not move this lead's stage.")
        old_stage, old_exit = r["stage"], r["exit_state"]

        if new_value in FORWARD_STAGES:
            # Moving forward again reopens a lead that had exited — SPEC.md's
            # data model has no route for a lead to be simultaneously
            # "in_negotiation" and "unreachable"; a fresh forward move is the
            # signal that the exit no longer applies.
            c.execute("""UPDATE leads SET stage=?, exit_state=NULL, stage_at=datetime('now')
                        WHERE id=?""", (new_value, lead_id))
            history_to = new_value
        else:
            # Exit states are their own column (SPEC.md D10 migration) — the
            # forward `stage` position is left exactly where it was; this
            # endpoint is still "move a lead's pipeline stage" in the sense
            # the matrix and the UI mean it (any terminal move a rep makes),
            # even though only one of the two columns actually changes.
            c.execute("UPDATE leads SET exit_state=?, stage_at=datetime('now') WHERE id=?",
                      (new_value, lead_id))
            history_to = "exit:%s" % new_value
        c.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, actor_user_id)
                     VALUES (?,?,?,?)""", (lead_id, old_stage, history_to, user.id))
        c.commit()
        staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
        scoring.record(c, lead_id, "stage_change", detail="%s -> %s" % (old_stage, history_to),
                       source="human", set_by=staff_id)
        auth.audit(c, user, "stage", "lead", lead_id,
                  before={"stage": old_stage, "exit_state": old_exit},
                  after={"stage": new_value if new_value in FORWARD_STAGES else old_stage,
                         "exit_state": new_value if new_value in EXIT_STATES else None})
        after_row = _lead_row(c, lead_id)
        return _lead_detail(c, user, after_row)
    finally:
        c.close()


@app.post("/api/leads/{lead_id}/signal")
async def signal(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    """D4: any request to meet in person, +30, hybrid detection — AI on email
    replies, or a human marking it here for the channels the system cannot
    read (WhatsApp/LINE/in person). set_by makes the lever attributable."""
    body = await req.json()
    kind = body.get("type")
    if kind != "wants_meeting":
        raise err(422, "unsupported_signal", "Only type='wants_meeting' is supported.")
    c = con()
    try:
        r = _lead_row(c, lead_id)
        if not r:
            raise err(404, "not_found", "No such lead.")
        if not _can_signal_lead(user, r):
            raise err(403, "forbidden", "You may not grant this signal on this lead.")
        staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
        before = scoring.score(c, lead_id)
        scoring.record(c, lead_id, "wants_meeting", detail=body.get("note"),
                       source="human", set_by=staff_id)
        after = scoring.score(c, lead_id)
        auth.audit(c, user, "signal", "lead", lead_id,
                  before={"score": before}, after={"score": after, "note": body.get("note")})
        after_row = _lead_row(c, lead_id)
        detail = _lead_detail(c, user, after_row)
        # additive — on top of the canonical LeadDetail, not instead of it,
        # so the moment of crossing is still visible to the caller that just
        # granted it.
        detail["signal_score_before"] = before
        detail["signal_score_after"] = after
        detail["signal_crossed_threshold"] = scoring.crossed_threshold(c, lead_id, before, after)
        return detail
    finally:
        c.close()


# ══ pipeline ═════════════════════════════════════════════════════════════════

@app.get("/api/pipeline")
def pipeline(user: auth.CurrentUser = Depends(auth.current_user),
            page: int = 1, page_size: int = 50):
    """Kanban buckets. Not wrapped in the {data,page,page_size,total} list
    envelope — it is six lists at once, not one — but each column's own SQL
    is still role-filtered exactly like /api/leads.

    H2 (FABLE-AUDIT.md) — used to hard-cap every column at 200 cards with no
    way to see past it: with ~30k leads mostly sitting in new/nurturing,
    everything past card #200 in those columns was unreachable from the app,
    full stop. `count` was always honest, the board just wasn't the data.
    Now every column is its own page — same `page`/`page_size` query params
    across all six buckets (a UI can still page one column at a time by
    re-requesting with a different page for just the column it is scrolling),
    with an accurate `has_more` per bucket so nothing is silently missing."""
    c = con()
    try:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size

        where, args = ["merged_into IS NULL"], []
        if not user.can("leads.all"):
            where.append("owner_user_id = ?")
            args.append(user.id)
        where_sql = " AND ".join(where)
        # one grouped count and one owner-name lookup for the whole board,
        # instead of a count and a name query per column / per card
        counts = {r["stage"]: int(r["n"]) for r in c.execute(
            "SELECT stage, count(*) AS n FROM leads WHERE %s GROUP BY stage" % where_sql, args)}
        stage_rows = {stage: c.execute(
            "SELECT * FROM leads WHERE %s AND stage=? ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            % where_sql, args + [stage, page_size, offset]).fetchall() for stage in FORWARD_STAGES}
        owners = _owner_names(c, [r["owner_user_id"] for rs in stage_rows.values() for r in rs])
        buckets = []
        for stage in FORWARD_STAGES:
            rows = stage_rows[stage]
            count = counts.get(stage, 0)
            label_en, label_ja = STAGE_LABELS[stage]
            leads_out = []
            for r in rows:
                # H1 — materialized, no per-card scoring.score() call; N3 —
                # self-heal if stale, bounded to this column's page.
                sc = scoring.fresh_score(c, r["id"], r["score_updated_at"], r["score"], commit=False)
                leads_out.append({
                    "id": r["id"], "name": r["name"], "company": r["company"],
                    "score": sc, "heat": scoring.heat(sc),
                    "stage": r["stage"], "exit_state": r["exit_state"],
                    "exit_state_provisional": r["exit_state"] is not None,   # H4
                    "owner_user_id": r["owner_user_id"],
                    "owner_name": owners.get(r["owner_user_id"]),
                })
            buckets.append({
                "key": stage, "label": label_en, "label_ja": label_ja,
                "count": count, "leads": leads_out,
                "page": page, "page_size": page_size,
                "has_more": offset + len(rows) < count,
            })
        c.commit()   # N3 — one commit for whatever fresh_score() recomputed above, not per-row
        # AUDIT.md critical finding: used to be {"stages": [...]} with no
        # labels — PipelineScreen.tsx calls data.buckets.reduce and crashes.
        return {"buckets": buckets, "page": page, "page_size": page_size}
    finally:
        c.close()


# ══ dashboard ═══════════════════════════════════════════════════════════════

DASHBOARD_TILE_LABELS = {
    "total_leads":      ("Total leads", "総リード数"),
    "first_sends":      ("First sends", "初回送信数"),
    "meetings_booked":  ("Meetings booked", "商談予約数"),
    "in_negotiation":   ("In negotiation", "商談中"),
    "won":              ("Won", "成約数"),
    "expected_revenue": ("Expected revenue", "見込み売上"),
}

FUNNEL_LABELS = {
    "sent":            ("Sent", "送信"),
    "opened":          ("Opened", "開封"),
    "clicked":         ("Clicked", "クリック"),
    "replied":         ("Replied", "返信"),
    "meeting_booked":  ("Meeting booked", "商談予約済み"),
    "in_negotiation":  ("In negotiation", "商談中"),
    "won":             ("Won", "成約"),
}


def _trend(c, days: int = 14) -> list:
    """Real per-day counts from lead_stage_history/leads — never the
    invented modular-arithmetic trend the demo's mocks used. 14 days,
    oldest first.

    Three grouped queries, not three per day. This used to issue `days * 3`
    scalar counts — 42 of the dashboard's 57 statements. On SQLite that is
    invisible; against a managed Postgres each statement is a network round
    trip, and the endpoint took 14 s from a client one region away. Grouping
    makes the cost independent of the window length.
    """
    today = datetime.utcnow().date()
    first = (today - timedelta(days=days - 1)).isoformat()
    buckets = {(today - timedelta(days=i)).isoformat(): {"leads": 0, "meetings_booked": 0,
                                                         "in_negotiation": 0}
               for i in range(days)}

    def fill(key, sql, args):
        for row in c.execute(sql, args):
            day = str(row["d"])[:10]
            if day in buckets:
                buckets[day][key] = row["n"]

    fill("leads",
         "SELECT date(created_at) AS d, count(*) AS n FROM leads"
         " WHERE date(created_at) >= ? GROUP BY date(created_at)", (first,))
    fill("meetings_booked",
         "SELECT date(at) AS d, count(*) AS n FROM lead_stage_history"
         " WHERE to_stage='meeting_booked' AND date(at) >= ? GROUP BY date(at)", (first,))
    fill("in_negotiation",
         "SELECT date(at) AS d, count(*) AS n FROM lead_stage_history"
         " WHERE to_stage='in_negotiation' AND date(at) >= ? GROUP BY date(at)", (first,))

    return [dict(date=day, **buckets[day]) for day in sorted(buckets)]


def _trend_flag(meetings_booked: int, in_negotiation: int,
                source_total_matches_tile: bool, by_source: list) -> Optional[dict]:
    """D16 — the dashboard explains its own anomaly instead of assuming a
    manager spots it. Single flag (SPEC's TrendFlag is one object, not a
    list) — the gap between booked-and-never-negotiated is reported first
    since it is actionable; a source-attribution mismatch is reported if
    that is the only anomaly."""
    gap = meetings_booked - in_negotiation
    if gap > 0 and gap >= max(3, meetings_booked * 0.3):
        return {
            "message": "%d booked meetings never became a conversation" % gap,
            "message_ja": "%d件の商談予約が商談に進んでいません" % gap,
            "detail": ("%d consultations were booked and %d progressed to negotiation. The gap is "
                      "meetings that happened, or did not happen, and went nowhere — no-shows, "
                      "wrong fit, or nobody followed up." % (meetings_booked, in_negotiation)),
            "detail_ja": "%d件が商談予約済み、%d件が商談中に進みました。" % (meetings_booked, in_negotiation),
        }
    if not source_total_matches_tile:
        total = sum(r["count"] for r in by_source)
        return {
            "message": "Source attribution does not total the meetings-booked tile",
            "message_ja": "流入元別の合計が商談予約数タイルと一致していません",
            "detail": ("by_source totals %d, the meetings_booked tile reads %d — a lead is being "
                      "counted under more than one first-touch, or not at all." % (total, meetings_booked)),
            "detail_ja": "",
        }
    return None


def _hot_leads(c, user: auth.CurrentUser, limit: int = 10) -> list:
    """H1 (FABLE-AUDIT.md) — used to SELECT every live lead and call
    scoring.score() (≈3 queries) on each one in a Python loop just to find
    the top 10; at 30k leads that is the single most expensive thing
    /api/dashboard did. Now one indexed SQL query against the materialized
    leads.score column (idx_leads_score) does the filtering AND the
    ordering — the database's job, not Python's."""
    thr = scoring.threshold(c)
    rows = c.execute(
        """SELECT * FROM leads WHERE merged_into IS NULL AND score >= ?
            ORDER BY score DESC LIMIT ?""", (thr, limit)).fetchall()
    owners = _owner_names(c, [r["owner_user_id"] for r in rows])
    out = []
    for r in rows:
        # N3 (FABLE-AUDIT-R2.md) — self-heal the displayed number so it can
        # never disagree with LeadDetail's live explain(). The SQL filter
        # above still used the (possibly stale) materialized column to pick
        # candidates cheaply; a stale row that has actually decayed below
        # threshold by now is dropped here rather than shown as hot with a
        # wrong number. (A truly-hot lead whose stale column reads low
        # enough to miss the SQL filter entirely is the one gap self-heal
        # cannot close without a full scan — that is what sweep_decay's
        # schedule is for; see scripts/sweep_decay.py.)
        sc = scoring.fresh_score(c, r["id"], r["score_updated_at"], r["score"], commit=False)
        if sc < thr:
            continue
        out.append({
            "id": r["id"], "name": r["name"], "company": r["company"],
            "score": sc, "heat": scoring.heat(sc),
            "stage": r["stage"], "exit_state": r["exit_state"],
            "exit_state_provisional": r["exit_state"] is not None,   # H4
            "owner_user_id": r["owner_user_id"], "owner_name": owners.get(r["owner_user_id"]),
            # SPEC.md rule 2 — a "hot leads" widget on a company-wide
            # dashboard still must not hand marketing a phone/email they
            # don't own.
            "contact_redacted": not _can_see_contact(user, r),
        })
    c.commit()   # N3 — one commit for whatever fresh_score() recomputed above, not per-row
    return out


@app.get("/api/dashboard")
def dashboard(user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "dashboard.company")
    c = con()
    try:
        def n(sql, args=()):
            return db.scalar(c, sql, args)

        live = "merged_into IS NULL"
        # One pass over leads for all four stage counts instead of four separate
        # statements: each statement is a ~0.33 s round trip from the production host.
        t = c.execute(
            """SELECT count(*) AS total,
                      sum(CASE WHEN stage IN ('meeting_booked','in_negotiation','won')
                               THEN 1 ELSE 0 END) AS booked,
                      sum(CASE WHEN stage IN ('in_negotiation','won') THEN 1 ELSE 0 END) AS negotiating,
                      sum(CASE WHEN stage = 'won' THEN 1 ELSE 0 END) AS won
                 FROM leads WHERE %s""" % live).fetchone()
        total_leads = int(t["total"] or 0)
        meetings_booked = int(t["booked"] or 0)
        in_negotiation = int(t["negotiating"] or 0)
        won = int(t["won"] or 0)
        first_sends = n("SELECT count(DISTINCT lead_id) FROM sends")

        # Reasons are bilingual like every other label this API returns: the
        # Japanese office reads this screen, and an English-only sentence in
        # the middle of a Japanese dashboard is a bug, not a detail.
        tile_values = {
            "total_leads": (total_leads, None, None),
            "first_sends": (first_sends, None, None),
            "meetings_booked": (meetings_booked, None, None),
            "in_negotiation": (in_negotiation, None, None),
            "won": (won, None, None),
            # D15 — expected revenue needs a stage-probability table Balraj
            # has not supplied. Reports null with a reason rather than
            # inventing a number that looks authoritative.
            "expected_revenue": (None,
                                 "stage close-probability percentages (see D15) — not yet supplied",
                                 "ステージ別の成約率（D15）が未設定のため算出できません"),
        }
        tiles = [{"key": k, "label": DASHBOARD_TILE_LABELS[k][0], "label_ja": DASHBOARD_TILE_LABELS[k][1],
                 "value": v, "unavailable_reason": reason, "unavailable_reason_ja": reason_ja}
                for k, (v, reason, reason_ja) in tile_values.items()]

        # D16 — attribute each booking to ONE source (first touch) so the
        # chart totals the tile. The demo's chart summed to 55 against a
        # tile of 30. Count rows are never bare {"n": ...} (AUDIT.md).
        by_source = [{
            "source": row["source"],
            "label": SOURCE_LABELS.get(row["source"], (row["source"], row["source"]))[0],
            "label_ja": SOURCE_LABELS.get(row["source"], (row["source"], row["source"]))[1],
            "count": row["n"],
        } for row in c.execute(
            """SELECT first_touch AS source, count(*) AS n FROM leads
                WHERE merged_into IS NULL AND stage IN ('meeting_booked','in_negotiation','won')
                GROUP BY first_touch ORDER BY n DESC""")]

        by_region = [{
            "region": row["region"],
            "label": REGION_LABELS.get(row["region"], (row["region"], row["region"]))[0],
            "label_ja": REGION_LABELS.get(row["region"], (row["region"], row["region"]))[1],
            "count": row["n"],
        } for row in c.execute(
            """SELECT r.region, count(DISTINCT l.id) AS n
                 FROM lead_regions r JOIN leads l ON l.id=r.lead_id
                WHERE l.merged_into IS NULL AND l.stage IN ('meeting_booked','in_negotiation','won')
                GROUP BY r.region""")]

        # open/click/reply in one pass over events, same numbers as the three
        # separate DISTINCT counts this replaced
        beh = c.execute(
            """SELECT count(DISTINCT CASE WHEN kind='open'  THEN lead_id END) AS opened,
                      count(DISTINCT CASE WHEN kind='click' THEN lead_id END) AS clicked,
                      count(DISTINCT CASE WHEN kind='reply' THEN lead_id END) AS replied
                 FROM events WHERE voided_at IS NULL""").fetchone()
        funnel_counts = {
            "sent": n("SELECT count(*) FROM sends"),
            "opened": int(beh["opened"] or 0),
            "clicked": int(beh["clicked"] or 0),
            "replied": int(beh["replied"] or 0),
            "meeting_booked": meetings_booked,
            "in_negotiation": in_negotiation,
            "won": won,
        }
        funnel = [{"stage": k, "label": FUNNEL_LABELS[k][0], "label_ja": FUNNEL_LABELS[k][1], "count": v}
                 for k, v in funnel_counts.items()]

        source_total_matches_tile = sum(r["count"] for r in by_source) == meetings_booked

        by_rep = None
        if user.can("dashboard.per_rep"):
            by_rep = [{
                "user_id": t["user_id"], "name": t["display_name"], "office": t["office"],
                "leads_owned": t["leads_owned"], "tasks_open": t["tasks_open"],
                "tasks_overdue": t["tasks_overdue"], "meetings_booked": t["meetings_booked"],
                "won": t["won"],
            } for t in tasks.team_status(c) if t["user_id"]]

        # AUDIT.md critical finding: used to be {kpis, flags, ...} with count
        # rows shaped as bare {"n": ...} — DashboardScreen.tsx calls
        # data.tiles.map and crashes.
        return {
            "tiles": tiles,
            "trend": _trend(c),
            "trend_flag": _trend_flag(meetings_booked, in_negotiation, source_total_matches_tile, by_source),
            "funnel": funnel,
            "by_source": by_source,
            "by_region": by_region,
            "hot_leads": _hot_leads(c, user),
            "by_rep": by_rep,
            "source_total_matches_tile": source_total_matches_tile,
        }
    finally:
        c.close()


# ══ tasks / today ═══════════════════════════════════════════════════════════

@app.get("/api/today")
def today(user: auth.CurrentUser = Depends(auth.current_user), page: int = 1, page_size: int = 50):
    """The caller's own tasks, ordered by urgency (score × lateness). Always
    self — 'see everyone's tasks' is a different capability (GET /api/team)."""
    c = con()
    try:
        items = tasks.for_owner(c, user.staff_id) if user.staff_id else []
        return paginate(items, page, page_size)
    finally:
        c.close()


@app.post("/api/tasks")
async def create_task(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await req.json()
    lead_id = body.get("lead_id")
    reason = body.get("reason")
    type_ = body.get("type")
    if not lead_id or not type_:
        raise err(422, "missing_field", "lead_id and type are required.")
    if not reason:
        raise err(422, "missing_field", "reason is required — D12: a task without one is not trusted.")
    # SPEC.md/AUDIT.md: due_at is required on create, and is the field name
    # the app sends — `due` is accepted too, for callers still on the old name.
    due = body.get("due_at") or body.get("due")
    if not due:
        raise err(422, "missing_field", "due_at is required.")
    # Timezone fix (FABLE-AUDIT.md) — a naive due_at is interpreted in THIS
    # caller's own office (never blindly UTC/JST), then stored tz-aware UTC.
    due = _utc_iso(_parse_office_datetime(due, user.office))

    owner_app_id = body.get("owner_user_id") or body.get("owner") or user.id
    if owner_app_id != user.id:
        # matrix row "assign / reassign an owner" — creating a task FOR
        # someone else is the same authority as reassigning one.
        auth.require(user, "assign")

    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        # AUDIT.md critical leak #2 (api.py:589 originally): task creation
        # checked the lead EXISTED but not that the caller may VIEW it —
        # sales created a task on sales2's lead (200) and then received that
        # foreign lead's name/company through /api/today. Require the same
        # lead access the read path enforces, on the write.
        if not _can_view_lead(user, lead):
            raise err(403, "forbidden", "You do not have access to this lead.")
        owner_row = c.execute("SELECT display_name FROM app_user WHERE id=?", (owner_app_id,)).fetchone()
        if not owner_row:
            raise err(404, "no_such_user", "owner_user_id does not match a user.")
        owner_staff_id = auth.ensure_staff_bridge(c, owner_app_id, owner_row["display_name"])
        task_id = tasks.create(c, lead_id, type_, reason=reason, owner_id=owner_staff_id,
                               due_at=due, created_by="human")
        auth.audit(c, user, "create", "task", task_id, after=body)
        # AUDIT.md high finding: used to return a bare {"id": ...}.
        return tasks.get(c, task_id)
    except ValueError as e:
        raise err(422, "invalid_task", str(e))
    finally:
        c.close()


@app.patch("/api/tasks/{task_id}")
async def patch_task(task_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    """action: complete | skip | snooze | reassign | reschedule.

    AUDIT.md critical findings: the app sends {action:'snooze', until} and
    {action:'reassign', owner_user_id}; the backend only accepted
    {action:'reschedule', due_at} and {action:'reassign', owner}. Both SPEC
    spellings work now; the original ones still do too, so nothing already
    depending on them breaks.
    """
    body = await req.json()
    action = body.get("action")
    c = con()
    try:
        row = c.execute(
            """SELECT t.*, au.id AS owner_app_user_id FROM tasks t
                 LEFT JOIN app_user au ON au.staff_id = t.owner_id
                WHERE t.id=?""", (task_id,)).fetchone()
        if not row:
            raise err(404, "not_found", "No such task.")
        is_own = row["owner_app_user_id"] == user.id

        if action in ("complete", "skip"):
            if not (is_own or user.can("tasks.all") or user.can("assign")):
                raise err(403, "forbidden", "You may not complete someone else's task.")
            staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
            tasks.complete(c, task_id, staff_id, skipped=(action == "skip"))
            auth.audit(c, user, action, "task", task_id)
            return tasks.get(c, task_id)

        if action == "reassign":
            auth.require(user, "assign")
            new_owner = body.get("owner_user_id") or body.get("owner")
            if not new_owner:
                raise err(422, "missing_field", "owner_user_id is required to reassign.")
            owner_row = c.execute("SELECT display_name FROM app_user WHERE id=?", (new_owner,)).fetchone()
            if not owner_row:
                raise err(404, "no_such_user", "owner_user_id does not match a user.")
            new_staff_id = auth.ensure_staff_bridge(c, new_owner, owner_row["display_name"])
            c.execute("UPDATE tasks SET owner_id=? WHERE id=?", (new_staff_id, task_id))
            c.commit()
            auth.audit(c, user, "reassign", "task", task_id,
                      before={"owner_id": row["owner_id"]}, after={"owner_id": new_staff_id})
            return tasks.get(c, task_id)

        if action in ("snooze", "reschedule"):
            if not (is_own or user.can("assign")):
                raise err(403, "forbidden", "You may not change someone else's task.")
            due = body.get("until") or body.get("due_at")
            if not due:
                raise err(422, "missing_field",
                         "%s is required." % ("until" if action == "snooze" else "due_at"))
            # Timezone fix (FABLE-AUDIT.md) — see create_task's comment.
            due = _utc_iso(_parse_office_datetime(due, user.office))
            c.execute("UPDATE tasks SET due_at=? WHERE id=?", (due, task_id))
            c.commit()
            auth.audit(c, user, action, "task", task_id,
                      before={"due_at": row["due_at"]}, after={"due_at": due})
            return tasks.get(c, task_id)

        raise err(422, "invalid_action",
                 "action must be one of complete, skip, snooze, reassign, reschedule.")
    finally:
        c.close()


# ══ team ═════════════════════════════════════════════════════════════════════

@app.get("/api/team")
def team(user: auth.CurrentUser = Depends(auth.current_user), page: int = 1, page_size: int = 50):
    auth.require(user, "dashboard.per_rep")
    c = con()
    try:
        rows = tasks.team_status(c)
        # AUDIT.md critical finding: used to return {data,page,page_size,
        # total,digest} with staff-shaped rows the app cannot map to
        # TeamMember. Envelope kept (SPEC.md — every list endpoint is
        # paginated); rows are now canonical, and limited to staff actually
        # bridged to a login (a "team member" without one cannot be an
        # accountability row in the RBAC sense).
        members = [{
            "user_id": r["user_id"],
            "name": r["display_name"],
            "role": r["role_code"],
            "office": r["office"],
            "is_active": r["is_active"],
            "tasks_open": r["tasks_open"],
            "tasks_overdue": r["tasks_overdue"],
            "tasks_escalated": r["tasks_escalated"],
            "leads_owned": r["leads_owned"],
            "meetings_booked": r["meetings_booked"],
            "won": r["won"],
            "idle": r["idle"],
        } for r in rows if r["user_id"]]
        result = paginate(members, page, page_size)
        result["digest"] = tasks.escalations_digest(c, team=rows)   # reuse, don't recompute
        return result
    finally:
        c.close()


# ══ users (admin) ═══════════════════════════════════════════════════════════

@app.get("/api/users")
def list_users(user: auth.CurrentUser = Depends(auth.current_user), page: int = 1, page_size: int = 50):
    auth.require(user, "users.manage")
    c = con()
    try:
        rows = [dict(r) for r in c.execute(
            """SELECT id,email,display_name,role,office,is_active,created_at
                 FROM app_user ORDER BY created_at""")]
        for r in rows:
            r["is_active"] = bool(r["is_active"])
        return paginate(rows, page, page_size)
    finally:
        c.close()


@app.post("/api/users")
async def create_user(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    """SPEC.md names this "invite + role". AUDIT.md high finding: it only
    ever inserted an app_user row — no Supabase Auth user was created, no
    email was sent, but the app reported a completed invitation. There is no
    Supabase project in this environment to invite anyone through, so this
    is made honest instead of faked: the response says plainly that the
    account is pre-approved and nothing was emailed, via `invited` and
    `access_status`. Wiring a real Supabase Admin API invite call is future
    work once a project exists — tracked, not pretended."""
    auth.require(user, "users.manage")
    body = await req.json()
    email = body.get("email")
    role = body.get("role")
    if not email or role not in auth.ROLES:
        raise err(422, "invalid_body", "email and a valid role (%s) are required." % ", ".join(auth.ROLES))
    c = con()
    try:
        if c.execute("SELECT id FROM app_user WHERE email=?", (email,)).fetchone():
            raise err(409, "already_exists", "A user with this email already exists.")
        new_id = str(uuid.uuid4())
        # invited by an admin: pre-approved, active from the start — the
        # fail-closed inactive default is only for *unknown* logins.
        c.execute("""INSERT INTO app_user (id, email, display_name, role, office, is_active)
                     VALUES (?,?,?,?,?,?)""",
                  (new_id, email, body.get("display_name"), role, body.get("office"),
                   bool(body.get("is_active", True))))
        c.commit()
        # canonical AppUser columns only — SELECT * would also leak internal
        # bridge columns (staff_id, supabase_uid) that are not part of the
        # declared contract (AUDIT.md's root complaint: response shapes that
        # were never checked against SPEC.md).
        row = dict(c.execute(
            """SELECT id,email,display_name,role,office,is_active,created_at
                 FROM app_user WHERE id=?""", (new_id,)).fetchone())
        row["is_active"] = bool(row["is_active"])
        row["invited"] = False
        row["access_status"] = "pre_approved_no_invite_sent"
        row["message"] = ("Access pre-approved. No invitation email was sent — Supabase Auth is "
                          "not connected in this environment. The person can sign in once "
                          "Supabase is wired up and their login email matches this record.")
        auth.audit(c, user, "create", "app_user", new_id, after=row)
        return row
    finally:
        c.close()


@app.patch("/api/users/{user_id}")
async def edit_user(user_id: str, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "users.manage")
    body = await req.json()
    c = con()
    try:
        row = c.execute("SELECT * FROM app_user WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise err(404, "not_found", "No such user.")
        fields = {}
        if "role" in body:
            if body["role"] not in auth.ROLES:
                raise err(422, "invalid_role", "role must be one of %s" % ", ".join(auth.ROLES))
            fields["role"] = body["role"]
        if "is_active" in body:
            fields["is_active"] = bool(body["is_active"])
        if "office" in body:
            fields["office"] = body["office"]
        if "display_name" in body:
            fields["display_name"] = body["display_name"]
        if not fields:
            raise err(422, "no_fields", "Nothing to update — pass role, is_active, office or display_name.")
        before = dict(row)
        set_sql = ", ".join("%s=?" % k for k in fields)
        c.execute("UPDATE app_user SET %s WHERE id=?" % set_sql, list(fields.values()) + [user_id])
        c.commit()
        auth.forget_users()
        auth.audit(c, user, "update", "app_user", user_id, before=before,
                  after=dict(c.execute("SELECT * FROM app_user WHERE id=?", (user_id,)).fetchone()))
        # canonical AppUser columns only — see the matching comment in
        # create_user() above.
        after = dict(c.execute(
            """SELECT id,email,display_name,role,office,is_active,created_at
                 FROM app_user WHERE id=?""", (user_id,)).fetchone())
        after["is_active"] = bool(after["is_active"])
        return after
    finally:
        c.close()


# ══ intake ══════════════════════════════════════════════════════════════════

@app.post("/api/import/questions")
async def import_questions(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    """D11 — the importer interrogates the file before writing a single row."""
    auth.require(user, "import.csv")
    body = await req.json()
    headers = body.get("headers", [])
    return {
        "guessed_mapping": ingest.guess_mapping(headers),
        "questions": ingest.import_questions(headers, body.get("sample", [])),
    }


@app.post("/api/referrals")
async def add_referral(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    """D11b — referrer recorded as a person, because three referrals makes
    somebody a partner, and partner is a 30-point rule. Any active login may
    submit one — the matrix has no row restricting this, and referrals are
    how any rep's own network gets captured."""
    body = await req.json()
    c = con()
    try:
        res = ingest.upsert_lead(c, name=body["name"], channel="referral",
                                 company=body.get("company"), email=body.get("email"),
                                 phone=body.get("phone"), note=body.get("note"))

        # D11b#1, the half that was missing: a referrer who is not an internal
        # agent and not yet anyone in the system. Recorded as free text, three
        # introductions from the same person are three unrelated strings and the
        # partner never surfaces. Turning them into a lead is what makes them
        # countable — and a person who introduces clients is exactly the
        # 30-point 'partner' the scoring model is looking for.
        referrer_lead_id = body.get("referrer_lead_id")
        referrer_name = (body.get("referrer_name") or "").strip()
        if not referrer_lead_id and referrer_name:
            ref_res = ingest.upsert_lead(
                c, name=referrer_name, channel="referral",
                email=(body.get("referrer_email") or None),
                note="Introduced %s" % body["name"])
            referrer_lead_id = ref_res["lead_id"]

        c.execute("""INSERT INTO referrals (lead_id, referrer_staff_id, referrer_lead_id,
                                            referrer_name, referrer_email,
                                            relationship_note, created_by_user_id, note)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (res["lead_id"], body.get("referrer_staff_id"), referrer_lead_id,
                   referrer_name or None, body.get("referrer_email"),
                   body.get("relationship_note"), user.id, body.get("note")))
        # three or more introductions and the referrer is a channel, not a contact
        if referrer_lead_id:
            body = dict(body, referrer_lead_id=referrer_lead_id)
            n = db.scalar(c, "SELECT count(*) FROM referrals WHERE referrer_lead_id=?",
                          (referrer_lead_id,))
            if n >= 3:
                scoring.record(c, body["referrer_lead_id"], "partnership",
                               detail="referred %d people" % n, source="system")
                c.execute("UPDATE leads SET relationship='partner' WHERE id=?",
                          (body["referrer_lead_id"],))
        c.commit()
        auth.audit(c, user, "create", "referral", res["lead_id"], after=res)
        return res
    finally:
        c.close()



# ══════════════════════════════════════════════════════════════════════════
# SPEC-V2 — everything below is new for v2. Section headers match SPEC-V2.md.
# ══════════════════════════════════════════════════════════════════════════

# ══ §1 · activity feed / scoring model ═══════════════════════════════════════

ACTIVITY_KINDS = ("open", "click", "page_view", "booking_page_view", "reply", "wants_meeting")
ACTIVITY_LABELS = {
    "open":              ("Opened an email", "メール開封"),
    "click":             ("Clicked a link", "リンククリック"),
    "page_view":         ("Viewed a page", "ページ閲覧"),
    "booking_page_view": ("Viewed the booking page", "相談ページ閲覧"),
    "reply":             ("Replied", "返信"),
    "wants_meeting":     ("Manual +30 — wants to meet", "対面希望（手動+30）"),
}


def _time_ago(occurred_at: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(occurred_at.replace("Z", ""))
    except (TypeError, ValueError):
        return ""
    secs = (now - then).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return "%dm ago" % (secs // 60)
    if secs < 86400:
        return "%dh ago" % (secs // 3600)
    if secs < 86400 * 30:
        return "%dd ago" % (secs // 86400)
    return "%dmo ago" % (secs // (86400 * 30))


@app.get("/api/activity")
def activity(user: auth.CurrentUser = Depends(auth.current_user), page: int = 1, page_size: int = 30):
    """SPEC-V2 §1: reverse-chronological events, role-filtered IN SQL — sales
    sees only its own leads' activity; marketing/admin/office_manager see
    all. Rows carry only name/company (same as LeadSummary) — never an
    identity — so the marketing contact-redaction rule has nothing to leak
    on this surface either."""
    c = con()
    try:
        placeholders = ",".join("?" for _ in ACTIVITY_KINDS)
        where = ["e.kind IN (%s)" % placeholders, "e.voided_at IS NULL", "l.merged_into IS NULL"]
        args = list(ACTIVITY_KINDS)
        if not user.can("leads.all"):
            where.append("l.owner_user_id = ?")
            args.append(user.id)
        where_sql = " AND ".join(where)

        total = db.scalar(c, "SELECT count(*) FROM events e JOIN leads l ON l.id = e.lead_id WHERE %s"
                          % where_sql, args)
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size
        rows = c.execute(
            """SELECT e.id, e.kind, e.detail, e.occurred_at, e.source,
                      l.id AS lead_id, l.name AS lead_name, l.company
                 FROM events e JOIN leads l ON l.id = e.lead_id
                WHERE %s
                ORDER BY e.occurred_at DESC LIMIT ? OFFSET ?""" % where_sql,
            args + [page_size, offset]).fetchall()

        rules = scoring._rules(c)
        now = datetime.utcnow()
        data = []
        for r in rows:
            rule = rules.get(r["kind"])
            label_en, label_ja = ACTIVITY_LABELS.get(r["kind"], (r["kind"], r["kind"]))
            data.append({
                "id": r["id"], "lead_id": r["lead_id"], "lead_name": r["lead_name"], "company": r["company"],
                "kind": r["kind"], "label_en": label_en, "label_ja": label_ja,
                "points": int(rule["points"]) if rule else 0,
                "context": r["detail"], "occurred_at": r["occurred_at"],
                "time_ago": _time_ago(r["occurred_at"], now), "source": r["source"],
            })
        return {"data": data, "page": page, "page_size": page_size, "total": total}
    finally:
        c.close()


@app.get("/api/scoring/model")
def scoring_model(user: auth.CurrentUser = Depends(auth.current_user)):
    """SPEC-V2 §1: the 10 rules split behaviour(fades)/fact(permanent) per
    D8, the decay curve in words, the threshold, and how many leads crossed
    it this week. No lead-specific data — every active role may view it."""
    c = con()
    try:
        rules = [dict(r) for r in c.execute(
            "SELECT event_kind, points, decays, label_en, label_ja FROM scoring_rules WHERE active = TRUE")]
        for r in rules:
            r["group"] = "behaviour" if r["decays"] else "fact"
            r["decays"] = bool(r["decays"])
        rules.sort(key=lambda r: (r["group"] != "behaviour", -r["points"]))

        thr = scoring.threshold(c)
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        # N4 (FABLE-AUDIT-R2.md) — this used to SELECT every live lead and
        # call scoring.score() TWICE each (now + a week ago) to answer
        # "crossed this week": ~60k explain() calls at 30k leads, the exact
        # H1 pattern H1 was meant to kill, missed because the materialized
        # leads.score column can only answer "now". score_threshold_crossings
        # (written by scoring.recompute_lead — the one place leads.score is
        # ever changed) already has the history; one indexed query answers
        # it instead. A lead only counts if it is both currently >= threshold
        # (matches the old semantics — a lead that crossed up and back down
        # within the week no longer reads as "hot" and should not either)
        # and had an 'up' crossing in the window.
        week_ago_ts = week_ago.isoformat(sep=" ", timespec="seconds")
        crossed_this_week = db.scalar(
            c,
            """SELECT count(DISTINCT stc.lead_id)
                 FROM score_threshold_crossings stc
                 JOIN leads l ON l.id = stc.lead_id
                WHERE stc.direction = 'up' AND stc.crossed_at >= ?
                  AND l.merged_into IS NULL AND l.score >= ?""",
            (week_ago_ts, thr))

        return {
            "rules": rules,
            "decay": {
                "half_life_days": scoring.HALF_LIFE_DAYS,
                "zero_days": scoring.ZERO_DAYS,
                "explanation_en": ("Behaviour points (opens, clicks, page views) halve in value at "
                                   "%d days and reach zero at %d. Facts (booked, wants to meet, "
                                   "corporate, partner, high budget) never decay." %
                                   (scoring.HALF_LIFE_DAYS, scoring.ZERO_DAYS)),
                "explanation_ja": ("行動ポイント（開封・クリック・閲覧）は%d日で半減し、%d日でゼロになります。"
                                   "事実ポイント（予約・対面希望・法人・パートナー・高予算）は減衰しません。" %
                                   (scoring.HALF_LIFE_DAYS, scoring.ZERO_DAYS)),
                # H3 (FABLE-AUDIT.md) — which unratified D8 interpretation
                # (per_event/lead_level) is currently active. Never silent.
                "mode": scoring.DECAY_MODE,
            },
            "threshold": thr,
            "crossed_this_week": crossed_this_week,
        }
    finally:
        c.close()


# ══ §2 · nurture / sequences ═════════════════════════════════════════════════

EDITORIAL_RULE = {
    "en": "Not “Won’t you buy Dubai real estate?” — “Dubai is surprisingly easy to live in.”",
    "ja": "「ドバイの不動産を買いませんか？」ではなく「ドバイは意外と住みやすい」",
}

# D17 — proposed, NOT ratified by Balraj. Implemented and labelled
# `provisional` here exactly as SPEC-V2 requires, on every sequence.
SEQUENCE_EXIT_CONDITIONS = [
    {"action": "stop", "label_en": "Stop", "label_ja": "停止",
     "trigger_en": "unsubscribed or bounced", "trigger_ja": "配信停止・バウンス", "provisional": True},
    {"action": "pause", "label_en": "Pause", "label_ja": "一時停止",
     "trigger_en": "replied, booked, or score >= 40", "trigger_ja": "返信・予約・スコア40以上",
     "provisional": True},
    {"action": "dormant", "label_en": "Dormant", "label_ja": "休眠",
     "trigger_en": "finished step 7 with no reaction", "trigger_ja": "第7ステップ完了・無反応",
     "provisional": True},
]


def _sequence_step_stats(c, step_id: int) -> dict:
    sent = db.scalar(c, "SELECT count(*) FROM sends WHERE step_id=?", (step_id,))
    opened = db.scalar(c, """SELECT count(DISTINCT s.send_id) FROM sends s
                              JOIN events e ON e.send_id = s.send_id
                             WHERE s.step_id=? AND e.kind='open' AND e.voided_at IS NULL""", (step_id,))
    clicked = db.scalar(c, """SELECT count(DISTINCT s.send_id) FROM sends s
                               JOIN events e ON e.send_id = s.send_id
                              WHERE s.step_id=? AND e.kind='click' AND e.voided_at IS NULL""", (step_id,))
    return {
        "sent": sent, "opened": opened, "clicked": clicked,
        "open_rate": round(opened / sent, 3) if sent else None,
        "click_rate": round(clicked / sent, 3) if sent else None,
    }


def _sequence_summary(c, row) -> dict:
    # Three queries per sequence however many steps it has — it used to be one
    # per sequence plus three per step (the Nurture list ran 24 statements for
    # one 7-step sequence). A send belongs to exactly one step, so counting
    # distinct sends across the whole sequence equals summing the per-step
    # distinct counts _sequence_step_stats() gives.
    step_count = db.scalar(c, "SELECT count(*) FROM sequence_steps WHERE sequence_id=?", (row["id"],))
    sent = db.scalar(c, """SELECT count(*) FROM sends s
                             JOIN sequence_steps st ON st.id = s.step_id
                            WHERE st.sequence_id=?""", (row["id"],))
    ev = c.execute(
        """SELECT count(DISTINCT CASE WHEN e.kind='open'  THEN s.send_id END) AS opened,
                  count(DISTINCT CASE WHEN e.kind='click' THEN s.send_id END) AS clicked
             FROM sends s
             JOIN sequence_steps st ON st.id = s.step_id
             JOIN events e ON e.send_id = s.send_id
            WHERE st.sequence_id=? AND e.voided_at IS NULL""", (row["id"],)).fetchone()
    totals = {"sent": int(sent or 0), "opened": int(ev["opened"] or 0),
              "clicked": int(ev["clicked"] or 0)}
    return {
        "id": row["id"], "name": row["name"], "active": bool(row["active"]), "step_count": int(step_count or 0),
        "total_sent": totals["sent"], "total_opened": totals["opened"], "total_clicked": totals["clicked"],
        "open_rate": round(totals["opened"] / totals["sent"], 3) if totals["sent"] else None,
        "click_rate": round(totals["clicked"] / totals["sent"], 3) if totals["sent"] else None,
    }


def _step_stats_bulk(c, step_ids) -> dict:
    """_sequence_step_stats for a whole sequence in one query. The detail screen
    showed 7 steps and paid 3 statements each; on the production host that was most of its
    11-second load. Same numbers, one round trip."""
    ids = [i for i in dict.fromkeys(step_ids) if i is not None]
    out = {i: {"sent": 0, "opened": 0, "clicked": 0, "open_rate": None, "click_rate": None}
           for i in ids}
    if not ids:
        return out
    rows = c.execute(
        """SELECT s.step_id,
                  count(DISTINCT s.send_id) AS sent,
                  count(DISTINCT CASE WHEN e.kind='open'  AND e.voided_at IS NULL
                                      THEN s.send_id END) AS opened,
                  count(DISTINCT CASE WHEN e.kind='click' AND e.voided_at IS NULL
                                      THEN s.send_id END) AS clicked
             FROM sends s LEFT JOIN events e ON e.send_id = s.send_id
            WHERE s.step_id IN (%s) GROUP BY s.step_id""" % ",".join("?" * len(ids)), ids)
    for r in rows:
        sent, opened, clicked = int(r["sent"] or 0), int(r["opened"] or 0), int(r["clicked"] or 0)
        out[r["step_id"]] = {
            "sent": sent, "opened": opened, "clicked": clicked,
            "open_rate": round(opened / sent, 3) if sent else None,
            "click_rate": round(clicked / sent, 3) if sent else None,
        }
    return out


def _sequence_step_out(c, s, stats=None) -> dict:
    return {
        "id": s["id"], "step_no": s["step_no"], "offset_days": s["offset_days"],
        "subject": s["subject"], "purpose": s["purpose"],
        "question": s["question"], "question_ja": s["question_ja"],
        "cta": s["cta"], "active": bool(s["active"]),
        "stats": stats if stats is not None else _sequence_step_stats(c, s["id"]),
    }


@app.get("/api/sequences")
def list_sequences(user: auth.CurrentUser = Depends(auth.current_user), page: int = 1, page_size: int = 20):
    auth.require(user, "nurture.view")
    c = con()
    try:
        rows = c.execute("SELECT * FROM sequences ORDER BY id").fetchall()
        return paginate([_sequence_summary(c, r) for r in rows], page, page_size)
    finally:
        c.close()


@app.get("/api/sequences/{sequence_id}")
def get_sequence(sequence_id: int, user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "nurture.view")
    c = con()
    try:
        row = c.execute("SELECT * FROM sequences WHERE id=?", (sequence_id,)).fetchone()
        if not row:
            raise err(404, "not_found", "No such sequence.")
        steps = c.execute("SELECT * FROM sequence_steps WHERE sequence_id=? ORDER BY step_no",
                         (sequence_id,)).fetchall()
        summary = _sequence_summary(c, row)
        stats = _step_stats_bulk(c, [s["id"] for s in steps])
        summary.update({
            "steps": [_sequence_step_out(c, s, stats.get(s["id"])) for s in steps],
            "editorial_rule": EDITORIAL_RULE,
            "exit_conditions": SEQUENCE_EXIT_CONDITIONS,
            "never_repeat_note": ("Enforced at the database — sends.(lead_id, step_id) is UNIQUE, so "
                                  "the same lead can never receive the same step twice."),
        })
        return summary
    finally:
        c.close()


@app.patch("/api/sequences/{sequence_id}/steps/{step_no}")
async def edit_sequence_step(sequence_id: int, step_no: int, req: Request,
                             user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "nurture.edit")
    body = await req.json()
    c = con()
    try:
        row = c.execute("SELECT * FROM sequence_steps WHERE sequence_id=? AND step_no=?",
                        (sequence_id, step_no)).fetchone()
        if not row:
            raise err(404, "not_found", "No such sequence step.")
        editable = {"subject", "purpose", "question", "question_ja", "cta", "offset_days", "active"}
        unknown = set(body) - editable
        if unknown:
            raise err(422, "not_editable", "These fields cannot be edited here: %s. Editable: %s"
                     % (sorted(unknown), sorted(editable)))
        fields = {k: v for k, v in body.items() if k in editable}
        if not fields:
            raise err(422, "no_editable_fields", "Nothing to update.")
        before = dict(row)
        set_sql = ", ".join("%s=?" % k for k in fields)
        c.execute("UPDATE sequence_steps SET %s WHERE id=?" % set_sql, list(fields.values()) + [row["id"]])
        c.commit()
        after = c.execute("SELECT * FROM sequence_steps WHERE id=?", (row["id"],)).fetchone()
        auth.audit(c, user, "update", "sequence_step", row["id"], before=before, after=dict(after))
        return _sequence_step_out(c, after)
    finally:
        c.close()


@app.get("/api/sequences/{sequence_id}/stats")
def sequence_stats(sequence_id: int, user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "nurture.view")
    c = con()
    try:
        row = c.execute("SELECT * FROM sequences WHERE id=?", (sequence_id,)).fetchone()
        if not row:
            raise err(404, "not_found", "No such sequence.")
        steps = c.execute("SELECT * FROM sequence_steps WHERE sequence_id=? ORDER BY step_no",
                         (sequence_id,)).fetchall()
        bulk = _step_stats_bulk(c, [s["id"] for s in steps])
        per_step = [{"step_no": s["step_no"], "subject": s["subject"], **bulk[s["id"]]}
                   for s in steps]
        # Provable, not asserted: sends.(lead_id, step_id) is UNIQUE at the
        # DB level, so this can only ever read 0 — a live check, not a claim.
        dup = db.scalar(c, """SELECT count(*) FROM (
                                 SELECT lead_id, step_id, count(*) c FROM sends
                                  WHERE step_id IN (SELECT id FROM sequence_steps WHERE sequence_id=?)
                                  GROUP BY lead_id, step_id HAVING c > 1)""", (sequence_id,))
        summary = _sequence_summary(c, row)
        summary.update({"per_step": per_step, "duplicate_sends_detected": dup})
        return summary
    finally:
        c.close()


# ══ §3 · SNS ══════════════════════════════════════════════════════════════════

@app.get("/api/sns/patterns")
def sns_patterns(user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "sns.view")
    c = con()
    try:
        rows = c.execute("SELECT * FROM sns_patterns WHERE active = TRUE ORDER BY key").fetchall()
        # Grouped: one count query and one sample query for every pattern, not
        # two per pattern plus ~3 scoring queries per sample card. Samples use
        # the materialized score self-healed by fresh_score(), the same number
        # explain() gives and every other list screen shows.
        counts = {r["pattern_key"]: int(r["n"]) for r in c.execute(
            """SELECT pattern_key, count(DISTINCT lead_id) AS n
                 FROM lead_sns_pattern GROUP BY pattern_key""")}
        samples = {}
        for s in c.execute(
                """SELECT pattern_key, id, name, company, stage, owner_user_id,
                          score, score_updated_at FROM (
                       SELECT p.pattern_key, l.id, l.name, l.company, l.stage,
                              l.owner_user_id, l.score, l.score_updated_at,
                              ROW_NUMBER() OVER (PARTITION BY p.pattern_key
                                                 ORDER BY p.set_at DESC) AS rn
                         FROM lead_sns_pattern p JOIN leads l ON l.id = p.lead_id
                        WHERE l.merged_into IS NULL
                   ) AS ranked WHERE rn <= 5
                   ORDER BY pattern_key, rn"""):
            samples.setdefault(s["pattern_key"], []).append(s)
        owners = _owner_names(c, [s["owner_user_id"] for ss in samples.values() for s in ss])
        data = []
        for r in rows:
            leads_count = counts.get(r["key"], 0)
            sample = []
            for s in samples.get(r["key"], []):
                sc = scoring.fresh_score(c, s["id"], s["score_updated_at"], s["score"], commit=False)
                sample.append({"id": s["id"], "name": s["name"], "company": s["company"],
                              "stage": s["stage"], "score": sc, "heat": scoring.heat(sc),
                              "owner_name": owners.get(s["owner_user_id"])})
            data.append({
                "key": r["key"], "title_en": r["title_en"], "title_ja": r["title_ja"],
                "description_en": r["description_en"], "description_ja": r["description_ja"],
                "leads_count": leads_count, "leads_sample": sample,
            })
        return {"data": data, "page": 1, "page_size": max(len(data), 1), "total": len(data)}
    finally:
        c.close()


SNS_FUNNEL_STEPS = [
    ("sns_post",       "SNS posts",           "SNS投稿"),
    ("lp_view",        "LP / free material",  "LP / 無料資料"),
    ("email_captured", "Email captured",      "メール取得"),
    ("sequence_sent",  "Step-email sequence", "ステップメール"),
    ("consult_booked", "Consultation booked", "相談予約"),
]


@app.get("/api/sns/funnel")
def sns_funnel(user: auth.CurrentUser = Depends(auth.current_user)):
    """SPEC-V2 §3: SNS投稿 → LP/無料資料 → メール取得 → ステップメール → 相談予約.
    The first two steps have no analytics API wired to any SNS platform —
    nothing in this codebase could compute them live — so they are read from
    sns_funnel_manual and marked tracked=false, source='manual'. The rest
    come straight from real rows."""
    auth.require(user, "sns.view")
    c = con()
    try:
        manual = {r["step_key"]: r["count"] for r in c.execute("SELECT * FROM sns_funnel_manual")}
        email_captured = db.scalar(c, "SELECT count(DISTINCT lead_id) FROM lead_channels WHERE channel='sns'")
        sequence_sent = db.scalar(c, """SELECT count(DISTINCT s.lead_id) FROM sends s
                                        JOIN lead_channels lc ON lc.lead_id=s.lead_id AND lc.channel='sns'""")
        consult_booked = db.scalar(c, """SELECT count(DISTINCT l.id) FROM leads l
                                         JOIN lead_channels lc ON lc.lead_id=l.id AND lc.channel='sns'
                                        WHERE l.merged_into IS NULL
                                          AND l.stage IN ('meeting_booked','in_negotiation','won')""")
        values = {
            "sns_post": (manual.get("sns_post", 0), False, "manual"),
            "lp_view": (manual.get("lp_view", 0), False, "manual"),
            "email_captured": (email_captured, True, "computed"),
            "sequence_sent": (sequence_sent, True, "computed"),
            "consult_booked": (consult_booked, True, "computed"),
        }
        steps = [{"step": key, "label_en": en, "label_ja": ja,
                 "count": values[key][0], "tracked": values[key][1], "source": values[key][2]}
                for key, en, ja in SNS_FUNNEL_STEPS]
        return {"steps": steps}
    finally:
        c.close()


# ══ §4 · booking ══════════════════════════════════════════════════════════════

def _booking_settings_payload(c) -> dict:
    row = c.execute("SELECT * FROM booking_settings WHERE id=1").fetchone()
    meeting_types = [dict(r) for r in c.execute("SELECT * FROM booking_meeting_types ORDER BY key")]
    for mt in meeting_types:
        reps = c.execute(
            """SELECT au.id, au.display_name FROM booking_rep_meeting_types brt
                 JOIN app_user au ON au.id = brt.user_id WHERE brt.meeting_type=?""", (mt["key"],)).fetchall()
        mt["reps"] = [{"user_id": r["id"], "name": r["display_name"]} for r in reps]
    return {
        "jst_gst_gap_hours": row["jst_gst_gap_hours"],
        "business_hours_start": row["business_hours_start"], "business_hours_end": row["business_hours_end"],
        "slot_length_minutes": row["slot_length_minutes"], "timezone": row["timezone"], "note": row["note"],
        "meeting_types": meeting_types,
        "public_booking_page": {
            "configured": False,
            "reason": "The public-facing booking page needs its own domain and public hosting — out of scope here.",
            "manual_path": "Share available slots by phone/email until the public page exists.",
        },
    }


@app.get("/api/booking/settings")
def get_booking_settings(user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "booking.manage")
    c = con()
    try:
        return _booking_settings_payload(c)
    finally:
        c.close()


@app.patch("/api/booking/settings")
async def patch_booking_settings(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "booking.manage")
    body = await req.json()
    c = con()
    try:
        fields = {k: body[k] for k in ("business_hours_start", "business_hours_end",
                                       "slot_length_minutes", "note") if k in body}
        before = dict(c.execute("SELECT * FROM booking_settings WHERE id=1").fetchone())
        if fields:
            set_sql = ", ".join("%s=?" % k for k in fields)
            c.execute("UPDATE booking_settings SET %s, updated_at=datetime('now') WHERE id=1" % set_sql,
                     list(fields.values()))
        if "rep_meeting_types" in body:
            c.execute("DELETE FROM booking_rep_meeting_types")
            for rep in body["rep_meeting_types"]:
                for mt in rep.get("meeting_types", []):
                    c.execute("INSERT OR IGNORE INTO booking_rep_meeting_types (user_id, meeting_type) VALUES (?,?)",
                             (rep["user_id"], mt))
        c.commit()
        auth.audit(c, user, "update", "booking_settings", 1, before=before, after=body)
        return _booking_settings_payload(c)
    finally:
        c.close()


def _week_bounds(week: Optional[str]) -> tuple:
    """Naive local-calendar Monday00:00 → next Monday00:00 — a WEEK is a
    calendar concept (labels only, `week_start`/`week_end` dates), never a
    specific instant, so it stays tz-naive on purpose. Anything that needs an
    actual instant (querying calendar_events, building slots) goes through
    _week_bounds_utc() below, which attaches an office timezone first."""
    if week:
        try:
            anchor = datetime.fromisoformat(week)
        except ValueError:
            raise err(422, "invalid_week", "week must be an ISO date, e.g. 2026-08-17.")
    else:
        anchor = datetime.utcnow()
    monday = (anchor - timedelta(days=anchor.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, monday + timedelta(days=7)


def _week_bounds_utc(week_start: datetime, week_end: datetime, office_zone: ZoneInfo) -> tuple:
    """Timezone fix (FABLE-AUDIT.md) — calendar_events.starts_at/ends_at are
    stored timezone-aware UTC; querying them with a naive local boundary is
    exactly the kind of silent mismatch that put a Dubai booking in the
    wrong hour when Tokyo read it. Attach the given office's zone to the
    naive calendar boundary, THEN convert to UTC."""
    return (week_start.replace(tzinfo=office_zone).astimezone(timezone.utc),
            week_end.replace(tzinfo=office_zone).astimezone(timezone.utc))


@app.get("/api/booking/slots")
def booking_slots(user: auth.CurrentUser = Depends(auth.current_user),
                  meeting_type: str = "consult_30", week: Optional[str] = None):
    auth.require(user, "booking.manage")
    c = con()
    try:
        mt = c.execute("SELECT * FROM booking_meeting_types WHERE key=?", (meeting_type,)).fetchone()
        if not mt:
            raise err(422, "invalid_meeting_type", "No such meeting type: %s" % meeting_type)
        reps = [r["user_id"] for r in c.execute(
            "SELECT user_id FROM booking_rep_meeting_types WHERE meeting_type=?", (meeting_type,))]
        settings = c.execute("SELECT * FROM booking_settings WHERE id=1").fetchone()
        week_start, week_end = _week_bounds(week)

        # Timezone fix (FABLE-AUDIT.md) — business hours are the JAPAN
        # OFFICE's own clock (settings.timezone, Asia/Tokyo). Building slots
        # as naive datetimes and comparing them against calendar_events rows
        # entered from whichever office happened to book them is exactly how
        # a Dubai-entered booking and a Tokyo-read slot used to disagree by
        # the JST/GST gap — 5 hours, not the 4 the decision log said (fixed
        # alongside this). Every slot is built tz-aware in the office's own
        # zone, converted to UTC before the busy-window comparison, and
        # returned as UTC ISO-8601 with an explicit offset — unambiguous no
        # matter which office reads it.
        office_zone = _office_zone("tokyo")
        week_start_utc, week_end_utc = _week_bounds_utc(week_start, week_end, office_zone)

        booked = c.execute(
            """SELECT owner_user_id, starts_at, ends_at FROM calendar_events
                WHERE type=? AND status='booked' AND starts_at >= ? AND starts_at < ?""",
            (meeting_type, _utc_iso(week_start_utc), _utc_iso(week_end_utc))).fetchall()
        busy = {}
        for b in booked:
            # office=None → legacy/naive rows (pre-fix data) default to
            # Tokyo, matching this endpoint's own business-hours zone; rows
            # already carrying an offset parse exactly as written regardless.
            b_start = _parse_office_datetime(b["starts_at"], None)
            b_end = _parse_office_datetime(b["ends_at"], None)
            busy.setdefault(b["owner_user_id"], []).append((b_start, b_end))

        slot_len = timedelta(minutes=settings["slot_length_minutes"])
        start_h, start_m = (int(x) for x in settings["business_hours_start"].split(":"))
        end_h, end_m = (int(x) for x in settings["business_hours_end"].split(":"))

        slots = []
        for day_offset in range(7):
            day = week_start + timedelta(days=day_offset)
            if day.weekday() >= 5:
                continue
            slot_start = day.replace(hour=start_h, minute=start_m, tzinfo=office_zone)
            day_end = day.replace(hour=end_h, minute=end_m, tzinfo=office_zone)
            while slot_start + slot_len <= day_end:
                slot_end = slot_start + slot_len
                slot_start_utc = slot_start.astimezone(timezone.utc)
                slot_end_utc = slot_end.astimezone(timezone.utc)
                available_reps = [rep_id for rep_id in reps if not any(
                    slot_start_utc < b_end and slot_end_utc > b_start
                    for b_start, b_end in busy.get(rep_id, []))]
                if available_reps:
                    slots.append({
                        "starts_at": _utc_iso(slot_start_utc),
                        "ends_at": _utc_iso(slot_end_utc),
                        "available_rep_user_ids": available_reps,
                    })
                slot_start = slot_end

        return {
            "meeting_type": meeting_type, "duration_minutes": mt["duration_minutes"],
            "week_start": week_start.date().isoformat(),
            "week_end": (week_end - timedelta(days=1)).date().isoformat(),
            "timezone": settings["timezone"], "jst_gst_gap_hours": settings["jst_gst_gap_hours"],
            "slots": slots,
        }
    finally:
        c.close()



# ══ public booking ════════════════════════════════════════════════════════════
# Everything below is reachable WITHOUT a login. The rules are different here:
# a stranger may learn which half-hours are free, and nothing else. No rep name,
# no rep id, no count of staff, no hint that an address is already a lead.


def _slot_grid(c, meeting_type: str, week: Optional[str],
               days_ahead: Optional[int] = None) -> dict:
    """Shared slot maths for the internal and public views.

    Extracted from /api/booking/slots rather than reimplemented: the timezone
    handling here is the part that was wrong once already (a Dubai-entered
    booking read in Tokyo landed five hours out, D27b), and two copies of it
    would drift.
    """
    mt = c.execute("SELECT * FROM booking_meeting_types WHERE key=?",
                   (meeting_type,)).fetchone()
    if not mt:
        raise err(422, "invalid_meeting_type", "No such meeting type: %s" % meeting_type)
    reps = booking.reps_for(c, meeting_type)
    settings = c.execute("SELECT * FROM booking_settings WHERE id=1").fetchone()
    office_zone = _office_zone("tokyo")

    if days_ahead:
        # A booking PAGE shows a rolling window from today, not the current
        # calendar week. On a Sunday the calendar week is entirely in the past,
        # so a week-based grid offers a customer no times at all — which is how
        # this was first noticed.
        week_start = datetime.utcnow().replace(hour=0, minute=0, second=0,
                                               microsecond=0)
        week_end = week_start + timedelta(days=days_ahead)
        span = days_ahead
    else:
        week_start, week_end = _week_bounds(week)
        span = 7
    week_start_utc, week_end_utc = _week_bounds_utc(week_start, week_end, office_zone)

    booked = c.execute(
        """SELECT owner_user_id, starts_at, ends_at FROM calendar_events
            WHERE status='booked' AND starts_at >= ? AND starts_at < ?""",
        (_utc_iso(week_start_utc), _utc_iso(week_end_utc))).fetchall()
    busy = {}
    for b in booked:
        busy.setdefault(b["owner_user_id"], []).append(
            (_parse_office_datetime(b["starts_at"], None),
             _parse_office_datetime(b["ends_at"], None)))

    slot_len = timedelta(minutes=int(mt["duration_minutes"]))
    start_h, start_m = (int(x) for x in settings["business_hours_start"].split(":"))
    end_h, end_m = (int(x) for x in settings["business_hours_end"].split(":"))
    now_utc = datetime.now(timezone.utc)

    slots = []
    for day_offset in range(span):
        day = week_start + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        slot_start = day.replace(hour=start_h, minute=start_m, tzinfo=office_zone)
        day_end = day.replace(hour=end_h, minute=end_m, tzinfo=office_zone)
        while slot_start + slot_len <= day_end:
            slot_end = slot_start + slot_len
            su, eu = slot_start.astimezone(timezone.utc), slot_end.astimezone(timezone.utc)
            if su > now_utc:
                free = [r for r in reps if not any(
                    su < b_end and eu > b_start for b_start, b_end in busy.get(r, []))]
                if free:
                    slots.append({"starts_at": _utc_iso(su), "ends_at": _utc_iso(eu),
                                  "available_rep_user_ids": free})
            slot_start = slot_end

    return {"meeting_type": meeting_type,
            "duration_minutes": int(mt["duration_minutes"]),
            "week_start": week_start.date().isoformat(),
            "week_end": (week_end - timedelta(days=1)).date().isoformat(),
            "timezone": settings["timezone"],
            "jst_gst_gap_hours": settings["jst_gst_gap_hours"],
            "slots": slots}


def _booking_error(e: "booking.BookingError"):
    return err(e.status, e.code, e.message)


BOOKING_PAGE = (Path(__file__).resolve().parent / "booking_page.html")


@app.get("/book")
def booking_page():
    """The customer-facing booking page. Public, no login, no CDN script — it is
    served to strangers on unknown networks, and a third-party script here would
    be one more thing that can break, be blocked, or watch someone type their
    phone number."""
    return Response(content=BOOKING_PAGE.read_text(encoding="utf-8"),
                    media_type="text/html; charset=utf-8",
                    headers={"X-Robots-Tag": "noindex, nofollow",
                             "Referrer-Policy": "no-referrer"})


@app.get("/api/public/booking/meeting-types")
def public_meeting_types():
    c = con()
    try:
        return {"data": booking.public_meeting_types(c)}
    finally:
        c.close()


@app.get("/api/public/booking/slots")
def public_slots(meeting_type: str = "consult_30", week: Optional[str] = None,
                 days: int = 21):
    """Times only. `available_rep_user_ids` is stripped — a customer has no
    business knowing who works here or how many people do.

    Defaults to a rolling three weeks from today rather than the current
    calendar week, so someone opening the page on a Saturday is offered next
    week instead of an empty grid."""
    c = con()
    try:
        days = max(1, min(int(days or 21), 60))
        grid = _slot_grid(c, meeting_type, week, days_ahead=None if week else days)
        grid["slots"] = [{"starts_at": s["starts_at"], "ends_at": s["ends_at"]}
                         for s in grid["slots"]]
        return grid
    finally:
        c.close()


@app.post("/api/public/booking/confirm")
async def public_confirm(req: Request):
    body = await req.json()
    c = con()
    try:
        try:
            return booking.confirm(
                c,
                meeting_type=str(body.get("meeting_type") or ""),
                starts_at_utc=str(body.get("starts_at") or ""),
                name=body.get("name"), email=body.get("email"),
                phone=body.get("phone"), note=body.get("note"),
                source="booking_page",
                request_id=body.get("request_id"))
        except booking.BookingError as e:
            raise _booking_error(e)
    finally:
        c.close()


@app.get("/api/public/booking/{ref}")
def public_get_booking(ref: str):
    c = con()
    try:
        row = booking.by_ref(c, ref)
        if row is None:
            raise err(404, "not_found", "We could not find that appointment.")
        return booking._public_view(c, row)
    finally:
        c.close()


@app.post("/api/public/booking/{ref}/cancel")
def public_cancel(ref: str):
    c = con()
    try:
        try:
            return booking.cancel(c, ref, by="customer")
        except booking.BookingError as e:
            raise _booking_error(e)
    finally:
        c.close()


@app.post("/api/public/booking/{ref}/reschedule")
async def public_reschedule(ref: str, req: Request):
    body = await req.json()
    c = con()
    try:
        try:
            return booking.reschedule(c, ref, str(body.get("starts_at") or ""))
        except booking.BookingError as e:
            raise _booking_error(e)
    finally:
        c.close()


@app.post("/api/public/enquiry")
async def public_enquiry(req: Request):
    """The website / landing-page form. The proposal lists LP and form
    enquiries as first-class sources; there was no door for them, so the
    dashboard reported a source it could not receive.

    Consent is recorded exactly as ticked and never inferred: an enquiry
    without the box ticked still becomes a lead the sales team may call, but
    it does NOT become someone the nurture sequence may email.
    """
    body = await req.json()
    c = con()
    try:
        try:
            name, email, phone, message = booking.clean_contact(
                body.get("name"), body.get("email"), body.get("phone"),
                body.get("message"))
        except booking.BookingError as e:
            raise _booking_error(e)
        form_key = str(body.get("form_key") or "website")[:64]
        consent = bool(body.get("consent"))
        # 'lp_form' is the existing channel name for a landing-page form, and
        # it already carries an 'explicit' consent default. That default is
        # deliberately NOT relied on here — the checkbox below is.
        res = ingest.upsert_lead(c, name=name, email=email, phone=phone or None,
                                 channel="lp_form")
        lead_id = res["lead_id"]
        c.execute("""
            INSERT INTO web_enquiries (lead_id, form_key, name, email, phone, message,
                                       source, medium, campaign, landing_page,
                                       consent_given, consent_text)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lead_id, form_key, name, email, phone, message,
             str(body.get("utm_source") or "")[:120] or None,
             str(body.get("utm_medium") or "")[:120] or None,
             str(body.get("utm_campaign") or "")[:120] or None,
             str(body.get("landing_page") or "")[:500] or None,
             consent, str(body.get("consent_text") or "")[:500] or None))
        # `lp_form` carries an 'explicit' consent DEFAULT in CONSENT_BY_CHANNEL,
        # which assumes the checkbox was ticked. When it was not, that default
        # is simply wrong and must be corrected — a form submitted without the
        # box is a lead the sales team may call, never someone the sequence may
        # email. Imports and forms must not manufacture consent.
        if consent:
            c.execute(
                "INSERT OR IGNORE INTO lead_consent (lead_id, basis, obtained_via)"
                " VALUES (?,?,?)", (lead_id, "explicit", "web_form:%s" % form_key))
            c.execute("UPDATE lead_consent SET basis='explicit', obtained_via=?"
                      " WHERE lead_id=? AND withdrawn_at IS NULL",
                      ("web_form:%s" % form_key, lead_id))
        else:
            c.execute("DELETE FROM lead_consent WHERE lead_id=? AND basis='explicit'"
                      " AND obtained_at >= datetime('now','-1 minute')", (lead_id,))
        c.commit()
        # Never confirm or deny whether this person was already known.
        return {"ok": True, "message": "Thank you — we will be in touch."}
    finally:
        c.close()


# ══ §5 · assign / calendar ════════════════════════════════════════════════════

def _rule_matches(rule, lead: dict) -> bool:
    if rule["is_fallback"]:
        return True
    if rule["match_field"] == "region":
        return rule["match_value"] in (lead.get("region") or [])
    if rule["match_field"] == "purpose":
        return rule["match_value"] == lead.get("purpose")
    # language/topic have no source column on `leads` yet — never fires
    # rather than guessing, so a rule referencing them is visibly inert
    # instead of silently wrong.
    return False


def _assignment_rule_row(c, r, sample_lead=None) -> dict:
    out = {
        "id": r["id"], "priority": r["priority"], "match_field": r["match_field"],
        "match_value": r["match_value"], "owner_user_id": r["owner_user_id"],
        "owner_name": _owner_name(c, r["owner_user_id"]),
        "is_fallback": bool(r["is_fallback"]), "active": bool(r["active"]),
    }
    if sample_lead is not None:
        out["fires_for_sample"] = _rule_matches(r, sample_lead)
    return out


@app.get("/api/assignment/rules")
def list_assignment_rules(user: auth.CurrentUser = Depends(auth.current_user),
                          sample_lead_id: Optional[int] = None):
    auth.require(user, "assignment.manage")
    c = con()
    try:
        rows = c.execute("SELECT * FROM assignment_rules WHERE active = TRUE ORDER BY priority").fetchall()
        sample = None
        if sample_lead_id:
            lr = _lead_row(c, sample_lead_id)
            if not lr:
                raise err(404, "not_found", "No such lead for sample_lead_id.")
            sample = {"region": _region_list(c, sample_lead_id), "purpose": lr["purpose"]}
        data = [_assignment_rule_row(c, r, sample) for r in rows]
        result = {"data": data, "page": 1, "page_size": max(len(data), 1), "total": len(data)}
        if sample_lead_id:
            result["evaluated_for_sample_lead_id"] = sample_lead_id
            result["winning_rule_id"] = next((d["id"] for d in data if d["fires_for_sample"]), None)
        return result
    finally:
        c.close()


@app.put("/api/assignment/rules")
async def replace_assignment_rules(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "assignment.manage")
    body = await req.json()
    rules = body.get("rules")
    if not isinstance(rules, list) or not rules:
        raise err(422, "missing_field", "rules must be a non-empty array.")
    fallbacks = [r for r in rules if r.get("is_fallback")]
    if len(fallbacks) != 1:
        raise err(422, "invalid_rules", "Exactly one rule must be the fallback (is_fallback=true).")
    for r in rules:
        if not r.get("owner_user_id"):
            raise err(422, "missing_field", "Every rule needs an owner_user_id.")
        if not r.get("is_fallback") and r.get("match_field") not in ("region", "purpose", "language", "topic"):
            raise err(422, "invalid_match_field",
                     "match_field must be one of region, purpose, language, topic (unless is_fallback).")

    c = con()
    try:
        before = [dict(r) for r in c.execute("SELECT * FROM assignment_rules")]
        c.execute("DELETE FROM assignment_rules")
        # sort by whatever priority the caller supplied, falling back to
        # submission order (index) for rows that omitted it — never a
        # closure over the enumerate() variable itself.
        ordered = sorted(enumerate(rules), key=lambda pair: pair[1].get("priority", pair[0]))
        for i, r in ordered:
            c.execute(
                # `active` is a real boolean on Postgres: a literal 1 raises
                # "column is of type boolean but expression is of type integer".
                # SQLite accepts either, so this only ever failed on the deploy
                # target. Same class of bug as the `= 1` comparisons fixed in
                # db.py's notes — write TRUE/FALSE, never 1/0.
                """INSERT INTO assignment_rules (priority, match_field, match_value, owner_user_id,
                                                 is_fallback, active) VALUES (?,?,?,?,?,TRUE)""",
                (r.get("priority", i + 1), None if r.get("is_fallback") else r.get("match_field"),
                 None if r.get("is_fallback") else r.get("match_value"),
                 r["owner_user_id"], bool(r.get("is_fallback"))))
        c.commit()
        auth.audit(c, user, "replace", "assignment_rules", None, before=before, after=rules)
        rows = c.execute("SELECT * FROM assignment_rules WHERE active = TRUE ORDER BY priority").fetchall()
        data = [_assignment_rule_row(c, r) for r in rows]
        return {"data": data, "page": 1, "page_size": max(len(data), 1), "total": len(data)}
    finally:
        c.close()


@app.get("/api/calendar")
def calendar_week(user: auth.CurrentUser = Depends(auth.current_user),
                  user_param: Optional[str] = Query(None, alias="user"), week: Optional[str] = None):
    auth.require(user, "assignment.manage")
    c = con()
    try:
        target = user_param or user.id
        target_row = c.execute("SELECT id, display_name, office FROM app_user WHERE id=?",
                               (target,)).fetchone()
        if not target_row:
            raise err(404, "no_such_user", "user does not match a known app_user.")
        week_start, week_end = _week_bounds(week)
        # Timezone fix (FABLE-AUDIT.md) — a rep's week is anchored to THEIR
        # OWN office (never the viewer's), so a Dubai manager looking at a
        # Tokyo rep's calendar still sees Monday–Sunday as that rep's Tokyo
        # week, not shifted by the manager's own timezone.
        office_zone = _office_zone(target_row["office"])
        week_start_utc, week_end_utc = _week_bounds_utc(week_start, week_end, office_zone)
        rows = c.execute(
            """SELECT ce.*, l.name AS lead_name FROM calendar_events ce
                 LEFT JOIN leads l ON l.id = ce.lead_id
                WHERE ce.owner_user_id=? AND ce.starts_at >= ? AND ce.starts_at < ?
                ORDER BY ce.starts_at""",
            (target, _utc_iso(week_start_utc), _utc_iso(week_end_utc))).fetchall()
        events = [{"id": r["id"], "lead_id": r["lead_id"], "lead_name": r["lead_name"], "type": r["type"],
                  "title": r["title"], "starts_at": r["starts_at"], "ends_at": r["ends_at"],
                  "status": r["status"]} for r in rows]
        return {
            "user_id": target, "user_name": target_row["display_name"],
            "week_start": week_start.date().isoformat(),
            "week_end": (week_end - timedelta(days=1)).date().isoformat(),
            "events": events,
            "google_calendar_sync": {
                "configured": False,
                "reason": "No Google Calendar connection exists. This view reads bookings from our own database only.",
                "manual_path": "Check Google Calendar separately for anything booked outside Exceed Box.",
            },
        }
    finally:
        c.close()


# ══ §6 · card scan ════════════════════════════════════════════════════════════

@app.post("/api/leads/scan")
async def scan_lead(user: auth.CurrentUser = Depends(auth.current_user),
                    image: UploadFile = File(...), name: str = Form(...),
                    name_kana: Optional[str] = Form(None), company: Optional[str] = Form(None),
                    title: Optional[str] = Form(None), email: Optional[str] = Form(None),
                    phone: Optional[str] = Form(None), address: Optional[str] = Form(None),
                    consent_given: bool = Form(False)):
    """SPEC-V2 §6 — the showroom iPad flow (D11b#3). No OCR provider is
    wired: every field above is exactly what the person at the capture
    screen typed, never read from the image. Runs the same dedupe/merge path
    every other channel uses, and — if `consent_given` — records the one
    moment a timestamped, explicit consent can genuinely be captured."""
    if not name or not name.strip():
        raise err(422, "missing_field", "name is required.")
    data = await image.read()
    if not data:
        raise err(422, "missing_image", "An image is required to scan a business card.")

    c = con()
    try:
        rel_path = media.save("business_cards", image.filename or "card.jpg", data)
        res = ingest.upsert_lead(c, name=name.strip(), channel="business_card", company=company,
                                 title=title, email=email, phone=phone,
                                 note=("address: %s" % address) if address else None)
        lead_id = res["lead_id"]
        c.execute("UPDATE leads SET card_image_path=?, updated_at=datetime('now') WHERE id=?",
                 (rel_path, lead_id))
        if name_kana:
            c.execute("UPDATE leads SET name_kana=? WHERE id=?", (name_kana, lead_id))
        staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
        c.execute("""UPDATE leads SET owner_user_id=COALESCE(owner_user_id,?),
                                      owner_id=COALESCE(owner_id,?) WHERE id=?""",
                 (user.id, staff_id, lead_id))
        if consent_given:
            ingest.set_consent_explicit(c, lead_id, via="business card scan — iPad consent checkbox")
        c.commit()
        auth.audit(c, user, "create", "lead", lead_id,
                  after={"channel": "business_card", "consent_given": bool(consent_given)})
        detail = _lead_detail(c, user, _lead_row(c, lead_id))
        detail["extraction"] = {
            "configured": False,
            "reason": "No OCR provider is wired — every field above was entered manually.",
            "manual_path": "Type the fields on the capture screen; nothing is auto-filled from the photo.",
        }
        return detail
    finally:
        c.close()


# ══ §7 · CSV / Excel import ═══════════════════════════════════════════════════

@app.post("/api/import/analyze")
async def import_analyze(user: auth.CurrentUser = Depends(auth.current_user), file: UploadFile = File(...),
                         sheet_name: Optional[str] = Form(None)):
    auth.require(user, "import.csv")
    data = await file.read()
    if not data:
        raise err(422, "empty_file", "The uploaded file is empty.")
    c = con()
    try:
        try:
            result = csvimport.analyze(c, filename=file.filename, data=data,
                                       imported_by_user_id=user.id, sheet_name=sheet_name)
        except ValueError as e:
            raise err(422, "unparseable_file", str(e))
        if result.get("needs_sheet_selection"):
            # Nothing was stashed and no row was parsed yet (D11: ask first) —
            # the audit trail picks up once the human's sheet choice actually
            # produces a real analyzed import, below.
            return result
        auth.audit(c, user, "analyze", "import", result["token"],
                  after={"filename": file.filename, "rows_seen": result["rows_seen"]})
        return result
    finally:
        c.close()


@app.post("/api/import/commit")
async def import_commit(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "import.csv")
    body = await req.json()
    token = body.get("token")
    mapping = body.get("mapping")
    if not token or not mapping:
        raise err(422, "missing_field", "token and mapping are required.")
    c = con()
    try:
        try:
            result = csvimport.commit(c, token=token, mapping=mapping, answers=body.get("answers") or {},
                                      committed_by_user_id=user.id, owner_user_id=body.get("owner_user_id"))
        except KeyError as e:
            raise err(404, "not_found", str(e))
        except ValueError as e:
            raise err(422, "invalid_commit", str(e))
        auth.audit(c, user, "commit", "import", result["import_id"], after=result)
        return result
    finally:
        c.close()


# ══ §8 · integrations ═════════════════════════════════════════════════════════

def _live_integrations(c) -> list:
    """The TRUE state of every integration, computed now — not read from a table
    someone has to remember to update.

    The `integrations` table was empty, so this screen showed nothing at all:
    not "disconnected", literally an empty list. A screen that says nothing is
    worse than one saying "not connected", because the reader concludes the
    feature does not exist rather than that it needs setting up.

    Keys match the contract the app already consumes (SPEC.md / test_contract).
    `what_is_needed` is never blank on a disconnected row: "not connected"
    without the next action is a dead end.

    Only things with an outside dependency appear here. The database and the
    auth mode are reported by /api/health instead — they are not integrations,
    and listing them would make "everything is disconnected" untrue for reasons
    that do not help anybody.
    """
    mail = mailer.describe()
    ai = replyai.describe()
    store = media.describe()

    def row(key, en, ja, connected, detail, needed, last_sync=None,
            count=None, read_only=False):
        return {"key": key, "label_en": en, "label_ja": ja,
                "connected": connected, "status_detail": detail,
                "what_is_needed": needed, "last_sync_at": last_sync,
                "record_count": count, "read_only": read_only,
                "updated_at": None}

    queued = failed = last = None
    try:
        queued = db.scalar(c, "SELECT count(*) FROM sends WHERE status='queued'")
        failed = db.scalar(c, "SELECT count(*) FROM sends WHERE status='failed'")
        lr = mailer.last_run(c, "mail_worker")
        last = str(lr["started_at"]) if lr else None
    except Exception:
        pass

    out = []

    if store["backend"] == "supabase":
        out.append(row("storage", "File storage", "ファイル保存", True,
                       "Private bucket %s." % store.get("bucket"), None))
    else:
        out.append(row("storage", "File storage", "ファイル保存", False,
                       "Card photos and voice notes are on this machine's disk.",
                       "Set SUPABASE_SECRET_KEY so uploads go to the private "
                       "storage bucket instead of a laptop."))

    if mail["delivers"] and mail["ready"]:
        out.append(row("email", "Email (SendGrid)", "メール配信", True,
                       "Sending as %s." % mail.get("from"), None,
                       last_sync=last, count=queued))
    elif mail["delivers"]:
        out.append(row("email", "Email (SendGrid)", "メール配信", False,
                       "Configured to send but incomplete: %s."
                       % "; ".join(mail["problems"]),
                       "Finish the SendGrid configuration before enabling it.",
                       last_sync=last, count=queued))
    else:
        out.append(row("email", "Email (SendGrid)", "メール配信", False,
                       "Dry run — messages are recorded and NOT delivered. "
                       "%s queued, %s failed." % (queued, failed),
                       "Create a SendGrid account, set SENDGRID_API_KEY and "
                       "EXCEEDBOX_MAIL_FROM, then EXCEEDBOX_MAIL_PROVIDER=sendgrid.",
                       last_sync=last, count=queued))

    out.append(row("worker", "Background worker", "バックグラウンド処理",
                   bool(last),
                   ("Last run %s." % last) if last else
                   "Never run — nothing is being sent and scores are not decaying.",
                   None if last else "Schedule scripts/worker.py every 5 minutes.",
                   last_sync=last))

    if ai["ready"]:
        out.append(row("ai", "Reply reading (AI)", "返信のAI判定", True,
                       "Model %s." % ai.get("model"), None))
    else:
        out.append(row("ai", "Reply reading (AI)", "返信のAI判定", False,
                       ai.get("note") or "; ".join(ai["problems"]) or "Off.",
                       "Set ANTHROPIC_API_KEY and EXCEEDBOX_AI_PROVIDER=anthropic."))

    topics = booking.public_meeting_types(c)
    out.append(row("booking", "Public booking page", "予約ページ", False,
                   ("%d topic(s) bookable at /book." % len(topics)) if topics
                   else "No meeting type has a salesperson assigned, so the "
                        "page offers nothing.",
                   None if topics else
                   "Assign at least one salesperson to a meeting type.",
                   count=len(topics)))

    out.append(row("google_calendar", "Google Calendar", "Googleカレンダー", False,
                   "Appointments live only in Exceed Box; no calendar is synced.",
                   "Needs a Google Workspace OAuth client, then one connection "
                   "per salesperson."))
    out.append(row("whatsapp", "WhatsApp", "WhatsApp", False,
                   "No WhatsApp inbox.",
                   "Needs a phone number Exceed owns plus Meta business "
                   "verification."))
    out.append(row("gohighlevel", "GoHighLevel", "GoHighLevel", False,
                   "No credentials; nothing is synced.",
                   "Future scope. Export from GoHighLevel and use the importer.",
                   read_only=True))
    p = pushmod.describe()
    out.append(row("push", "Push notifications", "プッシュ通知", p["ready"],
                   p.get("note") or "; ".join(p["problems"]) or
                   "Provider %s." % p["provider"],
                   None if p["ready"] else
                   "Set EXCEEDBOX_PUSH_PROVIDER=expo (and EXPO_ACCESS_TOKEN) once "
                   "a device build is registering tokens."))
    v = voice.describe()
    out.append(row("transcription", "Voice transcription", "音声の文字起こし",
                   v["ready"],
                   v.get("note") or "; ".join(v["problems"]) or
                   "Model %s." % v.get("model"),
                   None if v["ready"] else
                   "Set OPENAI_API_KEY and EXCEEDBOX_TRANSCRIBE_PROVIDER=whisper."))
    return out


@app.get("/api/integrations")
def list_integrations(user: auth.CurrentUser = Depends(auth.current_user)):
    auth.require(user, "integrations.view")
    c = con()
    try:
        rows = _live_integrations(c)
        return {"data": rows, "page": 1, "page_size": max(len(rows), 1),
                "total": len(rows)}
    finally:
        c.close()


@app.post("/api/integrations/gohighlevel/sync")
def sync_gohighlevel(user: auth.CurrentUser = Depends(auth.current_user)):
    """SPEC-V2 §8: 'Read-only until an audit and a conflict rule exist (D11)'
    — and no credentials exist yet at all. Never fakes a sync."""
    auth.require(user, "integrations.sync")
    c = con()
    try:
        row = c.execute("SELECT * FROM integrations WHERE key='gohighlevel'").fetchone()
        result = {
            "configured": False,
            "reason": "No GoHighLevel credentials are configured in this environment.",
            "manual_path": ("Export leads from GoHighLevel manually and use POST /api/import/analyze "
                            "until an audit and a conflict rule exist (D11) — this integration is "
                            "read-only by design even once connected."),
            "read_only": True,
            "last_sync_at": row["last_sync_at"] if row else None,
        }
        auth.audit(c, user, "sync_attempt", "integration", "gohighlevel", after=result)
        return result
    finally:
        c.close()


# ══ §9 · AI reply drafting ════════════════════════════════════════════════════

def _reply_row(c, reply_id: int):
    return c.execute("SELECT * FROM replies WHERE id=?", (reply_id,)).fetchone()


def _reply_out(c, r) -> dict:
    return {
        "id": r["id"], "lead_id": r["lead_id"], "channel": r["channel"],
        "subject": r["subject"], "body": r["body"], "received_at": r["received_at"], "status": r["status"],
        "verdict": json.loads(r["verdict_json"]) if r["verdict_json"] else None,
        "human_reply_body": r["human_reply_body"],
        "sent_at": r["sent_at"], "sent_by_name": _owner_name(c, r["sent_by_user_id"]),
        "voided_at": r["voided_at"],
    }


@app.get("/api/leads/{lead_id}/replies")
def lead_replies(lead_id: int, user: auth.CurrentUser = Depends(auth.current_user),
                 page: int = 1, page_size: int = 20):
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_view_lead(user, lead):
            raise err(403, "forbidden", "You do not have access to this lead.")
        rows = c.execute("SELECT * FROM replies WHERE lead_id=? ORDER BY received_at DESC",
                        (lead_id,)).fetchall()
        return paginate([_reply_out(c, r) for r in rows], page, page_size)
    finally:
        c.close()


@app.post("/api/replies/{reply_id}/draft")
def draft_reply(reply_id: int, user: auth.CurrentUser = Depends(auth.current_user)):
    """SPEC-V2 §9: 'No model is wired' — never a fabricated draft."""
    c = con()
    try:
        r = _reply_row(c, reply_id)
        if not r:
            raise err(404, "not_found", "No such reply.")
        lead = _lead_row(c, r["lead_id"])
        if not _can_edit_lead(user, lead):
            raise err(403, "forbidden", "You may not draft a reply on this lead.")
        return {
            "configured": False,
            "reason": "No AI model is wired to draft replies in this environment.",
            "manual_path": "Type the reply yourself and submit it with POST /api/replies/{id}/send.",
        }
    finally:
        c.close()


@app.post("/api/replies/{reply_id}/send")
async def send_reply(reply_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    """action='send' (default): a human types the reply and answers the
    four-question classifier (D7) themselves — no AI reads replies here — and
    it is scored exactly as an AI verdict would be, tagged source='human'.
    Gated with the SAME capability as the manual +30 (leads.signal.*) because
    a send can award it: marketing must never grant it, matching SPEC.md's
    'grant the manual +30' matrix row exactly.
    action='not_real': the one-tap 'this wasn't real' (D7) — voids exactly
    the events this reply created, never a blind sweep of the lead."""
    body = await req.json()
    action = body.get("action", "send")
    c = con()
    try:
        r = _reply_row(c, reply_id)
        if not r:
            raise err(404, "not_found", "No such reply.")
        lead = _lead_row(c, r["lead_id"])
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_signal_lead(user, lead):
            raise err(403, "forbidden",
                     "You may not approve a reply that can award the manual +30 on this lead.")

        if action == "not_real":
            if r["status"] != "sent":
                raise err(422, "not_sent", "Only a sent reply can be marked 'not real'.")
            staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
            for event_id in json.loads(r["scored_event_ids_json"] or "[]"):
                scoring.void(c, event_id, staff_id)
            c.execute("""UPDATE replies SET status='voided', voided_at=datetime('now'),
                                            voided_by_user_id=? WHERE id=?""", (user.id, reply_id))
            c.commit()
            auth.audit(c, user, "void", "reply", reply_id, before={"status": r["status"]},
                      after={"status": "voided"})
            return _reply_out(c, _reply_row(c, reply_id))

        if action != "send":
            raise err(422, "invalid_action", "action must be 'send' or 'not_real'.")
        if r["status"] != "received":
            raise err(422, "already_actioned", "This reply has already been sent or voided.")
        text = body.get("text")
        if not text:
            raise err(422, "missing_field", "text is required to approve and send a reply.")
        verdict = body.get("verdict") or {}

        staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
        outcome = tracking.record_human_approved_reply(c, r["lead_id"], subject=r["subject"] or "",
                                                        verdict=verdict, set_by=staff_id)
        c.execute(
            """UPDATE replies SET status='sent', human_reply_body=?, verdict_json=?,
                                  scored_event_ids_json=?, sent_at=datetime('now'), sent_by_user_id=?
                WHERE id=?""",
            (text, json.dumps(verdict), json.dumps(outcome.get("event_ids", [])), user.id, reply_id))
        c.commit()
        auth.audit(c, user, "send", "reply", reply_id, after={"verdict": verdict, "outcome": outcome})
        out = _reply_out(c, _reply_row(c, reply_id))
        out["scoring_outcome"] = outcome
        return out
    finally:
        c.close()


# ══ §10a · voice notes ════════════════════════════════════════════════════════

def _transcription_state(r) -> dict:
    v = voice.describe()
    if r["transcript"]:
        return {"configured": True, "reason": None, "manual_path": None}
    if not v["enabled"]:
        return {"configured": False,
                "reason": "Transcription is switched off; the memo is stored and "
                          "playable.",
                "manual_path": "Play the audio directly."}
    if not v["ready"]:
        return {"configured": False, "reason": "; ".join(v["problems"]),
                "manual_path": "Play the audio directly."}
    return {"configured": True,
            "reason": "Not transcribed — the provider did not return text.",
            "manual_path": "Play the audio directly."}


def _note_out(c, r) -> dict:
    return {
        "id": r["id"], "lead_id": r["lead_id"],
        "author_user_id": r["author_user_id"], "author_name": _owner_name(c, r["author_user_id"]),
        "audio_path": r["audio_path"], "duration_seconds": r["duration_seconds"],
        "transcript": r["transcript"],
        # The true state, per note. A memo recorded before a provider was
        # configured stays honestly untranscribed rather than looking like a
        # silent recording.
        "transcription": _transcription_state(r),
        "created_at": r["created_at"],
    }


@app.post("/api/leads/{lead_id}/notes")
async def add_voice_note(lead_id: int, user: auth.CurrentUser = Depends(auth.current_user),
                         audio: UploadFile = File(...), duration_seconds: Optional[float] = Form(None)):
    data = await audio.read()
    if not data:
        raise err(422, "empty_audio", "The uploaded audio file is empty.")
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_edit_lead(user, lead):
            raise err(403, "forbidden", "You may not add a note to this lead.")
        rel_path = media.save("voice_notes", audio.filename or "note.m4a", data)
        # Transcribe inline. The recordings are seconds long, and a rep who
        # taps stop expects to see the words — a background job would mean the
        # note is blank exactly when they look at it. A provider failure never
        # loses the memo: the audio is already stored, and the note is created
        # either way with an honest "not transcribed".
        transcript = None
        try:
            transcript = voice.transcribe(
                data, filename=audio.filename or "note.m4a")["text"]
        except voice.TranscribeUnavailable as e:
            log.info("voice note %s not transcribed: %s", rel_path, e)
        except Exception:
            log.exception("unexpected transcription failure for %s", rel_path)

        cur = c.execute(
            "INSERT INTO lead_notes (lead_id, author_user_id, audio_path,"
            " duration_seconds, transcript) VALUES (?,?,?,?,?)",
            (lead_id, user.id, rel_path, duration_seconds, transcript))
        c.commit()
        note_id = cur.lastrowid
        auth.audit(c, user, "create", "lead_note", note_id, after={"lead_id": lead_id})
        row = c.execute("SELECT * FROM lead_notes WHERE id=?", (note_id,)).fetchone()
        return _note_out(c, row)
    finally:
        c.close()


@app.get("/api/leads/{lead_id}/notes")
def list_voice_notes(lead_id: int, user: auth.CurrentUser = Depends(auth.current_user),
                     page: int = 1, page_size: int = 20):
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_view_lead(user, lead):
            raise err(403, "forbidden", "You do not have access to this lead.")
        rows = c.execute("SELECT * FROM lead_notes WHERE lead_id=? ORDER BY created_at DESC",
                        (lead_id,)).fetchall()
        return paginate([_note_out(c, r) for r in rows], page, page_size)
    finally:
        c.close()


# ══ §10b · push notifications ═════════════════════════════════════════════════

PUSH_PROVIDER_CONFIGURED = bool(os.environ.get("EXCEEDBOX_PUSH_PROVIDER"))
PUSH_SETTINGS_FIELDS = ("notify_new_lead", "notify_hot_lead", "notify_reply", "notify_task_escalation_level")


@app.post("/api/push/register")
async def register_push(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await req.json()
    token, platform = body.get("token"), body.get("platform")
    if not token or platform not in ("ios", "ipados", "android", "web"):
        raise err(422, "missing_field", "token and a valid platform are required.")
    c = con()
    try:
        c.execute("INSERT OR IGNORE INTO push_tokens (user_id, token, platform) VALUES (?,?,?)",
                 (user.id, token, platform))
        c.commit()
        auth.audit(c, user, "register", "push_token", token, after={"platform": platform})
        return {"registered": True, "platform": platform}
    finally:
        c.close()


def _push_settings_out(row) -> dict:
    return {
        "notify_new_lead": bool(row["notify_new_lead"]), "notify_hot_lead": bool(row["notify_hot_lead"]),
        "notify_reply": bool(row["notify_reply"]),
        "notify_task_escalation_level": row["notify_task_escalation_level"],
    }


@app.get("/api/push/settings")
def get_push_settings(user: auth.CurrentUser = Depends(auth.current_user)):
    c = con()
    try:
        row = c.execute("SELECT * FROM push_settings WHERE user_id=?", (user.id,)).fetchone()
        if not row:
            c.execute("INSERT INTO push_settings (user_id) VALUES (?)", (user.id,))
            c.commit()
            row = c.execute("SELECT * FROM push_settings WHERE user_id=?", (user.id,)).fetchone()
        return _push_settings_out(row)
    finally:
        c.close()


@app.patch("/api/push/settings")
async def patch_push_settings(req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await req.json()
    unknown = set(body) - set(PUSH_SETTINGS_FIELDS)
    if unknown:
        raise err(422, "not_editable", "These fields cannot be edited: %s" % sorted(unknown))
    c = con()
    try:
        if not c.execute("SELECT 1 FROM push_settings WHERE user_id=?", (user.id,)).fetchone():
            c.execute("INSERT INTO push_settings (user_id) VALUES (?)", (user.id,))
        fields = {k: bool(v) if k != "notify_task_escalation_level" else v
                 for k, v in body.items() if k in PUSH_SETTINGS_FIELDS}
        if fields:
            set_sql = ", ".join("%s=?" % k for k in fields)
            c.execute("UPDATE push_settings SET %s, updated_at=datetime('now') WHERE user_id=?" % set_sql,
                     list(fields.values()) + [user.id])
        c.commit()
        auth.audit(c, user, "update", "push_settings", user.id, after=body)
        row = c.execute("SELECT * FROM push_settings WHERE user_id=?", (user.id,)).fetchone()
        return _push_settings_out(row)
    finally:
        c.close()


@app.post("/api/push/test")
def send_test_push(user: auth.CurrentUser = Depends(auth.current_user)):
    c = con()
    try:
        token_row = c.execute("SELECT 1 FROM push_tokens WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                             (user.id,)).fetchone()
        if not token_row:
            raise err(422, "no_token", "Register a push token first (POST /api/push/register).")
        # Actually attempt it, and report what the provider said. The previous
        # version accepted the token, wrote an audit row and returned success
        # without ever contacting anything — the exact "green tick it has not
        # earned" this whole pass is about.
        report = pushmod.send(
            c, user_id=user.id, title="Exceed Box",
            body="Test notification — if you can read this, push is working.",
            data={"kind": "test"})
        state = pushmod.describe()
        result = {
            "configured": state["ready"], "sent": bool(report["sent"]),
            "reason": report.get("reason") or (
                "Delivered to %d device(s)." % report["sent"] if report["sent"]
                else "The provider accepted nothing."),
            "manual_path": ("Set EXCEEDBOX_PUSH_PROVIDER=expo once a device build "
                            "is registering tokens; until then, check the app "
                            "directly." if not state["ready"] else None),
        }
        auth.audit(c, user, "test", "push", user.id, after=result)
        return result
    finally:
        c.close()


# ══ §10c · lead drawer actions ════════════════════════════════════════════════

def _calendar_event_out(c, r) -> dict:
    lead = _lead_row(c, r["lead_id"]) if r["lead_id"] else None
    return {
        "id": r["id"], "lead_id": r["lead_id"], "lead_name": lead["name"] if lead else None,
        "owner_user_id": r["owner_user_id"], "owner_name": _owner_name(c, r["owner_user_id"]),
        "type": r["type"], "title": r["title"], "starts_at": r["starts_at"], "ends_at": r["ends_at"],
        "status": r["status"], "created_at": r["created_at"],
    }


@app.post("/api/leads/{lead_id}/notify-rep")
async def notify_rep(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await _optional_json(req)
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_edit_lead(user, lead):
            raise err(403, "forbidden", "You may not act on this lead.")
        target_user_id = body.get("target_user_id") or lead["owner_user_id"] or user.id
        target_row = c.execute("SELECT display_name FROM app_user WHERE id=?", (target_user_id,)).fetchone()
        if not target_row:
            raise err(404, "no_such_user", "target_user_id does not match a user.")
        owner_staff_id = auth.ensure_staff_bridge(c, target_user_id, target_row["display_name"])
        note = body.get("note") or ("Notify: %s" % scoring.explain(c, lead_id)["summary"])
        # Timezone fix (FABLE-AUDIT.md) — server-generated default, now
        # timezone-aware UTC with an explicit offset like every other due_at.
        task_id = tasks.create(c, lead_id, "call", reason=note, owner_id=owner_staff_id,
                               due_at=_utc_iso(_now_utc() + timedelta(hours=2)), created_by="human")
        has_token = bool(c.execute("SELECT 1 FROM push_tokens WHERE user_id=? LIMIT 1",
                                   (target_user_id,)).fetchone())
        push_attempt = {
            "configured": False,
            "reason": "No push credentials (APNs/FCM) are configured in this environment.",
            "manual_path": "A task was created instead — visible in the rep's Today list.",
            "recipient_has_token": has_token,
        }
        c.commit()
        auth.audit(c, user, "notify_rep", "lead", lead_id,
                  after={"task_id": task_id, "target_user_id": target_user_id})
        return {"task": tasks.get(c, task_id), "push_attempt": push_attempt}
    finally:
        c.close()


@app.post("/api/leads/{lead_id}/showroom-visit")
async def showroom_visit(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await _optional_json(req)
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_edit_lead(user, lead):
            raise err(403, "forbidden", "You may not act on this lead.")
        # Timezone fix (FABLE-AUDIT.md) — a client-supplied starts_at is
        # interpreted in THIS caller's own office, never blindly UTC/JST.
        starts_dt = (_parse_office_datetime(body["starts_at"], user.office) if body.get("starts_at")
                    else _now_utc() + timedelta(days=1))
        ends_dt = (_parse_office_datetime(body["ends_at"], user.office) if body.get("ends_at")
                  else starts_dt + timedelta(minutes=60))
        starts_at, ends_at = _utc_iso(starts_dt), _utc_iso(ends_dt)
        rep_id = body.get("rep_user_id") or lead["owner_user_id"] or user.id
        cur = c.execute(
            """INSERT INTO calendar_events (owner_user_id, lead_id, type, title, starts_at, ends_at,
                                            created_by_user_id) VALUES (?,?,?,?,?,?,?)""",
            (rep_id, lead_id, "showroom_visit", body.get("note") or "来店対応 / Showroom visit",
             starts_at, ends_at, user.id))
        c.commit()
        event_id = cur.lastrowid
        auth.audit(c, user, "showroom_visit", "lead", lead_id, after={"calendar_event_id": event_id})
        return _calendar_event_out(c, c.execute("SELECT * FROM calendar_events WHERE id=?",
                                                (event_id,)).fetchone())
    finally:
        c.close()


@app.post("/api/leads/{lead_id}/site-inspection")
async def site_inspection(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await _optional_json(req)
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_edit_lead(user, lead):
            raise err(403, "forbidden", "You may not act on this lead.")
        # Timezone fix (FABLE-AUDIT.md) — see showroom_visit's comment.
        starts_dt = (_parse_office_datetime(body["starts_at"], user.office) if body.get("starts_at")
                    else _now_utc() + timedelta(days=3))
        ends_dt = (_parse_office_datetime(body["ends_at"], user.office) if body.get("ends_at")
                  else starts_dt + timedelta(minutes=120))
        starts_at, ends_at = _utc_iso(starts_dt), _utc_iso(ends_dt)
        rep_id = body.get("rep_user_id") or lead["owner_user_id"] or user.id
        title = body.get("note") or ("視察を案内する / Site inspection%s" %
                                     ((" — %s" % body["region"]) if body.get("region") else ""))
        cur = c.execute(
            """INSERT INTO calendar_events (owner_user_id, lead_id, type, title, starts_at, ends_at,
                                            created_by_user_id) VALUES (?,?,?,?,?,?,?)""",
            (rep_id, lead_id, "site_inspection", title, starts_at, ends_at, user.id))
        c.commit()
        event_id = cur.lastrowid
        auth.audit(c, user, "site_inspection", "lead", lead_id, after={"calendar_event_id": event_id})
        return _calendar_event_out(c, c.execute("SELECT * FROM calendar_events WHERE id=?",
                                                (event_id,)).fetchone())
    finally:
        c.close()


@app.post("/api/leads/{lead_id}/set-contact-date")
async def set_contact_date(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await _optional_json(req)
    date = body.get("date")
    if not date:
        raise err(422, "missing_field", "date is required.")
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_edit_lead(user, lead):
            raise err(403, "forbidden", "You may not act on this lead.")
        # Timezone fix (FABLE-AUDIT.md) — `date` is a calendar DATE
        # ("2026-09-01"), not a clock time, so it is left exactly as typed
        # for revisit_at; but the TASK created from it carries a due_at, a
        # real instant, so that copy is normalised through the caller's own
        # office timezone the same way every other due_at now is.
        due = _utc_iso(_parse_office_datetime(date, user.office)) if _is_datetime_like(date) else date
        before = {"revisit_at": lead["revisit_at"]}
        c.execute("UPDATE leads SET revisit_at=?, updated_at=datetime('now') WHERE id=?", (date, lead_id))
        staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
        owner_staff_id = lead["owner_id"] or staff_id
        task_id = tasks.create(c, lead_id, "set_next_date",
                               reason=body.get("note") or "連絡日を設定 / Contact date set",
                               owner_id=owner_staff_id, due_at=due, created_by="human")
        c.commit()
        auth.audit(c, user, "set_contact_date", "lead", lead_id, before=before, after={"revisit_at": date})
        detail = _lead_detail(c, user, _lead_row(c, lead_id))
        detail["task"] = tasks.get(c, task_id)
        return detail
    finally:
        c.close()


@app.post("/api/leads/{lead_id}/follow")
async def follow_lead(lead_id: int, req: Request, user: auth.CurrentUser = Depends(auth.current_user)):
    body = await _optional_json(req)
    c = con()
    try:
        lead = _lead_row(c, lead_id)
        if not lead:
            raise err(404, "not_found", "No such lead.")
        if not _can_edit_lead(user, lead):
            raise err(403, "forbidden", "You may not act on this lead.")
        staff_id = user.staff_id or auth.ensure_staff_bridge(c, user.id, user.display_name)
        owner_staff_id = lead["owner_id"] or staff_id
        # Timezone fix (FABLE-AUDIT.md) — see create_task's comment.
        due = (_utc_iso(_parse_office_datetime(body["due_at"], user.office)) if body.get("due_at")
              else _utc_iso(_now_utc() + timedelta(days=3)))
        task_id = tasks.create(c, lead_id, "follow_up", reason=body.get("note") or "フォローする / Follow up",
                               owner_id=owner_staff_id, due_at=due, created_by="human")
        c.commit()
        auth.audit(c, user, "follow", "lead", lead_id, after={"task_id": task_id})
        return {"task": tasks.get(c, task_id)}
    finally:
        c.close()


# ══ §10d · uploaded media ═════════════════════════════════════════════════════

@app.get("/api/media/{category}/{filename}")
def get_media(category: str, filename: str, user: auth.CurrentUser = Depends(auth.current_user)):
    """C1 (FABLE-AUDIT.md, CRITICAL) — this used to gate on nothing but
    'any active login', full stop. Verified live: marketing and a
    non-owner sales rep both got HTTP 200 on a business-card image — a
    single file carrying name+company+phone+email+address — for a lead
    neither of them owned. That fully defeated SPEC.md rule 2 (contact
    redaction) and rule 1 (sales SQL isolation) for every card/voice-note.

    Fix: resolve the file back to the LEAD IT BELONGS TO and apply the
    exact same access rule the lead itself uses —
      business_cards → _can_see_contact() (same test '_lead_detail' uses to
                       redact card_image_path — a card image IS contact info)
      voice_notes    → _can_view_lead() (same test lead/task reads use)
    An unmapped or missing file is 404 (nothing to distinguish "wrong role"
    from "no such file" for a URL that was never valid). A real file the
    caller may not access is a real 403 — the SPEC.md convention this whole
    codebase uses for "you don't have access to this X" — never a silent
    200 and never an empty one."""
    rel_path = "%s/%s" % (category, filename)
    # Existence is checked through media.exists() rather than the filesystem so
    # this works identically on the local backend and on Supabase Storage.
    # Ownership is still decided below, from the database, never from the
    # storage layer — the bucket is private and knows nothing about roles.
    try:
        if not media.exists(rel_path):
            raise err(404, "not_found", "No such file.")
    except ValueError:
        raise err(404, "not_found", "No such file.")
    c = con()
    try:
        if category == "business_cards":
            lead = c.execute("SELECT * FROM leads WHERE card_image_path=?", (rel_path,)).fetchone()
            if not lead:
                raise err(404, "not_found", "No such file.")
            if not _can_see_contact(user, lead):
                raise err(403, "forbidden", "You do not have access to this business card image.")
        elif category == "voice_notes":
            note = c.execute("SELECT lead_id FROM lead_notes WHERE audio_path=?", (rel_path,)).fetchone()
            if not note:
                raise err(404, "not_found", "No such file.")
            lead = _lead_row(c, note["lead_id"])
            # N2 (FABLE-AUDIT-R2.md) — this used to gate on _can_view_lead
            # (leads.all), one tier weaker than business_cards. A voice memo
            # is a rep's spoken notes about a client — routinely the client's
            # own name/phone/email said out loud — so it belongs with
            # contact-class data, exactly like the card image: marketing has
            # leads.all but only consent.own, and could otherwise pull any
            # rep's memo on a lead whose contact fields are redacted from it
            # one field over. Same rule as business_cards, on purpose.
            if not lead or not _can_see_contact(user, lead):
                raise err(403, "forbidden", "You do not have access to this voice note.")
        elif category == "imports":
            # CSV/Excel uploads (SPEC-V2 §7) can hold up to ~25,000 rows of
            # exactly the same class of PII a card image does — the same
            # leak, at far larger scale, if this category were left gated on
            # nothing but "any login" the way business_cards used to be.
            # There is no single owning lead for an import file, so the
            # nearest equivalent access rule is the capability that gates
            # importing in the first place — but N7 (FABLE-AUDIT-R2.md)
            # flagged that `import.csv` alone is not enough once you notice
            # imports ARE persisted (csvimport.analyze() calls
            # media.save("imports", ...) and stores file_path — the filename
            # is just never returned to any client today): every marketing
            # or admin login shares that one capability, so without a
            # per-uploader check, any of them could read ANY import file —
            # a cross-lead PII dump at up to 25,000 rows — the moment a
            # filename leaked any other way (logs, a future feature). Gate
            # on uploader identity too, same principle as the lead-owner
            # checks above; admins (the only role with users.manage) can
            # see any import, matching their existing company-wide reach.
            auth.require(user, "import.csv")
            imp = c.execute("SELECT imported_by_user_id FROM imports WHERE file_path=?",
                            (rel_path,)).fetchone()
            if not imp:
                raise err(404, "not_found", "No such file.")
            if not (user.can("users.manage") or imp["imported_by_user_id"] == user.id):
                raise err(403, "forbidden", "You do not have access to this import file.")
        else:
            raise err(404, "not_found", "No such file.")

        # Served through the app, never by a public or signed storage URL: the
        # owner check above is the only thing standing between one rep and
        # another rep's customer photos, and a signed URL would outlive it.
        try:
            data = media.read(rel_path)
        except FileNotFoundError:
            raise err(404, "not_found", "No such file.")
        return Response(content=data, media_type=_media_type_for(filename),
                        headers={"Cache-Control": "private, max-age=0, no-store"})
    finally:
        c.close()


@app.get("/_status")
def viewer():
    """A read-only developer window onto the backend. It used to live at "/",
    which meant anyone opening exceedbox.app saw a status page instead of the
    product. The staff app owns "/" now; this moved aside."""
    return Response(
        content=(Path(__file__).parent / "viewer.html").read_text(encoding="utf-8"),
        media_type="text/html")


@app.get("/api/health")
def health():
    """Deliberately honest rather than reassuring.

    Every integration reports what it would actually do right now, so an
    Integrations screen can show "disconnected" instead of a green tick it has
    not earned. `mail.delivers=false` means the dry-run provider is live and no
    customer will receive anything, which is the correct default and must be
    visible rather than discovered.
    """
    c = con()
    try:
        last = None
        try:
            row = mailer.last_run(c, "mail_worker")
            if row:
                last = {"at": str(row["started_at"]), "ok": bool(row["ok"])}
        except Exception:
            last = None                       # pre-migration database
        queued = failed = None
        try:
            queued = db.scalar(c, "SELECT count(*) FROM sends WHERE status='queued'")
            failed = db.scalar(c, "SELECT count(*) FROM sends WHERE status='failed'")
        except Exception:
            pass
        return {
            "ok": True,
            "leads": db.scalar(c, "SELECT count(*) FROM leads"),
            "events": db.scalar(c, "SELECT count(*) FROM events"),
            "dialect": db.DIALECT,
            "auth": ("supabase-jwks" if auth.SUPABASE_JWKS_URL else
                     "supabase-hs256" if auth.SUPABASE_JWT_SECRET else
                     "dev" if auth.DEV_AUTH_ENABLED else "none"),
            "media": media.describe(),
            "mail": dict(mailer.describe(), queued=queued, failed=failed,
                         last_worker_run=last),
        }
    finally:
        c.close()



# ══ the staff web app ═════════════════════════════════════════════════════════
# The Expo web export is served from the same origin as the API, so there is no
# CORS to get wrong and no second hostname to maintain. This MUST stay the last
# route in the file: FastAPI matches in registration order, so every explicit
# route above (/api/*, /book, /u/*, /o/*, /c/*, /e, /webhooks/*, /_status) wins,
# and only what nothing else claimed reaches the catch-all.

WEBAPP_DIR = Path(os.environ.get("EXCEEDBOX_WEBAPP_DIR",
                                 Path(__file__).resolve().parent.parent / "webapp"))

# Paths the SPA must never swallow. They are already matched above; this list is
# the belt to that braces, so a typo like /api/lead (no s) returns a real 404
# JSON error instead of an HTML page that a client then fails to parse.
_NOT_THE_APP = ("api/", "_status", "book", "u/", "o/", "c/", "e", "webhooks/")


@app.get("/{full_path:path}", include_in_schema=False)
def staff_app(full_path: str):
    root = WEBAPP_DIR.resolve()
    index = root / "index.html"
    if not index.is_file():
        return JSONResponse(status_code=503, content={"error": {
            "code": "webapp_not_deployed",
            "message": "The staff app has not been built into this server."}})

    clean = full_path.strip("/")
    if clean == "e" or any(clean.startswith(p) for p in _NOT_THE_APP if p.endswith("/")):
        raise err(404, "not_found", "No such route.")

    if clean:
        candidate = (root / clean).resolve()
        # Path traversal guard: the resolved file must still be inside the
        # build directory. "../.env" resolves outside and falls through to the
        # index page — it never reads the file.
        if root in candidate.parents and candidate.is_file():
            hashed = "/_expo/static/" in "/" + clean
            return FileResponse(
                str(candidate),
                headers={"Cache-Control": "public, max-age=31536000, immutable"
                         if hashed else "public, max-age=300"})

    # Anything else is a client-side route (/leads/42, /today): hand back the
    # app and let it route. no-cache so a deploy is picked up immediately rather
    # than a week-old index pointing at bundles that no longer exist.
    return FileResponse(str(index), headers={"Cache-Control": "no-cache"})
