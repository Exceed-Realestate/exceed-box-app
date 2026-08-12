"""HTTP surface.

Two kinds of endpoint:

  public tracking   /o /c /e /webhooks/email
                    these are hit by mail clients, browsers and SendGrid.
                    No auth, by necessity — the send_id is the credential.

  internal JSON     /api/*
                    what the dashboard reads. No auth yet — this runs on
                    localhost only. Auth goes in before anything is deployed,
                    and that is stated in the README rather than left implied.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from . import db, ingest, scoring, tasks, tracking

app = FastAPI(title="Exceed Box API", version="0.1.0")


def con():
    return db.connect()


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


# ══ leads ═══════════════════════════════════════════════════════════════════

@app.get("/api/leads")
def list_leads(q: str = "", stage: str = "", region: str = "",
               purpose: str = "", limit: int = 50, offset: int = 0):
    c = con()
    try:
        sql = ["SELECT DISTINCT l.* FROM leads l"]
        args, where = [], ["l.merged_into IS NULL"]
        if region:
            sql.append("JOIN lead_regions r ON r.lead_id = l.id")
            where.append("r.region = ?")
            args.append(region)
        if q:
            where.append("(l.name LIKE ? OR l.company LIKE ?)")
            args += ["%%%s%%" % q, "%%%s%%" % q]
        if stage:
            where.append("l.stage = ?")
            args.append(stage)
        if purpose:
            where.append("l.purpose = ?")
            args.append(purpose)
        sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY l.id LIMIT ? OFFSET ?")
        args += [limit, offset]
        rows = c.execute(" ".join(sql), args).fetchall()
        out = []
        for r in rows:
            s = scoring.score(c, r["id"])
            out.append({
                "id": r["id"], "name": r["name"], "company": r["company"],
                "first_touch": r["first_touch"],
                "channels": [ch["channel"] for ch in ingest.channels(c, r["id"])],
                "purpose": r["purpose"], "relationship": r["relationship"],
                "stage": r["stage"], "score": s, "heat": scoring.heat(s),
                "activity_note": activity_note(c, r["id"]),   # D10
            })
        total = c.execute("SELECT count(*) c FROM leads WHERE merged_into IS NULL").fetchone()["c"]
        return {"total": total, "shown": len(out), "leads": out}
    finally:
        c.close()


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: int):
    c = con()
    try:
        r = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not r:
            return JSONResponse({"error": "not found"}, status_code=404)
        consent = c.execute("SELECT * FROM lead_consent WHERE lead_id=?", (lead_id,)).fetchone()
        return {
            "lead": dict(r),
            "identities": [dict(x) for x in c.execute(
                "SELECT kind,value,status FROM lead_identities WHERE lead_id=?", (lead_id,))],
            "channels": ingest.channels(c, lead_id),
            "regions": [dict(x) for x in c.execute(
                "SELECT * FROM lead_regions WHERE lead_id=?", (lead_id,))],
            "consent": dict(consent) if consent else None,
            "score": scoring.explain(c, lead_id),
            "tasks": [dict(x) for x in c.execute(
                "SELECT * FROM tasks WHERE lead_id=? ORDER BY due_at", (lead_id,))],
            "timeline": [dict(x) for x in c.execute(
                """SELECT kind, detail, source, occurred_at, voided_at
                     FROM events WHERE lead_id=? ORDER BY occurred_at""", (lead_id,))],
        }
    finally:
        c.close()


@app.get("/api/leads/{lead_id}/score")
def why_score(lead_id: int):
    c = con()
    try:
        return scoring.explain(c, lead_id)
    finally:
        c.close()


def activity_note(c, lead_id: int) -> str:
    """D10 — Balraj kept the free-text column but redefined it: not a status,
    a plain-English latest-activity line, written by the system from the event
    log and never typed by a human. That is what makes it safe."""
    r = c.execute(
        """SELECT kind, detail, occurred_at FROM events
            WHERE lead_id=? AND voided_at IS NULL
              AND kind NOT IN ('imported','merged','stage_change','sent')
            ORDER BY occurred_at DESC LIMIT 1""", (lead_id,)).fetchone()
    if not r:
        return "Imported, no activity yet"
    words = {
        "open": "Opened an email", "click": "Clicked a link",
        "page_view": "Viewed a page", "booking_page_view": "Viewed the booking page",
        "reply": "Replied", "booking_completed": "Booked a consultation",
        "wants_meeting": "Wants to meet in person", "corporate_deal": "Corporate deal",
        "partnership": "Partnership offer", "high_budget": "High budget",
        "bounce": "Email bounced", "unsubscribe": "Unsubscribed",
    }
    return words.get(r["kind"], r["kind"])


# ══ dashboard ═══════════════════════════════════════════════════════════════

@app.get("/api/dashboard")
def dashboard():
    c = con()
    try:
        def n(sql, args=()):
            return db.scalar(c, sql, args)

        live = "merged_into IS NULL"
        booked = n("SELECT count(*) FROM leads WHERE %s AND stage IN "
                   "('booked','negotiating','won')" % live)
        negotiating = n("SELECT count(*) FROM leads WHERE %s AND stage IN "
                        "('negotiating','won')" % live)
        won = n("SELECT count(*) FROM leads WHERE %s AND stage='won'" % live)

        # D16 — attribute each booking to ONE source (first touch) so the chart
        # totals the tile. The demo's chart summed to 55 against a tile of 30.
        by_source = [dict(r) for r in c.execute(
            """SELECT first_touch AS source, count(*) AS n FROM leads
                WHERE merged_into IS NULL AND stage IN ('booked','negotiating','won')
                GROUP BY first_touch ORDER BY n DESC""")]

        by_region = [dict(r) for r in c.execute(
            """SELECT r.region, count(DISTINCT l.id) AS n
                 FROM lead_regions r JOIN leads l ON l.id=r.lead_id
                WHERE l.merged_into IS NULL AND l.stage IN ('booked','negotiating','won')
                GROUP BY r.region""")]

        funnel = [
            {"stage": "sent",        "n": n("SELECT count(*) FROM sends")},
            {"stage": "opened",      "n": n("SELECT count(DISTINCT lead_id) FROM events WHERE kind='open' AND voided_at IS NULL")},
            {"stage": "clicked",     "n": n("SELECT count(DISTINCT lead_id) FROM events WHERE kind='click' AND voided_at IS NULL")},
            {"stage": "replied",     "n": n("SELECT count(DISTINCT lead_id) FROM events WHERE kind='reply' AND voided_at IS NULL")},
            {"stage": "booked",      "n": booked},
            {"stage": "negotiating", "n": negotiating},
            {"stage": "won",         "n": won},
        ]

        return {
            "kpis": {
                "total_leads": n("SELECT count(*) FROM leads WHERE %s" % live),
                "booked": booked, "negotiating": negotiating, "won": won,
                # D15 — expected revenue needs a stage-probability table.
                # Balraj has not supplied the percentages, so this reports null
                # rather than inventing a number that looks authoritative.
                "expected_revenue": None,
                "expected_revenue_blocked_on":
                    "stage close-probability percentages (see D15)",
            },
            "funnel": funnel,
            "by_source": by_source,
            "by_region": by_region,
            "source_total_matches_tile": sum(s["n"] for s in by_source) == booked,
            # D16 — the dashboard explains its own anomaly instead of assuming
            # a manager spots it
            "flags": _flags(booked, negotiating),
        }
    finally:
        c.close()


def _flags(booked: int, negotiating: int) -> list:
    out = []
    gap = booked - negotiating
    if gap > 0:
        out.append({
            "id": "booking_negotiation_gap",
            "level": "warn" if gap >= max(3, booked * 0.3) else "info",
            "title": "%d booked meetings never became a conversation" % gap,
            "detail": "%d consultations were booked and %d progressed to negotiation. "
                      "The gap is meetings that happened, or did not happen, and went "
                      "nowhere — no-shows, wrong fit, or nobody followed up. It is the "
                      "cheapest thing on this dashboard to fix." % (booked, negotiating),
        })
    return out


# ══ tasks / accountability ══════════════════════════════════════════════════

@app.get("/api/tasks")
def my_tasks(owner_id: int):
    c = con()
    try:
        return {"owner_id": owner_id, "tasks": tasks.for_owner(c, owner_id)}
    finally:
        c.close()


@app.post("/api/tasks/{task_id}/complete")
def finish_task(task_id: int, staff_id: int, skipped: bool = False):
    c = con()
    try:
        tasks.complete(c, task_id, staff_id, skipped)
        return {"ok": True}
    finally:
        c.close()


@app.get("/api/team")
def team():
    c = con()
    try:
        return {"team": tasks.team_status(c), "digest": tasks.escalations_digest(c)}
    finally:
        c.close()


# ══ intake ══════════════════════════════════════════════════════════════════

@app.post("/api/leads")
async def create_lead(req: Request):
    body = await req.json()
    c = con()
    try:
        return ingest.upsert_lead(
            c, name=body["name"], channel=body["channel"],
            company=body.get("company"), title=body.get("title"),
            email=body.get("email"), phone=body.get("phone"),
            note=body.get("note"))
    finally:
        c.close()


@app.post("/api/import/questions")
async def import_questions(req: Request):
    """D11 — the importer interrogates the file before writing a single row."""
    body = await req.json()
    headers = body.get("headers", [])
    return {
        "guessed_mapping": ingest.guess_mapping(headers),
        "questions": ingest.import_questions(headers, body.get("sample", [])),
    }


@app.post("/api/referrals")
async def add_referral(req: Request):
    """D11b — referrer recorded as a person, because three referrals makes
    somebody a partner, and partner is a 30-point rule."""
    body = await req.json()
    c = con()
    try:
        res = ingest.upsert_lead(c, name=body["name"], channel="referral",
                                 company=body.get("company"), email=body.get("email"),
                                 phone=body.get("phone"), note=body.get("note"))
        c.execute("""INSERT INTO referrals (lead_id, referrer_staff_id, referrer_lead_id, note)
                     VALUES (?,?,?,?)""",
                  (res["lead_id"], body.get("referrer_staff_id"),
                   body.get("referrer_lead_id"), body.get("note")))
        # three or more introductions and the referrer is a channel, not a contact
        if body.get("referrer_lead_id"):
            n = db.scalar(c, "SELECT count(*) FROM referrals WHERE referrer_lead_id=?",
                          (body["referrer_lead_id"],))
            if n >= 3:
                scoring.record(c, body["referrer_lead_id"], "partnership",
                               detail="referred %d people" % n, source="system")
                c.execute("UPDATE leads SET relationship='partner' WHERE id=?",
                          (body["referrer_lead_id"],))
        c.commit()
        return res
    finally:
        c.close()


@app.get("/api/health")
def health():
    c = con()
    try:
        return {"ok": True,
                "leads": db.scalar(c, "SELECT count(*) FROM leads"),
                "events": db.scalar(c, "SELECT count(*) FROM events")}
    finally:
        c.close()
