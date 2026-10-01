#!/usr/bin/env bash
# Deploy Exceed Box to a fresh or existing Linux server, from a laptop.
#
#   ./deploy/deploy.sh root@203.0.113.10
#
# Idempotent: safe to run again for every subsequent release. It installs Docker
# if absent, copies the code, refuses to start without a .env, applies database
# migrations, and only then swaps the running containers.
#
# The order matters. Migrations run BEFORE the new containers take traffic, so a
# request never reaches code expecting a column that does not exist yet.
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${REMOTE_DIR:-/srv/exceed-box}"
[ -n "$TARGET" ] || { echo "usage: $0 user@host" >&2; exit 2; }

here() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }
LOCAL="$(here)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Checking the server"
ssh "$TARGET" 'uname -a; echo; df -h / | tail -1'

say "Installing Docker if it is not already there"
ssh "$TARGET" 'command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sh)'
ssh "$TARGET" 'docker --version && docker compose version'

say "Copying the application to $REMOTE_DIR"
ssh "$TARGET" "mkdir -p $REMOTE_DIR"
# --delete keeps the server from accumulating files deleted locally. The
# excludes matter: .env lives ONLY on the server and must never be overwritten
# from a laptop, and data/ is the local-media fallback.
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'data' --exclude '.env' --exclude 'tests' \
  "$LOCAL/" "$TARGET:$REMOTE_DIR/"

say "Checking configuration"
if ! ssh "$TARGET" "test -f $REMOTE_DIR/.env"; then
  ssh "$TARGET" "cp $REMOTE_DIR/.env.example $REMOTE_DIR/.env && chmod 600 $REMOTE_DIR/.env"
  cat >&2 <<'MSG'

STOPPED. There is no .env on the server, so a template was copied there.

Fill it in before running this again:

    ssh <server> 'nano /srv/exceed-box/.env'

Nothing has been started. The application is deliberately unable to run on
defaults — a half-configured deployment that appears to work is worse than one
that refuses to start.
MSG
  exit 1
fi
ssh "$TARGET" "chmod 600 $REMOTE_DIR/.env"

say "Building the image"
ssh "$TARGET" "cd $REMOTE_DIR && docker compose build"

say "Applying database migrations"
# One-off container, not the running service: migrations must complete before
# any request reaches the new code.
ssh "$TARGET" "cd $REMOTE_DIR && docker compose run --rm api python -c \
  'from app import db; c=db.connect(); print(\"applied:\", db.migrate(c, verbose=True) or \"nothing new\"); print(\"identity counters:\", db.resync_identities(c) or \"in sync\"); c.close()'"

say "Seeding reference data (idempotent — inserts no people)"
ssh "$TARGET" "cd $REMOTE_DIR && docker compose run --rm api python scripts/bootstrap_reference.py"

say "Starting"
ssh "$TARGET" "cd $REMOTE_DIR && docker compose up -d --remove-orphans"

say "Waiting for health"
ssh "$TARGET" '
  for i in $(seq 1 30); do
    if docker inspect --format="{{.State.Health.Status}}" "$(docker compose -f /srv/exceed-box/docker-compose.yml ps -q api)" 2>/dev/null | grep -q healthy; then
      echo "healthy"; exit 0
    fi
    sleep 2
  done
  echo "NOT healthy after 60s — check: docker compose logs api" >&2
  exit 1'

say "Live state"
ssh "$TARGET" "cd $REMOTE_DIR && docker compose exec -T api curl -fsS http://127.0.0.1:8912/api/health"

cat <<'MSG'

Deployed.

Check, in this order:
  1. https://<your domain>/api/health   — mail.delivers tells you whether email
                                          is real or still a dry run
  2. https://<your domain>/book         — the public booking page
  3. docker compose logs -f worker      — a sweep every 5 minutes

To roll back: ./deploy/rollback.sh user@host
MSG
