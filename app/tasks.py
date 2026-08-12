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
from datetime import datetime, timedelta

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
           owner_id: int = None, due_at: datetime = None,
           created_by: str = "system") -> int:
    if not reason:
        raise ValueError("a task without a reason will not be trusted — see D12")
    if owner_id is None:
        owner_id = con.execute("SELECT owner_id FROM leads WHERE id=?",
                               (lead_id,)).fetchone()["owner_id"]
    due = (due_at or (datetime.utcnow() + timedelta(days=1)))
    cur = con.execute(
        """INSERT INTO tasks (lead_id, type, owner_id, due_at, created_by, reason)
           VALUES (?,?,?,?,?,?)""",
        (lead_id, type_, owner_id, due.isoformat(sep=" ", timespec="seconds"),
         created_by, reason))
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


def _overdue_days(due_at: str, now: datetime) -> float:
    if not due_at:
        return 0.0
    due = datetime.fromisoformat(due_at.replace("Z", ""))
    return max(0.0, (now - due).total_seconds() / 86400.0)


def for_owner(con: sqlite3.Connection, owner_id: int,
              now: datetime = None) -> list:
    """D12a — what one agent sees when they log in. Their own tasks only."""
    now = now or datetime.utcnow()
    thr = scoring.threshold(con)
    out = []
    rows = con.execute(
        """SELECT t.*, l.name, l.company
             FROM tasks t JOIN leads l ON l.id = t.lead_id
            WHERE t.owner_id = ? AND t.state = 'open'
            ORDER BY t.due_at""", (owner_id,)).fetchall()
    for r in rows:
        sc = scoring.score(con, r["lead_id"], now)
        late = _overdue_days(r["due_at"], now)
        out.append({
            "task_id": r["id"], "lead_id": r["lead_id"],
            "lead": r["name"], "company": r["company"],
            "type": r["type"],
            "label_ja": TASK_LABELS[r["type"]][0],
            "label_en": TASK_LABELS[r["type"]][1],
            "due_at": r["due_at"], "days_overdue": round(late, 1),
            "score": sc,
            "escalation": escalation(late, sc, thr),
            "created_by": r["created_by"],
            "reason": r["reason"],
        })
    out.sort(key=lambda t: (-t["escalation"], -t["score"]))
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
    for s in con.execute("SELECT * FROM staff WHERE active=1 ORDER BY id"):
        tasks = con.execute(
            "SELECT * FROM tasks WHERE owner_id=? AND state='open'", (s["id"],)).fetchall()
        overdue = due_today = 0
        worst = 0
        for t in tasks:
            late = _overdue_days(t["due_at"], now)
            if late > 0:
                overdue += 1
                sc = scoring.score(con, t["lead_id"], now)
                worst = max(worst, escalation(late, sc, thr))
            elif t["due_at"] and datetime.fromisoformat(t["due_at"]) <= today_end:
                due_today += 1
        leads_owned = con.execute(
            "SELECT count(*) c FROM leads WHERE owner_id=? AND merged_into IS NULL",
            (s["id"],)).fetchone()["c"]
        rows.append({
            "staff_id": s["id"], "name": s["name"], "name_en": s["name_en"],
            "role": s["role"], "open": len(tasks),
            "overdue": overdue, "due_today": due_today,
            "worst_escalation": worst,
            "leads_owned": leads_owned,
            # zero tasks against a real book of leads is the quiet failure
            "idle": len(tasks) == 0 and leads_owned > 0,
        })
    return rows


def escalations_digest(con: sqlite3.Connection, now: datetime = None) -> dict:
    """D12b — my flag, accepted: pinging the CEO on every level-2 gets muted
    within a week. This is the once-a-day digest instead."""
    now = now or datetime.utcnow()
    team = team_status(con, now)
    flagged = [t for t in team if t["worst_escalation"] >= 2 or t["idle"]]
    return {
        "generated_at": now.isoformat(sep=" ", timespec="seconds"),
        "needs_attention": flagged,
        "quiet": [t for t in team if t not in flagged],
        "send": bool(flagged),          # nothing to say → say nothing
    }
