"""Container health probes for the Django application.

Two distinct signals exist on purpose:

* liveness  -> ``/healthz/``  : the process can still serve HTTP. No database,
  cache or object-storage access, so a transient dependency hiccup cannot make
  the orchestrator restart every worker.
* readiness -> ``readyz/``    : the process can serve *useful* traffic. This
  touches PostgreSQL, Redis and object storage, so it is what a load balancer
  or a deploy script should gate on -- not ``HEALTHCHECK``.
"""

import argparse
import os
import sys
import urllib.request

TIMEOUT_SECONDS = 4

LIVENESS_URL_TEMPLATE = 'http://127.0.0.1:{port}/healthz/'
READINESS_URL_TEMPLATE = 'http://127.0.0.1:{port}/readyz/'


def _probe(url_template, label):
    port = os.getenv('PORT', '8000')
    url = url_template.format(port=port)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return response.status == 200
    except Exception:
        # Never echo the exception: probe output lands in container logs.
        print(f'{label} check failed', file=sys.stderr)
        return False


def is_alive() -> bool:
    """Liveness: the HTTP stack is answering."""
    return _probe(LIVENESS_URL_TEMPLATE, 'liveness')


def is_ready() -> bool:
    """Readiness: every backing service is reachable."""
    return _probe(READINESS_URL_TEMPLATE, 'readiness')


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='healthcheck.py',
        description='Container HEALTHCHECK probe. Defaults to liveness.',
    )
    parser.add_argument(
        '--liveness',
        action='store_true',
        help='probe /healthz/ (process only; safe as HEALTHCHECK)',
    )
    parser.add_argument(
        '--readiness',
        action='store_true',
        help='probe /readyz/ (touches PostgreSQL, Redis and object storage)',
    )
    args = parser.parse_args(argv)
    if args.liveness and args.readiness:
        parser.error('--liveness and --readiness are mutually exclusive')
    return args


def main(argv=None):
    options = parse_args(sys.argv[1:] if argv is None else argv)
    probe = is_ready if options.readiness else is_alive
    return 0 if probe() else 1


if __name__ == '__main__':
    sys.exit(main())