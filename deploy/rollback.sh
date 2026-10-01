#!/usr/bin/env bash
# Roll back to the previously deployed image.
#
#   ./deploy/rollback.sh root@203.0.113.10
#
# Only the application rolls back. The DATABASE DOES NOT — migrations here are
# additive (new tables, new nullable columns), so older code runs fine against a
# newer schema. That is the reason the migrations are written that way, and the
# reason there is no "down" script to get wrong at three in the morning.
set -euo pipefail
TARGET="${1:-}"; REMOTE_DIR="${REMOTE_DIR:-/srv/exceed-box}"
[ -n "$TARGET" ] || { echo "usage: $0 user@host" >&2; exit 2; }

ssh "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
cd /srv/exceed-box
PREV="$(docker images exceed-box --format '{{.ID}} {{.CreatedAt}}' | sort -k2 -r | sed -n 2p | cut -d' ' -f1)"
if [ -z "$PREV" ]; then
  echo "No previous image on this server — nothing to roll back to." >&2
  exit 1
fi
echo "Rolling back to image $PREV"
docker tag "$PREV" exceed-box:latest
docker compose up -d --no-build
sleep 5
docker compose exec -T api curl -fsS http://127.0.0.1:8912/api/health
REMOTE
echo "Rolled back. The database was not touched."
