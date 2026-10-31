# -*- coding: utf-8 -*-
"""One-shot startup tasks shared by the web entrypoint and the migrate job.

Schema migration and ``collectstatic`` must run exactly once per release, not
once per Gunicorn worker or per scaled-out replica. Both steps mutate shared
state that concurrent execution corrupts:

* concurrent ``migrate`` runs can race on the same migration lock;
* ``CompressedManifestStaticFilesStorage`` rewrites the ``staticfiles.json``
  manifest, so parallel ``collectstatic`` runs can leave web workers serving a
  half-written hash index.

The production compose topology therefore runs this module as a dedicated
short-lived ``migrate`` service and lets ``web`` wait for it. The container
entrypoint keeps calling the same functions so a bare ``docker run`` still
boots a working instance.

Usage::

    python docker/startup_tasks.py            # migrate + collectstatic
    python docker/startup_tasks.py --migrate-only
"""

import sys

import django
from django.apps import apps
from django.core.management import call_command

DEFAULT_VERBOSITY = 1


def _ensure_django_configured():
    """Load the app registry when this file is run as a standalone script.

    ``manage.py`` and the test runner call ``django.setup()`` themselves;
    a bare ``python docker/startup_tasks.py`` does not, and without the
    registry every management command dies with ``AppRegistryNotReady``.
    The guard keeps this idempotent so importing the module inside an
    already-configured process is a no-op.
    """
    if not apps.ready:
        django.setup()


def run_startup_tasks(*, collect_static=True, verbosity=DEFAULT_VERBOSITY, stdout=None):
    """Apply pending migrations and refresh the static manifest.

    Returns the names of the management commands that actually ran, so callers
    and tests can assert the real work instead of re-parsing log output.
    """
    _ensure_django_configured()
    stream = sys.stdout if stdout is None else stdout
    executed = []

    call_command('migrate', '--noinput', verbosity=verbosity, stdout=stream)
    executed.append('migrate')

    if collect_static:
        call_command('collectstatic', '--noinput', verbosity=verbosity, stdout=stream)
        executed.append('collectstatic')

    return executed


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    collect_static = '--migrate-only' not in argv
    run_startup_tasks(collect_static=collect_static)
    return 0


if __name__ == '__main__':
    sys.exit(main())
