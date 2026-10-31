#!/bin/sh
set -eu

# Startup tasks run once per release, not once per Gunicorn worker. In a scaled
# deployment the "migrate" compose service owns them and sets
# DJANGO_RUN_STARTUP_TASKS=0 here; a bare "docker run" keeps the old behaviour
# so a single container still migrates and collects before serving traffic.
if [ "${DJANGO_RUN_STARTUP_TASKS:-1}" = "1" ]; then
  python docker/startup_tasks.py
fi

# 多副本部署下，一条用户投诉只能落到某个副本的某一行日志里。access log 带上
# 请求关联 ID（由 RequestIdMiddleware 写进响应头）和请求耗时，才能把一次投诉
# 对回具体请求，也才能在没有任何指标系统时看出慢请求。
exec gunicorn campus_glimmer.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --access-logfile - \
  --access-logformat '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %({X-Request-ID}o)s %(L)s'