"""Tasks, escalation, and the accountability view.

D12  — a task is six fields, not a sentence: type, owner, due_at, state,
       created_by, reason. `reason` is mandatory; a rep who cannot see why
       will fall back on their gut, which is what the product exists to replace.
D12a — agents see only their own; managers see it broken down per person.
D12b — escalation ❗1 / ❗❗2 / ❗❗❗3.
       Balraj wanted lateness. I argued for score × lateness and he took it:
       a score-92 lead one day late matters more than a score-20 a week late.
       Escalating on time alone shouts about the wrong people.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import scoring

TASK_LABELS = {
    "call":              ("電話", "Call"),
    "email":             ("メール", "Email"),
    "send_material":     ("資料送付", "Send material"),
    "send_booking_link": ("予約リンク送付", "Send booking link"),
    "prepare":           ("商談準備", "Prepare for meeting"),
    "visit":             ("来店・視察対応", "Visit / site tour"),
    "follow_up":         ("フォロー", "Follow up"),
    "set_next_date":     ("次回連絡日設定", "Set next contact date"),
}


def create(con: sqlite3.Connection, lead_id: int, type_: str, *, reason: str,
           owner_id: int = None, due_at=None,
           created_by: str = "system") -> int:
    """due_at accepts either a datetime (internal/seed.py callers) or an
    ISO 8601 string (every real HTTP caller — the JSON body never carries a
    Python datetime). Both end up stored the same way. A caller that sent
    only a stray timezone-less string still ends up NOT NULL and parseable
    by the rest of the codebase's `datetime.fromisoformat(...)` calls."""
    if not reason:
        raise ValueError("a task without a reason will not be trusted — see D12")
    if owner_id is None:
        owner_id = con.execute("SELECT owner_id FROM leads WHERE id=?",
                               (lead_id,)).fetchone()["owner_id"]
    due = due_at or (datetime.utcnow() + timedelta(days=1))
    due_str = due.isoformat(sep=" ", timespec="seconds") if isinstance(due, datetime) else str(due)
    cur = con.execute(
        """INSERT INTO tasks (lead_id, type, owner_id, due_at, created_by, reason)
           VALUES (?,?,?,?,?,?)""",
        (lead_id, type_, owner_id, due_str, created_by, reason))
    con.commit()
    return cur.lastrowid


def complete(con: sqlite3.Connection, task_id: int, staff_id: int,
             skipped: bool = False) -> None:
    con.execute(
        """UPDATE tasks SET state=?, done_at=datetime('now'), done_by=?
            WHERE id=? AND state='open'""",
        ("skipped" if skipped else "done", staff_id, task_id))
    con.commit()


# ── escalation (D12b) ────────────────────────────────────────────────────────

def urgency(days_overdue: float, score: int, threshold: int = 40) -> float:
    """Days late, weighted by how much the lead is worth.

    A lead at the notify threshold weighs 1×; a 92 weighs 2.3×. So a hot lead
    accumulates urgency more than twice as fast per day of neglect, which is
    the whole point of not escalating on time alone.
    """
    if days_overdue <= 0:
        return 0.0
    weight = max(1.0, score / float(threshold or 40))
    return days_overdue * weight


def escalation(days_overdue: float, score: int, threshold: int = 40) -> int:
    """0 = fine, 1 = ❗, 2 = ❗❗, 3 = ❗❗❗.

    Bands are deliberately coarse — a rep reading a list needs three levels,
    not a decimal. Two different urgencies can therefore share a level; use
    urgency() when you need to sort or compare precisely.
    """
    u = urgency(days_overdue, score, threshold)
    if u >= 7:
        return 3
    if u >= 3:
        return 2
    if u >= 1:
        return 1
    return 0


def _parse_due(due_at: str) -> datetime:
    """Timezone fix (FABLE-AUDIT.md) — due_at may be a naive string (seed.py
    and other internal callers, always UTC by this codebase's existing
    convention) or a timezone-aware ISO string with an explicit offset (real
    HTTP callers, since app/api.py now normalises every client-supplied
    due_at through the caller's own office timezone before storing it).
    Normalises either to a naive UTC datetime so every comparison in this
    module — which works in naive-UTC `now`, same as scoring.py — stays
    apples-to-apples regardless of which shape actually got written."""
    s = due_at.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _overdue_days(due_at: str, now: datetime) -> float:
    if not due_at:
        return 0.0
    due = _parse_due(due_at)
    return max(0.0, (now - due).total_seconds() / 86400.0)


def _owner_info(con: sqlite3.Connection, staff_id) -> tuple:
    """staff.id -> (app_user.id, display name) for the canonical Task shape.
    Falls back to the staff row's own name when no app_user is bridged yet
    (SPEC.md's app_user/staff bridge is best-effort, not guaranteed)."""
    if not staff_id:
        return None, None
    row = con.execute("SELECT id, display_name FROM app_user WHERE staff_id=?",
                      (staff_id,)).fetchone()
    if row:
        return row["id"], row["display_name"]
    srow = con.execute("SELECT name, name_en FROM staff WHERE id=?", (staff_id,)).fetchone()
    return None, (srow["name_en"] or srow["name"]) if srow else None


def _canonical_task(con: sqlite3.Connection, row, now: datetime, thr: int) -> dict:
    """SPEC.md/AUDIT.md canonical Task: {id, lead_name, lead_score,
    escalation_level, ...} — never {task_id, lead, score, escalation}.

    H1 (FABLE-AUDIT.md) — if the caller's own query already joined
    leads.score (the materialized column: for_owner()/get() below do; so
    does api.py's _lead_tasks_for()), read it straight off the row instead
    of calling scoring.score() again per task. Callers that pass a bare
    `tasks` row without that join still work — same scoring.score() call as
    before — this is a read-if-present optimisation, never a behaviour
    requirement on every caller.

    N3 (FABLE-AUDIT-R2.md) — reading the joined column raw had the exact
    same staleness problem LeadDetail vs list had: a Task's `lead_score`
    could disagree with explain(). scoring.fresh_score() self-heals it here
    too, bounded to the rows this caller actually returned, same as every
    other read path."""
    row_keys = row.keys()
    if "lead_score" in row_keys and row["lead_score"] is not None:
        score_updated_at = row["lead_score_updated_at"] if "lead_score_updated_at" in row_keys else None
        sc = scoring.fresh_score(con, row["lead_id"], score_updated_at, row["lead_score"], now, commit=False)
    else:
        sc = scoring.score(con, row["lead_id"], now)
    late = _overdue_days(row["due_at"], now)
    owner_user_id, owner_name = _owner_info(con, row["owner_id"])
    return {
        "id": row["id"],
        "type": row["type"],
        "label_en": TASK_LABELS[row["type"]][1],
        "label_ja": TASK_LABELS[row["type"]][0],
        "owner_user_id": owner_user_id,
        "owner_name": owner_name,
        "lead_id": row["lead_id"],
        "lead_name": row["name"],
        "company": row["company"],
        "lead_score": sc,
        "due_at": row["due_at"],
        "days_overdue": round(late, 1),
        "state": row["state"],
        "reason": row["reason"],
        "escalation_level": escalation(late, sc, thr),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def canonical(con: sqlite3.Connection, row, now: datetime = None) -> dict:
    """Public entry point for api.py: turn a raw `tasks` row (already joined
    with the lead's name/company, e.g. by api.py's own filtered query) into
    the canonical Task shape, without duplicating the field mapping."""
    now = now or datetime.utcnow()
    thr = scoring.threshold(con)
    out = _canonical_task(con, row, now, thr)
    con.commit()   # N3 — commits fresh_score()'s recompute, if any, for this one row
    return out


def for_owner(con: sqlite3.Connection, owner_id: int,
              now: datetime = None) -> list:
    """D12a — what one agent sees when they log in. Their own tasks only."""
    now = now or datetime.utcnow()
    thr = scoring.threshold(con)
    out = []
    rows = con.execute(
        """SELECT t.*, l.name, l.company, l.score AS lead_score,
                 l.score_updated_at AS lead_score_updated_at
             FROM tasks t JOIN leads l ON l.id = t.lead_id
            WHERE t.owner_id = ? AND t.state = 'open'
            ORDER BY t.due_at""", (owner_id,)).fetchall()
    for r in rows:
        out.append(_canonical_task(con, r, now, thr))
    con.commit()   # N3 — one commit for whatever fresh_score() recomputed above, not per-row
    out.sort(key=lambda t: (-t["escalation_level"], -t["lead_score"]))
    return out


def get(con: sqlite3.Connection, task_id: int, now: datetime = None) -> dict | None:
    """One task, canonical shape — used to return a fresh Task after a
    create/patch mutation instead of a bare {"id": ...} receipt."""
    now = now or datetime.utcnow()
    thr = scoring.threshold(con)
    row = con.execute(
        """SELECT t.*, l.name, l.company, l.score AS lead_score,
                 l.score_updated_at AS lead_score_updated_at
             FROM tasks t JOIN leads l ON l.id = t.lead_id
            WHERE t.id=?""", (task_id,)).fetchone()
    if not row:
        return None
    out = _canonical_task(con, row, now, thr)
    con.commit()   # N3 — commits fresh_score()'s recompute, if any, for this one row
    return out


def team_status(con: sqlite3.Connection, now: datetime = None) -> list:
    """D12a — what a manager or the CEO sees. One row per person.

    The row that matters most is the empty one. A rep with seven overdue tasks
    is visible and fixable; a rep with none may be doing nothing at all, and
    today's Exceed Box cannot tell those two apart. `idle` names it.
    """
    now = now or datetime.utcnow()
    today_end = now.replace(hour=23, minute=59, second=59)
    thr = scoring.threshold(con)
    rows = []
    staff = con.execute("SELECT * FROM staff WHERE active = TRUE ORDER BY id").fetchall()
    staff_ids = [s["id"] for s in staff]
    # One query per KIND of number, company-wide — not five per staffer. On
    # the production host each statement is a ~0.33 s round trip; /api/team ran 65 of
    # them (and computed all of it twice, for the digest). Grouped here, then
    # looked up per staffer in the loop, with the same numbers as before.
    open_tasks, lead_counts, bridges = {}, {}, {}
    if staff_ids:
        ph = ",".join("?" * len(staff_ids))
        for t in con.execute(
                """SELECT t.*, l.score AS lead_score FROM tasks t
                     JOIN leads l ON l.id = t.lead_id
                    WHERE t.owner_id IN (%s) AND t.state='open'""" % ph, staff_ids):
            open_tasks.setdefault(t["owner_id"], []).append(t)
        for r in con.execute(
                """SELECT owner_id,
                          count(*) AS owned,
                          sum(CASE WHEN stage IN ('meeting_booked','in_negotiation','won')
                                   THEN 1 ELSE 0 END) AS booked,
                          sum(CASE WHEN stage = 'won' THEN 1 ELSE 0 END) AS won
                     FROM leads WHERE owner_id IN (%s) AND merged_into IS NULL
                    GROUP BY owner_id""" % ph, staff_ids):
            lead_counts[r["owner_id"]] = r
        for b in con.execute(
                """SELECT id, email, display_name, role, office, is_active, staff_id
                     FROM app_user WHERE staff_id IN (%s)""" % ph, staff_ids):
            bridges.setdefault(b["staff_id"], b)
    for s in staff:
        # H1 (FABLE-AUDIT.md) — this used to be scoring.score() (≈3 queries)
        # PER OPEN TASK, for every staffer, every request: the worse of the
        # two hotspots the audit named. Joining leads.score (materialized)
        # turns that into zero extra queries — the JOIN already paid for it.
        tasks = open_tasks.get(s["id"], [])
        overdue = due_today = escalated = 0
        worst = 0
        for t in tasks:
            late = _overdue_days(t["due_at"], now)
            # N3 (FABLE-AUDIT-R2.md) — deliberately NOT self-healed here,
            # unlike every other score-serving read path in this file/
            # api.py. Every one of those is bounded to a page (a list page,
            # a pipeline column, one lead's own tasks, one owner's own open
            # tasks) — self-heal costs at most page_size extra recomputes.
            # team_status() is not paginated by design (a manager needs
            # every staffer's TRUE overdue/escalated counts, not a page of
            # them), so it must touch every open task company-wide; if a
            # long-idle stretch left many leads stale at once, self-healing
            # here would silently reintroduce the exact per-task
            # scoring.score() storm H1 removed. worst_escalation/
            # tasks_escalated are aggregate badges, not a literal `score`
            # field this endpoint promises equals explain() (N3's own
            # invariant is about endpoints that expose a score value) — kept
            # reasonably fresh by scripts/sweep_decay.py and by every other
            # read path self-healing the leads it actually returns, not by
            # a full scan here.
            sc = t["lead_score"]
            lvl = escalation(late, sc, thr)
            if lvl >= 1:
                escalated += 1
            if late > 0:
                overdue += 1
                worst = max(worst, lvl)
            elif t["due_at"] and _parse_due(t["due_at"]) <= today_end:
                due_today += 1
        counts = lead_counts.get(s["id"])
        # int(): Postgres returns sum() as numeric, which the adapter reads as float
        leads_owned = int(counts["owned"]) if counts else 0
        meetings_booked = int(counts["booked"] or 0) if counts else 0
        won = int(counts["won"] or 0) if counts else 0
        bridge = bridges.get(s["id"])
        rows.append({
            # legacy/staff-keyed fields — kept for tasks.py's own callers
            # (test_decisions.py exercises this function directly against a
            # bare `staff` table, with no app_user rows at all).
            "staff_id": s["id"], "name": s["name"], "name_en": s["name_en"],
            "role": s["role"], "open": len(tasks),
            "overdue": overdue, "due_today": due_today,
            "worst_escalation": worst,
            "leads_owned": leads_owned,
            # zero tasks against a real book of leads is the quiet failure
            "idle": len(tasks) == 0 and leads_owned > 0,
            # canonical TeamMember fields (SPEC.md/AUDIT.md) — additive, so
            # the API layer can present a real per-rep accountability row.
            "user_id": bridge["id"] if bridge else None,
            "email": bridge["email"] if bridge else None,
            "display_name": (bridge["display_name"] if bridge else None) or s["name_en"] or s["name"],
            "role_code": bridge["role"] if bridge else None,
            "office": bridge["office"] if bridge else None,
            "is_active": bool(bridge["is_active"]) if bridge else None,
            "tasks_open": len(tasks),
            "tasks_overdue": overdue,
            "tasks_escalated": escalated,
            "meetings_booked": meetings_booked,
            "won": won,
        })
    con.commit()   # N3 — one commit for whatever fresh_score() recomputed above, not per-row
    return rows


def escalations_digest(con: sqlite3.Connection, now: datetime = None, team: list = None) -> dict:
    """D12b — my flag, accepted: pinging the CEO on every level-2 gets muted
    within a week. This is the once-a-day digest instead."""
    now = now or datetime.utcnow()
    team = team if team is not None else team_status(con, now)
    flagged =[t for t in team if t["worst_escalation"] >= 2 or t["idle"]]
    return {
        "generated_at": now.isoformat(sep=" ", timespec="seconds"),
        "needs_attention": flagged,
        "quiet": [t for t in team if t not in flagged],
        "send": bool(flagged),          # nothing to say → say nothing
    }
