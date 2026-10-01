#!/usr/bin/env python3
"""The one background process Exceed Box needs.

Four jobs, in order, each safe to run twice and safe to interrupt:

  1. reclaim  — return jobs whose worker died back to the queue
  2. sweep    — queue every nurture step that has come due
  3. send     — hand queued jobs to the mail provider
  4. decay    — recompute scores so behaviour points fade (D8)

Step 4 is load-bearing for reporting, not a nicety: with the sweep unscheduled,
`team_status` badges and the score-threshold filter serve stale numbers until
something else happens to recompute them.

Run it on a timer — every five minutes is plenty at this scale:

    */5 * * * *  cd /srv/exceed-box && .venv/bin/python scripts/worker.py --once

or as a loop under a supervisor:

    .venv/bin/python scripts/worker.py --loop --interval 300

Exits non-zero if any job raised, so a supervisor or a cron mailer notices.
Nothing here sends anything unless EXCEEDBOX_MAIL_PROVIDER is deliberately set
to a real provider — the default records and delivers nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, mailer, scoring  # noqa: E402

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    print("[worker] signal %s — finishing the current pass, then stopping" % signum,
          flush=True)


def run_pass(*, verbose: bool = True) -> dict:
    """One full pass. Returns a summary; never raises for a single job."""
    started = dt.datetime.utcnow()
    out = {"started_at": started.isoformat(sep=" ", timespec="seconds")}
    con = db.connect()
    ok = True
    try:
        for name, fn in (
            ("sweep", lambda: mailer.sweep_due(con)),
            ("send", lambda: mailer.run_once(con)),
            ("decay", lambda: _decay(con)),
        ):
            try:
                out[name] = fn()
            except Exception as e:
                ok = False
                out[name] = {"error": str(e)[:300]}
                traceback.print_exc()
        out["ok"] = ok
        out["seconds"] = round((dt.datetime.utcnow() - started).total_seconds(), 2)
        if verbose:
            print("[worker] %s" % json.dumps(out, default=str), flush=True)
        return out
    finally:
        con.close()


def _decay(con):
    """`sweep_decay` lives in scoring/scripts already — this only schedules it."""
    fn = getattr(scoring, "sweep_decay", None)
    if fn is None:
        return {"skipped": "scoring.sweep_decay not present"}
    n = fn(con)
    con.commit()
    return {"recomputed": n}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="one pass, then exit (cron)")
    g.add_argument("--loop", action="store_true", help="run for ever (supervisor)")
    ap.add_argument("--interval", type=int, default=300, help="seconds between passes")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    p = mailer.describe()
    print("[worker] mail provider=%s delivers=%s ready=%s" %
          (p["provider"], p["delivers"], p["ready"]), flush=True)
    for problem in p["problems"]:
        print("[worker]   note: %s" % problem, flush=True)

    if a.loop:
        rc = 0
        while not _stop:
            out = run_pass(verbose=not a.quiet)
            rc = 0 if out.get("ok") else 1
            for _ in range(a.interval):
                if _stop:
                    break
                time.sleep(1)
        return rc

    out = run_pass(verbose=not a.quiet)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
