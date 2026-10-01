"""D8 facts are states, not tallies: each fact kind scores once.

Found live 2026-09-14: a lead marked "wants to meet" twice sat at 60 instead
of 30 — one fact counted as two permanent +30s. Behaviour events (opens,
clicks) still add up per event; only facts are capped.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, scoring  # noqa: E402


def _lead(c, name):
    cur = c.execute("INSERT INTO leads (name) VALUES (?)", (name,))
    c.commit()
    return cur.lastrowid


def _points(c, kind):
    rule = scoring._rules(c).get(kind)
    return int(rule["points"]) if rule else None


def setup_module(_):
    c = db.connect()
    db.init(c)
    c.close()


def test_the_same_fact_twice_scores_once():
    c = db.connect()
    try:
        pts = _points(c, "wants_meeting")
        if pts is None:
            import pytest
            pytest.skip("scoring rules not seeded in this database")
        lid = _lead(c, "twice")
        scoring.record(c, lid, "wants_meeting", source="human")
        scoring.record(c, lid, "wants_meeting", source="human")
        ex = scoring.explain(c, lid)
        facts = [x for x in ex["breakdown"] if x["kind"] == "wants_meeting"]
        assert len(facts) == 1
        assert ex["score"] == pts
    finally:
        c.close()


def test_voiding_the_first_lets_the_next_one_count():
    c = db.connect()
    try:
        pts = _points(c, "wants_meeting")
        if pts is None:
            import pytest
            pytest.skip("scoring rules not seeded in this database")
        lid = _lead(c, "voided")
        scoring.record(c, lid, "wants_meeting", source="human")
        scoring.record(c, lid, "wants_meeting", source="human")
        first = c.execute("SELECT id FROM events WHERE lead_id=? AND kind='wants_meeting' "
                          "ORDER BY id LIMIT 1", (lid,)).fetchone()["id"]
        c.execute("UPDATE events SET voided_at=datetime('now') WHERE id=?", (first,))
        c.commit()
        assert scoring.explain(c, lid)["score"] == pts
    finally:
        c.close()


def test_different_facts_still_add_up():
    c = db.connect()
    try:
        a, b = _points(c, "wants_meeting"), _points(c, "high_budget")
        if a is None or b is None:
            import pytest
            pytest.skip("scoring rules not seeded in this database")
        lid = _lead(c, "two facts")
        scoring.record(c, lid, "wants_meeting", source="human")
        scoring.record(c, lid, "high_budget", source="human")
        assert scoring.explain(c, lid)["score"] == a + b
    finally:
        c.close()


def test_behaviour_still_stacks_per_event():
    c = db.connect()
    try:
        pts = _points(c, "click")
        if pts is None:
            import pytest
            pytest.skip("scoring rules not seeded in this database")
        lid = _lead(c, "clicks")
        for _ in range(3):
            scoring.record(c, lid, "click", source="system")
        clicks = [x for x in scoring.explain(c, lid)["breakdown"] if x["kind"] == "click"]
        assert len(clicks) == 3
    finally:
        c.close()
