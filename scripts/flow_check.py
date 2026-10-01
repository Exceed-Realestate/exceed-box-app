"""Exercise the flows the browser walk never opened, against the LIVE system.

Card scan (photo + typed fields), CSV import (analyze then commit), sequence
detail, and a lead's own detail/notes. Everything it creates is named
"FLOWCHECK …" so it is obvious in the UI and easy to remove.

    .venv/bin/python scripts/flow_check.py          # create + check
    .venv/bin/python scripts/flow_check.py --clean  # list what it made

Prints status codes and field names only.
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

SECRETS = ("/Users/a44/Documents/dojo/dojo/Master vault/Exceed Real Estate/"
           "Exceed Box/🔐 Exceed-Box-Secrets.md")
SUPABASE = "https://mprvfdprioyhxsfjnqtk.supabase.co"
BASE = "https://exceedbox.app"
UA = "Mozilla/5.0 (Macintosh) ExceedBoxQA"

# a real 1x1 PNG — the endpoint refuses an empty upload
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478"
    "9c6360000002000154a24f5e0000000049454e44ae426082")


def call(url, data=None, headers=None, method=None, raw=False):
    req = urllib.request.Request(
        url, data=data if raw else (json.dumps(data).encode() if data is not None else None),
        headers={"User-Agent": UA, **({} if raw else {"Content-Type": "application/json"}),
                 **(headers or {})}, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {}), time.time() - t0
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body), time.time() - t0
        except Exception:
            return e.code, {"raw": body[:200].decode(errors="replace")}, time.time() - t0


def multipart(fields: dict, filename: str, filecontent: bytes, field="image"):
    boundary = "----exceedbox" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
              f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    out.write(filecontent)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="only list what earlier runs created")
    args = ap.parse_args()

    text = open(SECRETS, encoding="utf-8").read()
    pw = re.search(r"qa-\*@exceed-re\.ae` account: `([^`]+)`", text).group(1)
    anon = re.search(r"Publishable key[^`]*`([^`]+)`", text).group(1)
    code, body, _ = call(f"{SUPABASE}/auth/v1/token?grant_type=password",
                         {"email": "qa-admin@exceed-re.ae", "password": pw}, {"apikey": anon})
    if code != 200:
        sys.exit("login failed: %s" % code)
    H = {"Authorization": "Bearer " + body["access_token"]}

    if args.clean:
        code, leads, _ = call(BASE + "/api/leads?q=FLOWCHECK&page_size=50", headers=H)
        for r in leads.get("data", []):
            print(f"  lead #{r['id']} {r['name']} stage={r['stage']}")
        print(f"{len(leads.get('data', []))} FLOWCHECK leads exist (delete from the app if unwanted)")
        return

    stamp = uuid.uuid4().hex[:6]
    print("== business card scan (SPEC §6 — photo stored, fields typed)")
    payload, ctype = multipart({
        "name": f"FLOWCHECK Card {stamp}", "company": "Flow Check KK",
        "title": "Director", "email": f"flowcheck-{stamp}@example.invalid",
        "phone": "0312345678", "consent_given": "false",
    }, "card.png", PNG)
    code, out, dt = call(BASE + "/api/leads/scan", data=payload, raw=True,
                         headers={**H, "Content-Type": ctype})
    print(f"   POST /api/leads/scan -> {code} in {dt:.1f}s")
    card_lead = out.get("id")
    if code == 200:
        print(f"   lead #{card_lead} channels={[c.get('channel') for c in out.get('channels', [])]} "
              f"card_image={'yes' if out.get('card_image_path') or out.get('card_image_url') else 'no'} "
              f"extraction={json.dumps(out.get('extraction'))[:80]}")
    else:
        print("   error:", json.dumps(out)[:200])

    print("\n== CSV import (analyze, then commit)")
    csv = ("name,company,email,phone,purpose\n"
           f"FLOWCHECK Import A {stamp},Import Test KK,flowimp-a-{stamp}@example.invalid,0311111111,investment\n"
           f"FLOWCHECK Import B {stamp},Import Test KK,flowimp-b-{stamp}@example.invalid,0322222222,relocation\n"
           f"FLOWCHECK Card {stamp},Flow Check KK,flowcheck-{stamp}@example.invalid,0312345678,investment\n")
    payload, ctype = multipart({}, "leads.csv", csv.encode(), field="file")
    code, an, dt = call(BASE + "/api/import/analyze", data=payload, raw=True,
                        headers={**H, "Content-Type": ctype})
    print(f"   POST /api/import/analyze -> {code} in {dt:.1f}s keys={sorted(an)[:8]}")
    if code == 200:
        print("   " + json.dumps({k: an[k] for k in list(an)[:6]})[:220])
        # commit takes the analyze token plus the column mapping the user
        # confirms on screen — analyze only ever *guesses* it.
        mapping = {k: v for k, v in (an.get("guessed_mapping") or {}).items() if v}
        mapping.setdefault("purpose", "purpose")
        code, cm, dt = call(BASE + "/api/import/commit",
                            {"token": an.get("token"), "mapping": mapping}, headers=H)
        print(f"   POST /api/import/commit -> {code} in {dt:.1f}s {json.dumps(cm)[:220]}")

    print("\n== sequence detail")
    code, seq, dt = call(BASE + "/api/sequences/1", headers=H)
    print(f"   GET /api/sequences/1 -> {code} in {dt:.1f}s steps={len(seq.get('steps', []))} "
          f"keys={sorted(seq)[:8]}")

    if card_lead:
        print("\n== lead detail + notes for the scanned card")
        for path in (f"/api/leads/{card_lead}", f"/api/leads/{card_lead}/notes",
                     f"/api/leads/{card_lead}/replies", f"/api/leads/{card_lead}/score"):
            code, out, dt = call(BASE + path, headers=H)
            print(f"   GET {path} -> {code} in {dt:.1f}s")

    print("\nCleanup: run with --clean to list the FLOWCHECK rows this created.")


if __name__ == "__main__":
    main()
