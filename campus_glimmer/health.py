"""Small deployment probes.

The two questions are deliberately separate: ``/healthz/`` proves that this
process can still handle a request, while ``/readyz/`` proves that its default
database can answer the smallest possible query before traffic is sent here.
"""

import logging

from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods


logger = logging.getLogger(__name__)
SERVICE_NAME = 'campus-glimmer'


def _json_response(payload, status=200):
    response = JsonResponse(payload, status=status)
    response.headers['Cache-Control'] = 'no-store'
    return response


@require_http_methods(['GET', 'HEAD'])
def health_check(request):
    """Return 200 when this Django process can handle requests."""
    return _json_response({
        'status': 'ok',
        'service': SERVICE_NAME,
    })


@require_http_methods(['GET', 'HEAD'])
def ready_check(request):
    """Return 503 when the default database cannot answer a minimal query."""
    try:
        with connections['default'].cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exc:
        logger.warning(
            'Readiness database check failed (%s)', type(exc).__name__,
        )
        return _json_response({
            'status': 'unavailable',
            'service': SERVICE_NAME,
            'checks': {'database': 'unavailable'},
        }, status=503)

    return _json_response({
        'status': 'ok',
        'service': SERVICE_NAME,
        'checks': {'database': 'ok'},
    })
