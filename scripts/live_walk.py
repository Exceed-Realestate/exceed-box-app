"""Walk the LIVE system the way the staff app does, once per QA role.

Signs in to Supabase as each qa-* account and calls every GET that the app's
screens load on open. Prints status codes, error codes and timings only —
never a token, password or record body.

    .venv/bin/python scripts/live_walk.py [--base https://exceedbox.app]

Cloudflare's browser integrity check answers Python's default user agent with
403 "error code: 1010", which looks exactly like a permissions bug. A browser
style User-Agent is sent for that reason.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request

SECRETS = ("/Users/a44/Documents/dojo/dojo/Master vault/Exceed Real Estate/"
           "Exceed Box/🔐 Exceed-Box-Secrets.md")
SUPABASE = "https://mprvfdprioyhxsfjnqtk.supabase.co"
UA = "Mozilla/5.0 (Macintosh) ExceedBoxQA"

ROLES = ["qa-admin", "qa-manager", "qa-sales-a", "qa-sales-b", "qa-marketing", "qa-inactive"]

# screen -> the GETs it makes when it opens
SCREENS = {
    "Today": ["/api/today"],
    "Dashboard": ["/api/dashboard"],
    "Leads": ["/api/leads"],
    "Pipeline": ["/api/pipeline"],
    "Team": ["/api/team"],
    "Tracking": ["/api/activity"],
    "Nurture": ["/api/sequences"],
    "Sns": ["/api/sns/funnel", "/api/sns/patterns"],
    "Integrations": ["/api/integrations"],
    "Assign": ["/api/assignment/rules"],
    "Booking": ["/api/booking/settings", "/api/calendar"],
    "AdminUsers": ["/api/users"],
    "NotificationSettings": ["/api/push/settings"],
    "Scoring": ["/api/scoring/model"],
}


def _call(url, data=None, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": UA, **(headers or {})})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, time.time() - t0, r.read()
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0, e.read()


def _err(body: bytes) -> str:
    try:
        j = json.loads(body)
        return (j.get("error") or {}).get("code") or str(j)[:50]
    except Exception:
        return body[:50].decode(errors="replace").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://exceedbox.app")
    base = ap.parse_args().base

    text = open(SECRETS, encoding="utf-8").read()
    password = re.search(r"qa-\*@exceed-re\.ae` account: `([^`]+)`", text).group(1)
    anon = re.search(r"Publishable key[^`]*`([^`]+)`", text).group(1)

    for role in ROLES:
        code, _, body = _call(f"{SUPABASE}/auth/v1/token?grant_type=password",
                              {"email": f"{role}@exceed-re.ae", "password": password},
                              {"apikey": anon})
        if code != 200:
            print(f"{role:13} supabase login {code} {_err(body)}")
            continue
        auth = {"Authorization": "Bearer " + json.loads(body)["access_token"]}

        code, dt, body = _call(base + "/api/me", headers=auth)
        who = ""
        if code == 200:
            j = json.loads(body)
            u = j.get("user", j)
            who = f"role={u.get('role')} active={u.get('active', u.get('is_active'))}"
        print(f"\n{role:13} /api/me {code} {who or _err(body)} {dt:.1f}s")

        for screen, paths in SCREENS.items():
            parts = []
            for p in paths:
                code, dt, body = _call(base + p, headers=auth)
                tag = "" if code < 400 else f" {_err(body)}"
                parts.append(f"{p.split('?')[0][5:]} {code}{tag} {dt:.1f}s")
            print(f"  {screen:21} " + " | ".join(parts), flush=True)


if __name__ == "__main__":
    main()
