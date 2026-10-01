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

import os
import sqlite3
from datetime import datetime, timedelta

# ── decay curve (D8) ─────────────────────────────────────────────────────────
# Behaviour points halve at 60 days and reach zero at 120.
# Piecewise linear and continuous: 1.0 at day 0, 0.5 at day 60, 0.0 at day 120.
HALF_LIFE_DAYS = 60
ZERO_DAYS = 120

# H3 (FABLE-AUDIT.md) — "60 days of silence" (D8) is ambiguous and was never
# ratified: it could mean the age of EACH event, or the time since the LEAD
# last did anything. Both readings are implemented; this constant is the one
# place that chooses, so the choice can never drift silently between
# functions. Default is the ORIGINAL per-event behaviour — nothing changes
# for anyone until Balraj rules and this is flipped (or overridden via the
# env var for a side-by-side comparison without a code change).
#   per_event   (current/default) — each event's own age decays independently.
#               A June reply keeps fading even if the lead opened an email
#               in December.
#   lead_level  — decay is driven by time since the lead's MOST RECENT
#               behaviour event. One recent open revives every behaviour
#               point on the lead back toward full value.
DECAY_MODE_PER_EVENT = "per_event"
DECAY_MODE_LEAD_LEVEL = "lead_level"
DECAY_MODE = os.environ.get("EXCEEDBOX_DECAY_MODE", DECAY_MODE_PER_EVENT)
if DECAY_MODE not in (DECAY_MODE_PER_EVENT, DECAY_MODE_LEAD_LEVEL):
    raise RuntimeError("EXCEEDBOX_DECAY_MODE must be '%s' or '%s', got %r" %
                       (DECAY_MODE_PER_EVENT, DECAY_MODE_LEAD_LEVEL, DECAY_MODE))

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
            con.execute("SELECT * FROM scoring_rules WHERE active = TRUE")}


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

    # H3 / lead_level mode: decay is driven by time since the lead's most
    # recent behaviour event, not each event's own age. `rows` is ordered
    # oldest-first, so the last behaviour-kind row seen is the most recent.
    silence_days = None
    if DECAY_MODE == DECAY_MODE_LEAD_LEVEL:
        latest_behaviour_at = None
        for r in rows:
            if r["kind"] in BEHAVIOUR:
                latest_behaviour_at = r["occurred_at"]
        if latest_behaviour_at:
            latest = datetime.fromisoformat(latest_behaviour_at.replace("Z", ""))
            silence_days = (now - latest).total_seconds() / 86400.0

    components, total = [], 0.0
    # D8 lists facts as states a lead is in — "wants to meet +30", "corporate
    # buyer +30" — not as things that add up each time they are observed. A rep
    # who taps "wants to meet" twice, or marks it after the AI already read it
    # in a reply, must not turn one fact into a permanent +60 (seen live
    # 2026-09-14: a test lead at 60 from two taps 40 minutes apart). Each fact
    # kind scores once, from its earliest un-voided occurrence; the later
    # events stay in the timeline and audit log. Behaviour still stacks.
    facts_seen = set()
    for r in rows:
        rule = rules.get(r["kind"])
        if not rule or r["kind"] not in SCORED:
            continue
        if not rule["decays"]:
            if r["kind"] in facts_seen:
                continue
            facts_seen.add(r["kind"])
        occurred = datetime.fromisoformat(r["occurred_at"].replace("Z", ""))
        age = (now - occurred).total_seconds() / 86400.0
        base = float(rule["points"])
        if rule["decays"]:
            kind_group = "behaviour"
            decay_age = silence_days if (DECAY_MODE == DECAY_MODE_LEAD_LEVEL and
                                         silence_days is not None) else age
            factor = decay_factor(decay_age)
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
        # SPEC.md/AUDIT.md canonical name — the app reads `breakdown`, not
        # `components`. Kept as the same list of component dicts; only the
        # key it hangs off changed.
        "breakdown": components,
        "decay_note": _decay_note(components),
        # H3 — which interpretation produced this breakdown, always visible,
        # never silent. See the DECAY_MODE comment above.
        "decay_mode": DECAY_MODE,
        # the one-line "why" that goes next to a task (D12) — not part of the
        # declared score-explain contract, but tasks.create()/seed.py still
        # read this for the mandatory `reason` field, so it stays.
        "summary": _summary(components, score),
    }


def _decay_note(components: list) -> str:
    """The one-line explanation of *why the number moves on its own* (D8) —
    the piece `hot`/`threshold` alone cannot tell a rep."""
    behaviour = [c for c in components if c["group"] == "behaviour"]
    if not behaviour:
        return "This score is built entirely from permanent facts — nothing here decays."
    faded = [c for c in behaviour if c["decay_factor"] < 1.0]
    if not faded:
        return "All behaviour points are fresh — none have started to decay yet."
    return ("Behaviour points fade over time (half value at %d days, zero at %d) — "
            "%d of %d behaviour signals here have already started to fade." %
            (HALF_LIFE_DAYS, ZERO_DAYS, len(faded), len(behaviour)))


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
    # H1 (FABLE-AUDIT.md) — record() is the ONLY place an event is ever
    # written, so it is the one choke point where the materialized
    # leads.score column can be kept current without every caller
    # remembering to do it themselves. explain() stays the single source of
    # truth for the number; this just writes it down.
    recompute_lead(con, lead_id)
    return cur.lastrowid


def void(con: sqlite3.Connection, event_id: int, staff_id: int) -> None:
    """D7: the rep's one-tap 'this wasn't real'. Removes the points without
    deleting the history — the event stays, marked."""
    row = con.execute("SELECT lead_id FROM events WHERE id=?", (event_id,)).fetchone()
    con.execute("UPDATE events SET voided_at=datetime('now'), voided_by=? WHERE id=?",
                (staff_id, event_id))
    con.commit()
    if row:
        recompute_lead(con, row["lead_id"])   # H1 — same choke-point rule as record()


def crossed_threshold(con: sqlite3.Connection, lead_id: int,
                      before: int, after: int) -> bool:
    """True only on the transition, so a lead does not re-notify on every event."""
    thr = threshold(con)
    return before < thr <= after


# ── H1 (FABLE-AUDIT.md) — materialized score ─────────────────────────────────
# `_hot_leads`/`/api/leads`/`/api/pipeline`/tasks.team_status used to call
# explain()/score() per row on every request — ~3 queries per lead, so a
# dashboard load at 30k leads was ~10^5 SQLite round trips in a Python loop.
# leads.score/leads.score_updated_at are the materialized answer. explain()
# stays authoritative for the actual number (nothing about its math changed);
# these two functions are the only things that write the column, so the
# materialized value can never disagree with what explain() would compute at
# the moment it was last written — the gap is only ever "how long since the
# last write", never "wrong formula".

def recompute_lead(con: sqlite3.Connection, lead_id: int, now: datetime | None = None,
                   commit: bool = True) -> int:
    """Recompute one lead's score via explain() (unchanged authority) and
    persist it. Called automatically by record()/void() on every write, so a
    live lead's score column is always exactly what explain() would return
    right now. Also the choke point for N3's lazy self-heal on read
    (fresh_score(), below) and for sweep_decay()'s bulk pass — every path
    that ever writes leads.score goes through here, which is what makes it
    the one place a threshold crossing can be recorded (N4, below) without
    any caller having to remember to do it.

    What it does NOT do on its own is track pure time passing on a lead
    nobody has touched or read — that is sweep_decay()'s job (or the next
    self-healed read)."""
    row = con.execute("SELECT score FROM leads WHERE id=?", (lead_id,)).fetchone()
    before = row["score"] if row is not None else 0
    sc = explain(con, lead_id, now)["score"]
    ts = (now or datetime.utcnow()).isoformat(sep=" ", timespec="seconds")
    con.execute("UPDATE leads SET score=?, score_updated_at=? WHERE id=?", (sc, ts, lead_id))

    # N4 (FABLE-AUDIT-R2.md) — record the crossing, if this recompute caused
    # one, so /api/scoring/model can answer "crossed this week" from history
    # instead of re-scoring every live lead twice on every request.
    thr = threshold(con)
    was_over, is_over = before >= thr, sc >= thr
    if was_over != is_over:
        con.execute(
            """INSERT INTO score_threshold_crossings
                 (lead_id, crossed_at, direction, threshold, score_before, score_after)
               VALUES (?,?,?,?,?,?)""",
            (lead_id, ts, "up" if is_over else "down", thr, before, sc))

    if commit:
        con.commit()
    return sc


# ── N3 (FABLE-AUDIT-R2.md) — self-healing read path ──────────────────────────
# recompute_lead() keeps leads.score exact at write time, but pure time
# passing (D8 decay) only reconciles the column when sweep_decay() runs —
# and nothing in this repo schedules it. Meanwhile every read path
# (list/pipeline/hot-leads/team/tasks) was reading the stored column
# straight, while LeadDetail/why_score always called explain() live — so the
# SAME lead could show two different scores on two screens the moment any
# decay had elapsed with no sweep (demonstrated: stored 52 vs live 31 at
# +90d). fresh_score() is the one place every such read path now goes
# through instead of trusting `leads.score` blindly: if the column is older
# than SCORE_STALE_SECONDS it is recomputed right there — cheap and bounded
# by how many rows the caller is actually returning (a page), never a
# full-table scan — so what an endpoint returns can never disagree with
# scoring.explain() for that lead at the moment it was asked.
SCORE_STALE_SECONDS = int(os.environ.get("EXCEEDBOX_SCORE_STALE_SECONDS", "300"))


def is_stale(score_updated_at: str | None, now: datetime | None = None) -> bool:
    """True when a materialized score is old enough that pure time-based
    decay (not just missing events) could have moved the real number."""
    if not score_updated_at:
        return True
    now = now or datetime.utcnow()
    updated = datetime.fromisoformat(score_updated_at.replace("Z", ""))
    return (now - updated).total_seconds() > SCORE_STALE_SECONDS


# When true, a stale row found during a read is written back. Off by default so GET
# stays read-only; set EXCEEDBOX_READ_HEALS_PERSIST=1 if you would rather amortise
# the recompute onto readers and accept writes on GET.
READ_HEALS_PERSIST = os.getenv("EXCEEDBOX_READ_HEALS_PERSIST", "0") == "1"


def fresh_score(con: sqlite3.Connection, lead_id: int, score_updated_at: str | None,
                stored_score: int, now: datetime | None = None, commit: bool = True) -> int:
    """The self-healing read: returns the materialized score unchanged when
    it is still fresh (the common case — cheap, no extra work), or
    recomputes and persists it via recompute_lead() (same authority as
    explain(), so it is exact, not approximate) when it has gone stale.
    Callers serving a page of N rows may pass commit=False and commit once
    after the loop, so N stale rows cost one write, not N."""
    if not is_stale(score_updated_at, now):
        return stored_score
    if READ_HEALS_PERSIST:
        return recompute_lead(con, lead_id, now, commit=commit)
    # NEW-3: default is to compute in memory and NOT write. A GET that issues an
    # UPDATE (and can INSERT a threshold crossing) means read traffic contends for
    # the write lock, breaks against a read-only replica, and stamps decay crossings
    # at whatever moment someone happened to open a screen rather than when the
    # decay actually occurred. The number served is identical either way — it comes
    # from explain() — so persisting it is an optimisation, not a correctness need.
    # sweep_decay() is the intended writer; scripts/sweep_decay.py schedules it.
    return explain(con, lead_id, now)["score"]


def sweep_decay(con: sqlite3.Connection, now: datetime | None = None) -> int:
    """The other half of materialization: behaviour points fade purely by
    time passing (D8), with no new event to trigger record()'s automatic
    recompute. Run this on a schedule (see scripts/sweep_decay.py — cron/
    launchd, not wired by this repo) so a lead that has gone quiet actually
    drops out of `hot_leads`/crosses back under threshold without waiting for
    its next event. One commit for the whole sweep, not one per lead, so this
    stays a single round trip's worth of write I/O even at 30k rows."""
    now = now or datetime.utcnow()
    ids = [r["id"] for r in con.execute("SELECT id FROM leads WHERE merged_into IS NULL")]
    for lead_id in ids:
        recompute_lead(con, lead_id, now, commit=False)
    con.commit()
    return len(ids)
