#!/usr/bin/env bash
# What actually needs backing up, and what does not.
#
#   ./deploy/backup.sh                 # from the server, or with DATABASE_URL set
#
# Supabase already takes daily database backups on the Pro plan, with
# point-in-time recovery. Duplicating that is not the job here. What Supabase
# does NOT protect you from is somebody deleting rows on purpose, and what no
# managed service protects you from is losing the one file that says how this
# instance is configured.
#
# So this takes:
#   1. a logical dump of the schema and data, so a bad import can be undone
#      without a full project restore
#   2. the .env, encrypted, because it is the only unrecoverable artefact here
#
# The private media bucket is NOT dumped: Supabase Storage is replicated, and a
# copy of every customer's business card sitting on a VPS disk is a liability,
# not a backup.
set -euo pipefail

OUT="${BACKUP_DIR:-/srv/exceed-box/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

if [ -f /srv/exceed-box/.env ]; then set -a; . /srv/exceed-box/.env; set +a; fi
: "${DATABASE_URL:?DATABASE_URL is not set}"

echo "==> Dumping the database"
# --no-owner/--no-acl so the dump restores into a different Supabase project
# without complaining about roles that do not exist there.
docker run --rm -e PGPASSWORD -i postgres:16-alpine \
  pg_dump --no-owner --no-acl --format=custom "$DATABASE_URL" \
  > "$OUT/exceedbox-$STAMP.dump"

SIZE=$(du -h "$OUT/exceedbox-$STAMP.dump" | cut -f1)
echo "    $OUT/exceedbox-$STAMP.dump ($SIZE)"

# A dump that restores is a backup. A dump nobody has opened is a hope.
echo "==> Verifying the dump is readable and contains the expected tables"
docker run --rm -i postgres:16-alpine pg_restore --list < "$OUT/exceedbox-$STAMP.dump" \
  | grep -E 'TABLE DATA public (leads|app_user|sends|calendar_events)' \
  | sed 's/^/    found: /' \
  || { echo "    DUMP LOOKS WRONG — it does not contain the core tables" >&2; exit 1; }

if [ -f /srv/exceed-box/.env ]; then
  echo "==> Copying the configuration"
  if command -v age >/dev/null 2>&1 && [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
    age -r "$BACKUP_AGE_RECIPIENT" -o "$OUT/env-$STAMP.age" /srv/exceed-box/.env
    echo "    $OUT/env-$STAMP.age (encrypted)"
  else
    install -m 600 /srv/exceed-box/.env "$OUT/env-$STAMP"
    echo "    $OUT/env-$STAMP (PLAINTEXT — it holds live keys)"
    echo "    Set BACKUP_AGE_RECIPIENT and install age to encrypt it, or move"
    echo "    these off the server."
  fi
fi

echo "==> Removing backups older than $KEEP_DAYS days"
find "$OUT" -name 'exceedbox-*.dump' -mtime "+$KEEP_DAYS" -print -delete || true
find "$OUT" -name 'env-*' -mtime "+$KEEP_DAYS" -print -delete || true

cat <<MSG

Done. $(ls -1 "$OUT"/exceedbox-*.dump 2>/dev/null | wc -l | tr -d ' ') dump(s) held.

These live on the same server as the application, which protects against a bad
import but NOT against losing the server. Copy them somewhere else — that part
is not automated here because it needs a destination and credentials that are
Balraj's to choose.
MSG
