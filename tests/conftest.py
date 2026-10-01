"""Suite-wide environment, applied before any test module is imported.

This exists because two modules in `app/` resolve configuration once, at import
time — `app.db` fixes DB_PATH from EXCEEDBOX_DB, and `app.auth` resolves the
verification mode from SUPABASE_*/EXCEEDBOX_DEV_AUTH. Whichever test module
pytest happens to import first therefore decided both for the entire run.

That was load-bearing by accident: every test file set the same two variables
at its own top, and the alphabetically-first one silently won. Adding a test
file that sorted earlier and did not set them pointed the whole suite at the
real `data/exceedbox.db` — which `db.reset()` then deletes. Setting them here
removes the ordering dependency and makes it impossible for a new test file to
reintroduce it.
"""
from __future__ import annotations

import os
import tempfile

# A per-run temp database. Never the repo's data/exceedbox.db: the suite calls
# db.reset(), which unlinks the file it points at.
os.environ.setdefault(
    "EXCEEDBOX_DB", os.path.join(tempfile.mkdtemp(prefix="exceedbox-tests-"), "test.db"))

# Local dev tokens, never a real Supabase project. app.auth refuses to enable
# this if any real verification path is configured, so a stray SUPABASE_URL in
# the environment cannot silently turn it on.
os.environ["EXCEEDBOX_DEV_AUTH"] = "1"

# The login domains the fixtures sign in with. Assigned, not setdefault: an
# operator with this exported in their shell would otherwise break the suite.
# Every test module also sets this at its own top for the same import-once
# reason; those lines are now redundant but harmless, and identical to this.
os.environ["EXCEEDBOX_ALLOWED_EMAIL_DOMAINS"] = (
    "test.example,contract.example,fable-fix.example,exceed-re.ae")

# Real Supabase verification must never be configured for the SQLite suite —
# it would disable dev auth and 401 every fixture, and it would switch
# app.media to the remote backend for tests that expect local files.
#
# The values are preserved under EXCEEDBOX_TEST_* first, so the opt-in
# integration files can still reach the real project deliberately. Removing
# them outright silently skipped the live storage tests, which looked like a
# pass.
#
# DATABASE_URL is stripped for the same reason and it is the most dangerous of
# the set: with it present the whole SQLite suite runs against the real Supabase
# database. `db.reset()` now refuses to run there, which turns that mistake into
# 127 loud failures instead of a wiped production schema — but the right answer
# is for `pytest` to simply be correct after `source .env`, with no env juggling
# and no memorised incantation.
for _leak in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY",
              "SUPABASE_JWT_SECRET", "EXCEEDBOX_SUPABASE_JWKS_URL",
              "EXCEEDBOX_SUPABASE_ISSUER"):
    _v = os.environ.pop(_leak, None)
    if _v:
        os.environ.setdefault("EXCEEDBOX_TEST_" + _leak, _v)
