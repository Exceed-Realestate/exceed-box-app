#!/usr/bin/env python3
"""H1 (FABLE-AUDIT.md) — the decay half of score materialization.

`app/scoring.py`'s `record()`/`void()` keep `leads.score` current on every
write, but behaviour points (D8) also fade purely by TIME passing, with no
new event to trigger that. This script is the "nightly sweep" the fix calls
for: it recomputes every live lead's materialized score once, so a lead that
has gone quiet actually drops out of hot_leads / back under threshold without
waiting for its next click.

Not wired to a scheduler by this repo (no cron/launchd config lives here) —
run it from whatever schedules this deployment, e.g.:

    # crontab -e
    17 3 * * * cd /path/to/exceed-box-app && python3 scripts/sweep_decay.py

Usage:
    python3 scripts/sweep_decay.py            # sweep, print count + elapsed
    EXCEEDBOX_DB=/path/to/db python3 scripts/sweep_decay.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, scoring   # noqa: E402


def main() -> None:
    con = db.connect()
    try:
        t0 = time.time()
        n = scoring.sweep_decay(con)
        elapsed = time.time() - t0
        print("sweep_decay: %d leads recomputed in %.2fs" % (n, elapsed))
    finally:
        con.close()


if __name__ == "__main__":
    main()
