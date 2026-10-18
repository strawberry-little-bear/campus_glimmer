"""Small deployment probes.

The two questions are deliberately separate: ``/healthz/`` proves that this
process can still handle a request, while ``/readyz/`` proves that its critical
persistence dependencies can answer small probes before traffic is sent here.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods


logger = logging.getLogger(__name__)
SERVICE_NAME = 'campus-glimmer'
OBJECT_STORAGE_BACKENDS = {'s3', 'minio'}


def _json_response(payload, status=200):
    response = JsonResponse(payload, status=status)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _probe_object_storage():
    """Verify that the configured S3-compatible bucket is reachable.

    The probe only performs a bucket metadata request. It neither creates the
    bucket nor writes a temporary object during every health check.
    """
    from django.core.files.storage import storages

    storage = storages['default']
    storage.connection.meta.client.head_bucket(Bucket=storage.bucket_name)


@require_http_methods(['GET', 'HEAD'])
def health_check(request):
    """Return 200 when this Django process can handle requests."""
    return _json_response({
        'status': 'ok',
        'service': SERVICE_NAME,
    })


@require_http_methods(['GET', 'HEAD'])
def ready_check(request):
    """Return 503 when a critical persistence dependency is unavailable."""
    checks = {}

    try:
        with connections['default'].cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        checks['database'] = 'ok'
    except Exception as exc:
        checks['database'] = 'unavailable'
        logger.warning(
            'Readiness database check failed (%s)', type(exc).__name__,
        )

    try:
        cache.set('healthcheck:readyz', 'ok', timeout=15)
        if cache.get('healthcheck:readyz') != 'ok':
            raise RuntimeError('cache probe returned an unexpected value')
        checks['cache'] = 'ok'
    except Exception as exc:
        checks['cache'] = 'unavailable'
        logger.warning(
            'Readiness cache check failed (%s)', type(exc).__name__,
        )

    if settings.STORAGE_BACKEND.lower() in OBJECT_STORAGE_BACKENDS:
        try:
            _probe_object_storage()
            checks['object_storage'] = 'ok'
        except Exception as exc:
            checks['object_storage'] = 'unavailable'
            logger.warning(
                'Readiness object storage check failed (%s)', type(exc).__name__,
            )

    if 'unavailable' in checks.values():
        return _json_response({
            'status': 'unavailable',
            'service': SERVICE_NAME,
            'checks': checks,
        }, status=503)

    return _json_response({
        'status': 'ok',
        'service': SERVICE_NAME,
        'checks': checks,
    })
