# Exceed Box — API and worker share one image.
#
# Same image, two commands: the compose file runs it once as the web service and
# once as the background worker. One image means the worker can never be running
# a different version of the scoring rules than the API that displays them,
# which is the sort of drift nobody notices until the numbers disagree.
FROM python:3.12-slim AS base

# psycopg[binary] ships its own libpq, so no build toolchain is needed. curl is
# here only for the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Never run as root. A container that only needs to read its own code and talk
# to Postgres has no business being able to write to its own filesystem.
RUN useradd --create-home --uid 10001 exceedbox
WORKDIR /srv/exceed-box

# Dependencies first: this layer is cached unless requirements.txt itself
# changes, so a code-only deploy does not reinstall psycopg every time.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "psycopg[binary]>=3.1" "cryptography>=42" gunicorn

COPY app/ ./app/
COPY migrations/ ./migrations/
COPY schema.sql ./
COPY scripts/ ./scripts/

# Somewhere for the local media backend to land IF someone runs without
# Supabase Storage configured. In a real deployment media lives in the private
# bucket and this stays empty.
RUN mkdir -p /srv/exceed-box/data/uploads && chown -R exceedbox:exceedbox /srv/exceed-box

USER exceedbox
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

EXPOSE 8912

# tini reaps zombies and, more importantly here, passes SIGTERM straight
# through — the worker's own handler finishes the pass it is in rather than
# being killed with a half-written send.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8912/api/health || exit 1

# Two Uvicorn workers is right for a 10-15 person internal tool on a small VPS:
# enough that one slow query does not block the office, few enough that the
# Supabase pooler connection count stays comfortable.
CMD ["gunicorn", "app.api:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", \
     "--bind", "0.0.0.0:8912", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
