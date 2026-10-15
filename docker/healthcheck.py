"""Container readiness probe for the Django application."""

import os
import sys
import urllib.request


READY_URL_TEMPLATE = 'http://127.0.0.1:{port}/readyz/'


def is_ready() -> bool:
    port = os.getenv('PORT', '8000')
    url = READY_URL_TEMPLATE.format(port=port)
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return response.status == 200
    except Exception:
        print('readiness check failed', file=sys.stderr)
        return False


if __name__ == '__main__':
    sys.exit(0 if is_ready() else 1)
