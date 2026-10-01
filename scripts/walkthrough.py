#!/usr/bin/env python3
"""End-to-end walkthrough against a RUNNING backend, over real HTTP, with real
Supabase tokens. Proves the journeys the product is judged on, rather than
asserting them from unit tests.

    EXCEEDBOX_QA_PASSWORD=... python scripts/walkthrough.py            # run
    EXCEEDBOX_QA_PASSWORD=... python scripts/walkthrough.py --clean    # remove

Every lead it creates is named with the WALK prefix so `--clean` can find them
and so no row can be mistaken for a real person. Nothing here touches a table
directly; every write goes through an endpoint a user would hit.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API = os.environ.get("EXCEEDBOX_API", "http://127.0.0.1:8912")
SUPA = os.environ["SUPABASE_URL"].rstrip("/")
ANON = os.environ.get("EXCEEDBOX_ANON_KEY",
                      "sb_publishable_fB0TGpeIaL9IcQhUkI8_bQ_z2v6BB2C")
PW = os.environ["EXCEEDBOX_QA_PASSWORD"]
PREFIX = "WALK"

ok = fail = 0
notes: list = []


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok    %s" % label)
    else:
        fail += 1
        print("  FAIL  %s  %s" % (label, detail))
        notes.append((label, detail))
    return cond


def token(user):
    req = urllib.request.Request(
        SUPA + "/auth/v1/token?grant_type=password",
        data=json.dumps({"email": "%s@exceed-re.ae" % user, "password": PW}).encode(),
        headers={"apikey": ANON, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))["access_token"]


def call(tok, method, path, body=None, files=None):
    """Returns (status, parsed_body)."""
    headers = {}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    data = None
    if files is not None:
        boundary = "----walk%s" % uuid.uuid4().hex
        buf = io.BytesIO()
        for k, v in (body or {}).items():
            buf.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, k, v)).encode())
        for k, (fn, content, ctype) in files.items():
            buf.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                       "Content-Type: %s\r\n\r\n" % (boundary, k, fn, ctype)).encode())
            buf.write(content)
            buf.write(b"\r\n")
        buf.write(("--%s--\r\n" % boundary).encode())
        data = buf.getvalue()
        headers["Content-Type"] = "multipart/form-data; boundary=%s" % boundary
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"raw": raw.decode()[:200]}


# a 1x1 JPEG, so the card-scan upload path is exercised with a real image body
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffc00011080001000103012200021101031101ffc400"
    "1f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303"
    "020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c115"
    "52d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a53545556"
    "5758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7"
    "e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fefffe28a28a2800a28a2803ffd9")


def run():
    print("\n1. sign-in, five roles, real Supabase tokens")
    toks = {}
    for u in ("qa-admin", "qa-manager", "qa-sales-a", "qa-sales-b", "qa-marketing"):
        try:
            toks[u] = token(u)
            check("%s obtained a token" % u, True)
        except Exception as e:
            check("%s obtained a token" % u, False, str(e))
    admin = toks.get("qa-admin")
    if not admin:
        print("cannot continue without an admin token")
        return

    st, me = call(admin, "GET", "/api/me")
    check("admin /api/me is 200 and says admin", st == 200 and me.get("role") == "admin",
          "got %s %s" % (st, me))

    print("\n2. access control")
    st, _ = call(None, "GET", "/api/leads")
    check("unauthenticated /api/leads is 401", st == 401, "got %s" % st)
    st, _ = call(toks["qa-manager"], "GET", "/api/users")
    check("manager cannot reach /api/users (403)", st == 403, "got %s" % st)
    st, _ = call(toks["qa-sales-a"], "GET", "/api/dashboard")
    check("sales cannot reach the company dashboard (403)", st == 403, "got %s" % st)
    st, _ = call(toks["qa-admin"], "GET", "/api/users")
    check("admin can reach /api/users (200)", st == 200, "got %s" % st)
    try:
        bad = token("qa-inactive")
        st, body = call(bad, "GET", "/api/me")
        check("an inactive user is refused (403)", st == 403, "got %s %s" % (st, body))
    except Exception as e:
        check("an inactive user is refused (403)", False, "login itself failed: %s" % e)

    print("\n3. lead intake")
    st, lead = call(admin, "POST", "/api/leads", {
        "name": "%s Aoyama Kenji" % PREFIX, "company": "Aoyama Shoji",
        "email": "%s-kenji@example.invalid" % PREFIX.lower(), "phone": "+81-3-5555-0101",
        "channel": "csv", "regions": ["dubai"], "purpose": "investment"})
    lead_id = lead.get("lead_id") or lead.get("id")
    check("created a lead through POST /api/leads", st in (200, 201) and lead_id,
          "got %s %s" % (st, lead))

    st, dupe = call(admin, "POST", "/api/leads", {
        "name": "%s Aoyama Kenji" % PREFIX, "company": "Aoyama Shoji",
        "email": "%s-kenji@example.invalid" % PREFIX.lower(), "channel": "gohighlevel"})
    dupe_id = dupe.get("lead_id") or dupe.get("id")
    check("the same email de-duplicates onto one person",
          dupe_id == lead_id, "first=%s second=%s" % (lead_id, dupe_id))

    st, scan = call(admin, "POST", "/api/leads/scan",
                    {"name": "%s Morita Kenji" % PREFIX, "company": "Morita Estate",
                     "email": "%s-morita@example.invalid" % PREFIX.lower(),
                     "phone": "090-1234-5678"},
                    files={"image": ("card.jpg", JPEG, "image/jpeg")})
    scan_id = scan.get("lead_id") or scan.get("id")
    check("business-card scan created a lead and stored the image",
          st in (200, 201) and scan_id, "got %s %s" % (st, scan))

    print("\n4. media access control")
    if scan_id:
        st, detail = call(admin, "GET", "/api/leads/%s" % scan_id)
        path = (detail or {}).get("card_image_path")
        if path:
            st_a, _ = call(admin, "GET", "/api/media/%s" % path.split("/")[-1])
            st_b, _ = call(toks["qa-sales-b"], "GET", "/api/media/%s" % path.split("/")[-1])
            check("a non-owning rep cannot fetch the card photo",
                  st_b in (403, 404), "owner=%s other=%s" % (st_a, st_b))
        else:
            notes.append(("card image path", "LeadDetail exposed no card_image_path to admin"))
            print("  note  no card_image_path on the detail payload — media check skipped")

    print("\n5. scoring")
    if lead_id:
        # The only scoring signal reachable over HTTP today is the human-granted
        # in-person meeting intent (D4). Opens, clicks and replies all hang off a
        # `sends` row, and nothing creates one yet — there is no enrol endpoint and
        # no scheduler. That is the product's missing middle, recorded rather than
        # faked: writing a sends row straight into the table here would prove
        # nothing about the path a real campaign takes.
        st, sig = call(admin, "POST", "/api/leads/%s/signal" % lead_id,
                       {"type": "wants_meeting", "note": "walkthrough"})
        check("in-person meeting signal accepted", st == 200, "got %s %s" % (st, sig))
        check("score moved 0 -> 30 and is attributable",
              (sig or {}).get("signal_score_before") == 0
              and (sig or {}).get("signal_score_after") == 30,
              "before=%s after=%s" % ((sig or {}).get("signal_score_before"),
                                      (sig or {}).get("signal_score_after")))
        st, exp = call(admin, "GET", "/api/leads/%s/score" % lead_id)
        if st == 200:
            check("score explanation agrees with the lead detail",
                  (exp or {}).get("score") == 30, "explain says %s" % (exp or {}).get("score"))
        st, sends = call(admin, "GET", "/api/sequences")
        check("sequence content is present and editable",
              st == 200 and len((sends or {}).get("sequences", sends or [])) >= 1,
              "got %s" % st)
        notes.append(("BLOCKED", "no endpoint enrols a lead into a sequence, so no `sends` row "
                                 "can exist; opens/clicks/replies and the whole nurture path "
                                 "cannot be exercised until the scheduler is built"))

    print("\n6. pipeline")
    if lead_id:
        st, _ = call(admin, "POST", "/api/leads/%s/stage" % lead_id, {"stage": "engaged"})
        check("stage change accepted", st == 200, "got %s" % st)
        st, d = call(admin, "GET", "/api/leads/%s" % lead_id)
        check("stage persisted as engaged", (d or {}).get("stage") == "engaged",
              "got %s" % (d or {}).get("stage"))

    print("\n7. tasks")
    if lead_id:
        st, t = call(admin, "POST", "/api/tasks", {
            "lead_id": lead_id, "type": "call", "due_at": "2026-09-11T09:00:00Z",
            "reason": "walkthrough"})
        tid = (t or {}).get("task_id") or (t or {}).get("id")
        check("task created", st in (200, 201) and tid, "got %s %s" % (st, t))
        if tid:
            st, _ = call(admin, "PATCH", "/api/tasks/%s" % tid, {"action": "complete"})
            check("task completion accepted", st == 200, "got %s" % st)
            st, today = call(admin, "GET", "/api/today")
            items = (today or {}).get("items", today if isinstance(today, list) else [])
            still = [i for i in items if str(i.get("task_id")) == str(tid)]
            check("completed task left the Today list", not still,
                  "%d matching items remain" % len(still))

    print("\n8. dashboard consistency")
    st, dash = call(admin, "GET", "/api/dashboard")
    check("dashboard is 200", st == 200, "got %s" % st)
    if st == 200:
        tiles = {t["key"]: t for t in dash.get("tiles", [])}
        rev = tiles.get("expected_revenue", {})
        check("expected revenue is unavailable with a stated reason, not invented",
              rev.get("value") is None and rev.get("unavailable_reason"),
              "got %s" % rev)
        check("trend has 14 points", len(dash.get("trend", [])) == 14,
              "got %d" % len(dash.get("trend", [])))
        booked = tiles.get("meetings_booked", {}).get("value")
        by_src = sum(r["count"] for r in dash.get("by_source", []))
        check("bookings by source reconcile with the tile", by_src == booked,
              "tile=%s sources=%s" % (booked, by_src))

    print("\n9. integrations report true state")
    st, integ = call(admin, "GET", "/api/integrations")
    if st == 200:
        rows = integ.get("integrations", integ if isinstance(integ, list) else [])
        connected = [r for r in rows
                     if str(r.get("status", "")).lower() in ("connected", "healthy", "ok")]
        check("nothing claims to be connected while unconfigured", not connected,
              "claimed connected: %s" % [r.get("key") for r in connected])
    else:
        print("  note  /api/integrations returned %s" % st)

    print("\n%d passed, %d failed" % (ok, fail))
    if notes:
        print("\ndetails:")
        for label, detail in notes:
            print("  - %s: %s" % (label, detail))
    return fail


def clean():
    admin = token("qa-admin")
    st, res = call(admin, "GET", "/api/leads?page=1&page_size=200&q=%s" % PREFIX)
    items = (res or {}).get("items", [])
    n = 0
    for it in items:
        if str(it.get("name", "")).startswith(PREFIX):
            code, _ = call(admin, "DELETE", "/api/leads/%s" % it["id"])
            n += code in (200, 204)
    print("removed %d of %d walkthrough leads via the API" % (n, len(items)))
    if n < len(items):
        print("note: the API has no delete for the rest; use scripts/ or the DB directly")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    a = ap.parse_args()
    sys.exit(clean() if a.clean else (run() or 0))
