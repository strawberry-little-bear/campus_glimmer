"""Machine-readable log formatting shared by Django and Gunicorn.

The production topology writes logs to stdout and a collector reads Docker's
log stream.  Human-readable text is convenient locally, but a collector should
not need a stack-specific regex to recover the level, logger and request ID.
Production therefore emits one compact JSON object per line while local and
test runs keep the previous text format.
"""

import json
import logging
import os
import traceback
from datetime import datetime, timezone

from .middleware import NO_REQUEST_ID, sanitize_request_id

DEFAULT_SERVICE_NAME = 'campus-glimmer'
JSON_FORMAT_NAME = 'json'


def _service_name(service=None):
    return (
        service
        or os.getenv('DJANGO_METRICS_SERVICE_NAME', '').strip()
        or DEFAULT_SERVICE_NAME
    )


def _utc_timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(
        timespec='milliseconds',
    ).replace('+00:00', 'Z')


class JsonLogFormatter(logging.Formatter):
    """Render one stable JSON object per line without leaking extra fields."""

    def __init__(self, *args, service=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = _service_name(service)

    def format(self, record):
        payload = {
            'timestamp': _utc_timestamp(datetime.fromtimestamp(
                record.created, timezone.utc,
            )),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'request_id': getattr(record, 'request_id', NO_REQUEST_ID)
            or NO_REQUEST_ID,
            'service': self.service,
            'pid': record.process,
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        if record.stack_info:
            payload['stack'] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def _integer_or_text(value):
    if value is None or value == '-':
        return '-'
    text = str(value)
    return int(text) if text.isdigit() else text


def build_access_log_record(atoms, occurred_at=None, service=None):
    """Build the Gunicorn access-log payload from its documented atoms.

    The raw request URI is intentionally not copied: it may contain tokens in a
    query string.  Method and path are enough for operational correlation, and
    cookies / authorization headers are never included.
    """

    duration_text = str(atoms.get('L') or '0')
    try:
        duration_seconds = float(duration_text)
    except (TypeError, ValueError):
        duration_seconds = 0.0

    return {
        'timestamp': _utc_timestamp(occurred_at),
        'level': 'INFO',
        'logger': 'gunicorn.access',
        'event': 'http_request',
        'message': 'request',
        'request_id': (
            sanitize_request_id(atoms.get('{x-request-id}o', ''))
            or NO_REQUEST_ID
        ),
        'service': _service_name(service),
        'pid': os.getpid(),
        'remote_addr': atoms.get('h') or '-',
        'method': atoms.get('m') or '-',
        'path': atoms.get('U') or '-',
        'protocol': atoms.get('H') or '-',
        'status': _integer_or_text(atoms.get('s')),
        'bytes': _integer_or_text(atoms.get('b')),
        'duration_seconds': round(duration_seconds, 6),
        'user_agent': atoms.get('a') or '-',
    }


try:
    from gunicorn.glogging import Logger as _GunicornLogger
except ImportError:  # Gunicorn imports fcntl on Windows and is not used there.
    _GunicornLogger = object


def _json_mode_enabled():
    return (
        os.getenv('DJANGO_LOG_FORMAT', 'text').strip().lower()
        == JSON_FORMAT_NAME
    )


class JsonLogger(_GunicornLogger):
    """Gunicorn logger that switches lines to JSON in production."""

    def setup(self, cfg):
        super().setup(cfg)
        if not _json_mode_enabled():
            return
        formatter = JsonLogFormatter(service=_service_name())
        for handler in self.error_log.handlers:
            handler.setFormatter(formatter)

    def access(self, resp, req, environ, request_time):
        if not _json_mode_enabled():
            return super().access(resp, req, environ, request_time)

        if not (
            self.cfg.accesslog
            or self.cfg.logconfig
            or self.cfg.logconfig_dict
            or self.cfg.logconfig_json
            or (
                self.cfg.syslog
                and not self.cfg.disable_redirect_access_to_syslog
            )
        ):
            return

        try:
            payload = build_access_log_record(
                self.atoms(resp, req, environ, request_time),
            )
            self.access_log.info(json.dumps(
                payload, ensure_ascii=False, separators=(',', ':'),
            ))
        except Exception:
            self.error(traceback.format_exc())
