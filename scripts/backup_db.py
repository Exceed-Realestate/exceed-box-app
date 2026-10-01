"""Back up every table to compressed CSV, verify it, and prune old runs.

Why not pg_dump: the production host has no Postgres client installed, and the box is
short on disk. This uses the psycopg already in the venv, writes one gzipped
CSV per table, and checks each file's row count against the table it came
from — a dump nobody has opened is a hope, not a backup.

    .venv/bin/python scripts/backup_db.py              # run one backup
    .venv/bin/python scripts/backup_db.py --list       # what exists
    .venv/bin/python scripts/backup_db.py --keep 14    # change retention

What it does NOT cover: Supabase Storage (business-card photos), which is
replicated by Supabase and whose contents are customer personal data, and
`.env`, which holds the keys and is the one unrecoverable file here — copy
that somewhere safe by hand, once.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEST = os.environ.get("EXCEEDBOX_BACKUP_DIR", os.path.expanduser("~/backups/exceedbox"))


def _tables(cur):
    cur.execute("""SELECT table_name FROM information_schema.tables
                    WHERE table_schema='public' AND table_type='BASE TABLE'
                    ORDER BY table_name""")
    return [r[0] for r in cur.fetchall()]


def run_backup(keep: int) -> int:
    import psycopg

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set — source the .env first")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(DEST, stamp)
    os.makedirs(out_dir, exist_ok=True)

    manifest = {"started_at": dt.datetime.now().isoformat(timespec="seconds"), "tables": {}}
    total_bytes = 0
    with psycopg.connect(url) as con:
        cur = con.cursor()
        for table in _tables(cur):
            cur.execute('SELECT count(*) FROM "%s"' % table)
            expected = cur.fetchone()[0]
            path = os.path.join(out_dir, f"{table}.csv.gz")
            written = 0
            buf = io.StringIO()
            writer = csv.writer(buf)
            with cur.copy('COPY (SELECT * FROM "%s") TO STDOUT WITH CSV HEADER' % table) as copy:
                with gzip.open(path, "wb") as fh:
                    for chunk in copy:
                        fh.write(bytes(chunk))
            # verify: count the data lines back out of the file we just wrote
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                written = max(0, sum(1 for _ in fh) - 1)
            size = os.path.getsize(path)
            total_bytes += size
            ok = written == expected
            manifest["tables"][table] = {"rows_in_db": expected, "rows_in_file": written,
                                         "bytes": size, "ok": ok}
            if not ok:
                print(f"  MISMATCH {table}: db={expected} file={written}")
        del buf, writer

    manifest["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    manifest["total_bytes"] = total_bytes
    manifest["ok"] = all(t["ok"] for t in manifest["tables"].values())
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)

    rows = sum(t["rows_in_db"] for t in manifest["tables"].values())
    print(f"backup {stamp}: {len(manifest['tables'])} tables, {rows} rows, "
          f"{total_bytes/1024:.0f} KB, verified={manifest['ok']}")

    # prune: keep the newest `keep` runs, nothing older
    runs = sorted(d for d in os.listdir(DEST) if os.path.isdir(os.path.join(DEST, d)))
    for old in runs[:-keep] if keep > 0 else []:
        shutil.rmtree(os.path.join(DEST, old))
        print(f"  pruned {old}")
    return 0 if manifest["ok"] else 1


def list_backups() -> int:
    if not os.path.isdir(DEST):
        print("no backups yet at", DEST)
        return 0
    for d in sorted(os.listdir(DEST)):
        mpath = os.path.join(DEST, d, "manifest.json")
        if not os.path.isfile(mpath):
            print(f"  {d}  (no manifest — incomplete run)")
            continue
        m = json.load(open(mpath))
        rows = sum(t["rows_in_db"] for t in m["tables"].values())
        print(f"  {d}  tables={len(m['tables'])} rows={rows} "
              f"{m['total_bytes']/1024:.0f} KB verified={m['ok']}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=7, help="how many runs to keep (default 7)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    sys.exit(list_backups() if args.list else run_backup(args.keep))
