#!/usr/bin/env bash
# Restore a dump — into a NEW Supabase project, never over a live one.
#
#   ./deploy/restore.sh backups/exceedbox-20260914T090000Z.dump \
#       'postgresql://postgres.<newref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres'
#
# Restoring on top of a database that is still serving traffic is how a bad
# hour becomes a bad week. The intended sequence is: create a fresh project,
# restore into it, point DATABASE_URL at it, redeploy. The old project stays
# untouched and available until somebody is sure.
set -euo pipefail

DUMP="${1:-}"; TARGET_URL="${2:-}"
[ -f "$DUMP" ] || { echo "usage: $0 <dump file> <target DATABASE_URL>" >&2; exit 2; }
[ -n "$TARGET_URL" ] || { echo "usage: $0 <dump file> <target DATABASE_URL>" >&2; exit 2; }

if [ -f /srv/exceed-box/.env ] && grep -q "$(echo "$TARGET_URL" | sed -n 's#.*postgres\.\([a-z]*\).*#\1#p')" /srv/exceed-box/.env 2>/dev/null; then
  cat >&2 <<'MSG'
REFUSED. That target looks like the database this server is CURRENTLY using.

Restore into a new Supabase project, then repoint DATABASE_URL. Restoring over
a live database destroys everything written since the dump, including anything
written while you were deciding to restore.
MSG
  exit 1
fi

echo "About to restore $(du -h "$DUMP" | cut -f1) into:"
echo "  $(echo "$TARGET_URL" | sed 's#:[^:@]*@#:****@#')"
read -r -p "Type RESTORE to continue: " confirm
[ "$confirm" = "RESTORE" ] || { echo "Cancelled."; exit 1; }

docker run --rm -i postgres:16-alpine \
  pg_restore --no-owner --no-acl --clean --if-exists \
  --dbname "$TARGET_URL" < "$DUMP"

echo
echo "Restored. Now, in order:"
echo "  1. point DATABASE_URL at the new project in /srv/exceed-box/.env"
echo "  2. ./deploy/deploy.sh user@host"
echo "  3. check /api/health, then sign in and confirm a lead you recognise"
echo
echo "The old project has not been touched. Keep it until you are sure."
