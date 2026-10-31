#!/bin/sh
set -eu

# Startup tasks run once per release, not once per Gunicorn worker. In a scaled
# deployment the "migrate" compose service owns them and sets
# DJANGO_RUN_STARTUP_TASKS=0 here; a bare "docker run" keeps the old behaviour
# so a single container still migrates and collects before serving traffic.
if [ "${DJANGO_RUN_STARTUP_TASKS:-1}" = "1" ]; then
  python docker/startup_tasks.py
fi

exec gunicorn campus_glimmer.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --access-logfile -