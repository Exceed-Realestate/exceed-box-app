#!/usr/bin/env python3
"""Create the QA sign-in accounts, in Supabase Auth and in app_user, idempotently.

These exist so the permission matrix can be walked end to end against the real
stack — a real GoTrue password grant, a real ES256 token, real server-side role
enforcement — rather than asserted from unit tests.

Every account is prefixed `qa-` and lives on the company domain because
app/auth.py enforces the domain server-side. They are obviously test accounts by
name, carry no personal data, and are safe to delete:

    python scripts/provision_test_users.py --delete

Requires SUPABASE_URL, SUPABASE_SECRET_KEY and DATABASE_URL in the environment.
Never prints a password or a key.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402

DOMAIN = os.environ.get("EXCEEDBOX_QA_DOMAIN", "exceed-re.ae")

# role, office, display name. One sales rep per office so cross-rep isolation is
# testable, plus the least-privilege marketing role the audits covered.
ACCOUNTS = [
    ("qa-admin",     "admin",          "tokyo", "QA Admin"),
    ("qa-manager",   "office_manager", "tokyo", "QA Manager"),
    ("qa-sales-a",   "sales",          "tokyo", "QA Sales A"),
    ("qa-sales-b",   "sales",          "dubai", "QA Sales B"),
    ("qa-marketing", "marketing",      "tokyo", "QA Marketing"),
    ("qa-inactive",  "sales",          "tokyo", "QA Inactive"),
]


def _req(method: str, path: str, body=None):
    url = "%s/auth/v1%s" % (os.environ["SUPABASE_URL"].rstrip("/"), path)
    key = os.environ["SUPABASE_SECRET_KEY"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "apikey": key,
        "Authorization": "Bearer %s" % key,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise SystemExit("Supabase %s %s failed: %s %s" % (method, path, e.code, detail))


def _existing_by_email():
    out = {}
    page = 1
    while True:
        res = _req("GET", "/admin/users?per_page=200&page=%d" % page)
        users = res.get("users", res if isinstance(res, list) else [])
        if not users:
            break
        for u in users:
            out[(u.get("email") or "").lower()] = u["id"]
        if len(users) < 200:
            break
        page += 1
    return out


def provision(password: str):
    existing = _existing_by_email()
    con = db.connect()
    made, reused = 0, 0
    for local, role, office, name in ACCOUNTS:
        email = "%s@%s" % (local, DOMAIN)
        uid = existing.get(email)
        if uid:
            reused += 1
            # keep the password predictable across reruns
            _req("PUT", "/admin/users/%s" % uid, {"password": password})
        else:
            u = _req("POST", "/admin/users", {
                "email": email, "password": password, "email_confirm": True,
                "user_metadata": {"full_name": name, "qa_account": True},
            })
            uid = u["id"]
            made += 1

        active = local != "qa-inactive"
        row = db.one(con, "SELECT id FROM app_user WHERE lower(email::text)=?", (email,))
        if row:
            con.execute(
                "UPDATE app_user SET supabase_uid=?, role=?, office=?, is_active=?,"
                " display_name=? WHERE id=?",
                (uid, role, office, active, name, row["id"]))
        else:
            con.execute(
                "INSERT INTO app_user (id, supabase_uid, email, display_name, role, office,"
                " is_active) VALUES (?,?,?,?,?,?,?)",
                (uid, uid, email, name, role, office, active))
    con.commit()
    print("auth users: %d created, %d updated" % (made, reused))
    for r in db.all_(con, "SELECT email::text AS email, role, office, is_active"
                          " FROM app_user ORDER BY role, email"):
        print("  %-28s %-15s %-6s %s" % (r["email"], r["role"], r["office"] or "-",
                                         "active" if r["is_active"] else "INACTIVE"))
    con.close()


def delete():
    existing = _existing_by_email()
    con = db.connect()
    n = 0
    for local, _, _, _ in ACCOUNTS:
        email = "%s@%s" % (local, DOMAIN)
        uid = existing.get(email)
        if uid:
            _req("DELETE", "/admin/users/%s" % uid)
            n += 1
        con.execute("DELETE FROM app_user WHERE lower(email::text)=?", (email,))
    con.commit()
    con.close()
    print("removed %d auth users and their app_user rows" % n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--password", default=os.environ.get("EXCEEDBOX_QA_PASSWORD"))
    a = ap.parse_args()
    if a.delete:
        delete()
    else:
        if not a.password:
            raise SystemExit("set EXCEEDBOX_QA_PASSWORD or pass --password")
        provision(a.password)
