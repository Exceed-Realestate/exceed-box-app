"""Check the LIVE system against the OneBox proposal, item by item.

Every line comes from a real API call as qa-admin — nothing is read from the
code. Prints counts and names only, no customer data.

    .venv/bin/python scripts/spec_check.py
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

SECRETS = ("/Users/a44/Documents/dojo/dojo/Master vault/Exceed Real Estate/"
           "Exceed Box/🔐 Exceed-Box-Secrets.md")
SUPABASE = "https://mprvfdprioyhxsfjnqtk.supabase.co"
BASE = "https://exceedbox.app"
UA = "Mozilla/5.0 (Macintosh) ExceedBoxQA"


def call(url, data=None, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


text = open(SECRETS, encoding="utf-8").read()
pw = re.search(r"qa-\*@exceed-re\.ae` account: `([^`]+)`", text).group(1)
anon = re.search(r"Publishable key[^`]*`([^`]+)`", text).group(1)
code, body = call(f"{SUPABASE}/auth/v1/token?grant_type=password",
                  {"email": "qa-admin@exceed-re.ae", "password": pw}, {"apikey": anon})
H = {"Authorization": "Bearer " + body["access_token"]}


def get(path):
    return call(BASE + path, headers=H)


print("== 1. Lead aggregation (proposal p4/p6): one store, many sources")
_, ch = get("/api/leads/channels") if False else (0, {})
_, leads = get("/api/leads?page_size=1")
print(f"   leads endpoint: total={leads.get('total')}")
_, integ = get("/api/integrations")
for row in integ.get("data", integ if isinstance(integ, list) else []):
    print("   integration: %-16s connected=%-5s %s" % (row.get('key'), row.get('connected'), str(row.get('status_detail'))[:60]))

print("\n== 2. AI classify / dedupe / scoring (p4 step 2, p16)")
_, model = get("/api/scoring/model")
rules = model.get("rules", model.get("data", []))
print(f"   scoring rules live: {len(rules)} (proposal lists 9, Exceed decided 10)")
for r in rules:
    print("      " + json.dumps(r)[:120])
print(f"   threshold: {model.get('threshold')}")

print("\n== 3. Step-mail nurture (p9: 7 steps)")
_, seqs = get("/api/sequences")
for s in seqs.get("data", []):
    print(f"   sequence #{s.get('id')} '{s.get('name')}' steps={s.get('step_count')} active={s.get('active')} "
          f"sent={s.get('total_sent')} opened={s.get('total_opened')}")
_, seq1 = get("/api/sequences/1")
for st in seq1.get("steps", [])[:10]:
    print("      step " + json.dumps({k: st.get(k) for k in ('step_no','subject','cta','active','wait_days','stats')})[:190])

print("\n== 4. Reaction detection (p4 step 4)")
_, act = get("/api/activity?page_size=5")
kinds = sorted({a.get("kind") for a in act.get("data", [])})
print(f"   activity rows: total={act.get('total')} kinds seen={kinds}")

print("\n== 5–6. Booking page + calendar (p15)")
_, bk = get("/api/booking/settings")
for mt in bk.get("meeting_types", []):
    print("   meeting type: " + json.dumps(mt)[:150])
import urllib.request as _u  # the booking page is HTML, not JSON
_req = _u.Request(BASE + "/book", headers={"User-Agent": UA})
print(f"   booking page /book: HTTP {_u.urlopen(_req, timeout=60).status} (public, no login)")
_, cal = get("/api/calendar")
print(f"   calendar endpoint: {len(cal.get('days', cal.get('data', [])) or [])} day rows")

print("\n== 7. Notify the rep (p17 Today view)")
_, today = get("/api/today")
print(f"   today items: {today.get('total')}")
_, push = get("/api/push/settings")
print(f"   push settings: {json.dumps(push)[:120]}")

print("\n== 8. Management dashboard (p5: 9 tiles + 3 panels)")
_, dash = get("/api/dashboard")
print(f"   tiles: {len(dash.get('tiles', []))}")
for t in dash.get("tiles", []):
    print("      " + json.dumps({k: t.get(k) for k in ('key','value','unavailable_reason')})[:150])
print(f"   panels: by_rep={'yes' if dash.get('by_rep') is not None else 'no'} "
      f"by_source={len(dash.get('by_source', []))} hot_leads={len(dash.get('hot_leads', []))} "
      f"funnel={len(dash.get('funnel', []))} trend_points={len(dash.get('trend', []))}")

print("\n== SNS plans (p10–p14: 4 patterns, 20 posts)")
_, sns = get("/api/sns/patterns")
print(f"   patterns: {len(sns.get('data', []))}")
for p in sns.get("data", []):
    print("      " + json.dumps({k: p.get(k) for k in ('key','title_en','leads_count')})[:150])
_, funnel = get("/api/sns/funnel")
print(f"   sns funnel stages: {len(funnel.get('stages', funnel.get('data', [])) or [])}")

print("\n== Mail delivery (proposal assumes real sending)")
code, health = call(BASE + "/api/health")
print(f"   {json.dumps(health.get('mail'))}")
