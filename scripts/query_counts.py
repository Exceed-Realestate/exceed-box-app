"""How many SQL statements does each screen's API call run?

On the production host every statement is a ~0.33 s round trip, so this count — not CPU —
is what makes a screen slow. Runs against a throwaway SQLite copy of the demo
seed; touches no real database.

    .venv/bin/python scripts/query_counts.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
os.environ.pop("DATABASE_URL", None)
os.environ.update(EXCEEDBOX_DB=os.path.join(tmp, "q.db"), EXCEEDBOX_DEV_AUTH="1",
                  EXCEEDBOX_ALLOWED_EMAIL_DOMAINS="exceed-re.ae")
for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_JWT_SECRET"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)

subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True,
               env=os.environ, cwd=ROOT, stdout=subprocess.DEVNULL)

from fastapi.testclient import TestClient  # noqa: E402

from app import api, db, devauth  # noqa: E402

counter = {"n": 0}
_real_connect = db.connect


def counting_connect():
    c = _real_connect()
    c.set_trace_callback(lambda s: counter.__setitem__("n", counter["n"] + 1))
    return c


db.connect = counting_connect

sub = str(uuid.uuid4())
c = _real_connect()
c.execute("INSERT INTO app_user (id, supabase_uid, email, display_name, role, is_active) "
          "VALUES (?,?,?,?,?,TRUE)", (str(uuid.uuid4()), sub, "qc@exceed-re.ae", "QC", "admin"))
c.commit()
c.close()

H = {"Authorization": "Bearer " + devauth.mint(sub=sub, email="qc@exceed-re.ae")}
client = TestClient(api.app)
PATHS = ["/api/me", "/api/today", "/api/dashboard", "/api/leads", "/api/pipeline",
         "/api/team", "/api/activity", "/api/sequences", "/api/sns/funnel",
         "/api/sns/patterns", "/api/integrations", "/api/assignment/rules",
         "/api/booking/settings", "/api/calendar", "/api/users", "/api/push/settings",
         "/api/scoring/model"]
for p in PATHS:
    counter["n"] = 0
    r = client.get(p, headers=H)
    print(f"{p:28} {r.status_code}  {counter['n']:4d} statements")
