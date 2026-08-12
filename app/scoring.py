"""The scoring engine.

Implements the model agreed in Exceed-Box-Build-Decisions.md:

  D2/D5/D6/D4  the ten rules, grown from OneBox's nine by splitting
               「法人案件」 into corporate buyer and partnership
  D8           behaviour decays, facts do not
  D8           notify threshold, tunable, default 40

Two things this module does that the demo never could:

  score(lead_id)    the number, computed from events — never typed in
  explain(lead_id)  *why* the number is what it is

The second one is not decoration. A rep who cannot see why a lead is a 92 goes
back to their own judgement, which is the habit that killed Salesforce here.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

# ── decay curve (D8) ─────────────────────────────────────────────────────────
# Behaviour points halve at 60 days and reach zero at 120.
# Piecewise linear and continuous: 1.0 at day 0, 0.5 at day 60, 0.0 at day 120.
#
# NOTE ON INTERPRETATION — flagged for Balraj.
# "60 days of silence" could mean the age of each event, or the time since the
# lead last did anything. This implements PER-EVENT age. The alternative
# (lead-level silence) means one open in December would restore a June reply to
# full value, which is wrong. Say the word if you meant lead-level.
HALF_LIFE_DAYS = 60
ZERO_DAYS = 120

BEHAVIOUR = {"open", "click", "page_view", "booking_page_view", "reply"}
FACTS = {"booking_completed", "wants_meeting", "corporate_deal",
         "partnership", "high_budget"}
SCORED = BEHAVIOUR | FACTS


def decay_factor(age_days: float) -> float:
    if age_days <= 0:
        return 1.0
    if age_days >= ZERO_DAYS:
        return 0.0
    if age_days <= HALF_LIFE_DAYS:
        return 1.0 - 0.5 * (age_days / HALF_LIFE_DAYS)
    return 0.5 * (1.0 - (age_days - HALF_LIFE_DAYS) / (ZERO_DAYS - HALF_LIFE_DAYS))


def _rules(con: sqlite3.Connection) -> dict:
    return {r["event_kind"]: r for r in
            con.execute("SELECT * FROM scoring_rules WHERE active=1")}


def threshold(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT value FROM settings WHERE key='notify_threshold'").fetchone()
    return int(row["value"]) if row else 40


def explain(con: sqlite3.Connection, lead_id: int, now: datetime | None = None) -> dict:
    """Score plus the full breakdown of how it got there."""
    now = now or datetime.utcnow()
    rules = _rules(con)

    rows = con.execute(
        """SELECT kind, detail, occurred_at, source, set_by
             FROM events
            WHERE lead_id = ? AND voided_at IS NULL
            ORDER BY occurred_at""",
        (lead_id,),
    ).fetchall()

    components, total = [], 0.0
    for r in rows:
        rule = rules.get(r["kind"])
        if not rule or r["kind"] not in SCORED:
            continue
        occurred = datetime.fromisoformat(r["occurred_at"].replace("Z", ""))
        age = (now - occurred).total_seconds() / 86400.0
        base = float(rule["points"])
        if rule["decays"]:
            factor = decay_factor(age)
            kind_group = "behaviour"
        else:
            factor = 1.0
            kind_group = "fact"
        value = base * factor
        total += value
        components.append({
            "kind": r["kind"],
            "label_en": rule["label_en"],
            "label_ja": rule["label_ja"],
            "group": kind_group,
            "detail": r["detail"],
            "occurred_at": r["occurred_at"],
            "age_days": round(age, 1),
            "base_points": int(base),
            "decay_factor": round(factor, 3),
            "points": round(value, 1),
            "source": r["source"],
            "set_by": r["set_by"],
        })

    score = int(round(total))
    thr = threshold(con)
    return {
        "lead_id": lead_id,
        "score": score,
        "threshold": thr,
        "hot": score >= thr,
        "components": components,
        # the one-line "why" that goes next to a task (D12)
        "summary": _summary(components, score),
    }


def _summary(components: list, score: int) -> str:
    """Short human reason, biggest contributors first. Goes on the task."""
    if not components:
        return "No activity yet"
    top = sorted(components, key=lambda c: -c["points"])[:3]
    bits = [c["label_en"] for c in top if c["points"] > 0]
    return "Score {} · {}".format(score, " · ".join(bits)) if bits else "Score {}".format(score)


def score(con: sqlite3.Connection, lead_id: int, now: datetime | None = None) -> int:
    return explain(con, lead_id, now)["score"]


def heat(score_value: int) -> str:
    """The dot colour on the pill. Same bands as the demo."""
    if score_value >= 70:
        return "hot"
    if score_value >= 35:
        return "warm"
    return "cold"


def record(con: sqlite3.Connection, lead_id: int, kind: str, *, detail: str = None,
           send_id: str = None, source: str = "system", set_by: int = None,
           occurred_at: datetime | None = None) -> int:
    """Append one event. The only way anything is ever scored.

    D4: a human-granted +30 must be attributable, so set_by is stored and
    surfaced in explain(). Nothing is blocked — it is made visible.
    """
    ts = (occurred_at or datetime.utcnow()).isoformat(sep=" ", timespec="seconds")
    cur = con.execute(
        """INSERT INTO events (lead_id, kind, detail, send_id, source, set_by, occurred_at)
           VALUES (?,?,?,?,?,?,?)""",
        (lead_id, kind, detail, send_id, source, set_by, ts))
    con.commit()
    return cur.lastrowid


def void(con: sqlite3.Connection, event_id: int, staff_id: int) -> None:
    """D7: the rep's one-tap 'this wasn't real'. Removes the points without
    deleting the history — the event stays, marked."""
    con.execute("UPDATE events SET voided_at=datetime('now'), voided_by=? WHERE id=?",
                (staff_id, event_id))
    con.commit()


def crossed_threshold(con: sqlite3.Connection, lead_id: int,
                      before: int, after: int) -> bool:
    """True only on the transition, so a lead does not re-notify on every event."""
    thr = threshold(con)
    return before < thr <= after
