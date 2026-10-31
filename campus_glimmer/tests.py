import importlib.util
import io
import json
from pathlib import Path
import os
import subprocess
import sys
from unittest.mock import Mock, patch

from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .health import _probe_object_storage


class HealthCheckTests(SimpleTestCase):
    def test_health_check_returns_stable_json_without_database(self):
        with patch('campus_glimmer.health.connections') as connections:
            response = self.client.get(reverse('health_check'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(
            json.loads(response.content),
            {'status': 'ok', 'service': 'campus-glimmer'},
        )
        connections['default'].cursor.assert_not_called()


class ReadyCheckTests(TestCase):
    def test_ready_check_reports_database_and_cache_as_ok(self):
        response = self.client.get(reverse('ready_check'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(
            json.loads(response.content),
            {
                'status': 'ok',
                'service': 'campus-glimmer',
                'checks': {'database': 'ok', 'cache': 'ok'},
            },
        )

    def test_ready_check_returns_503_without_leaking_database_error(self):
        error = OperationalError('database is locked: /tmp/secret.sqlite3')
        with patch('campus_glimmer.health.connections') as connections:
            connections['default'].cursor.side_effect = error
            with self.assertLogs('campus_glimmer.health', level='WARNING') as logged:
                response = self.client.get(reverse('ready_check'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        payload = json.loads(response.content)
        self.assertEqual(
            payload,
            {
                'status': 'unavailable',
                'service': 'campus-glimmer',
                'checks': {'database': 'unavailable', 'cache': 'ok'},
            },
        )
        self.assertNotIn('secret.sqlite3', response.content.decode())
        self.assertNotIn('database is locked', response.content.decode())
        self.assertIn('OperationalError', '\n'.join(logged.output))
        self.assertNotIn('secret.sqlite3', '\n'.join(logged.output))

    def test_ready_check_returns_503_when_cache_is_unavailable(self):
        with patch('campus_glimmer.health.cache') as cache_backend:
            cache_backend.set.side_effect = ConnectionError('redis host unavailable')
            with self.assertLogs('campus_glimmer.health', level='WARNING') as logged:
                response = self.client.get(reverse('ready_check'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(response.content)['checks'],
            {'database': 'ok', 'cache': 'unavailable'},
        )
        self.assertIn('ConnectionError', '\n'.join(logged.output))
        self.assertNotIn('redis host unavailable', response.content.decode())

    @override_settings(STORAGE_BACKEND='s3')
    def test_ready_check_includes_object_storage_when_s3_is_enabled(self):
        with patch('campus_glimmer.health._probe_object_storage') as probe:
            response = self.client.get(reverse('ready_check'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content)['checks'],
            {'database': 'ok', 'cache': 'ok', 'object_storage': 'ok'},
        )
        probe.assert_called_once_with()

    @override_settings(STORAGE_BACKEND='s3')
    def test_object_storage_probe_uses_the_boto_client(self):
        storage = Mock()
        storage.bucket_name = 'campus-glimmer-test'
        with patch(
            'django.core.files.storage.storages',
            {'default': storage},
        ):
            _probe_object_storage()

        storage.connection.meta.client.head_bucket.assert_called_once_with(
            Bucket='campus-glimmer-test',
        )

    @override_settings(STORAGE_BACKEND='minio')
    def test_ready_check_returns_503_when_object_storage_is_unavailable(self):
        with patch(
            'campus_glimmer.health._probe_object_storage',
            side_effect=PermissionError('bucket secret'),
        ):
            with self.assertLogs('campus_glimmer.health', level='WARNING') as logged:
                response = self.client.get(reverse('ready_check'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(response.content)['checks'],
            {
                'database': 'ok',
                'cache': 'ok',
                'object_storage': 'unavailable',
            },
        )
        self.assertIn('PermissionError', '\n'.join(logged.output))
        self.assertNotIn('bucket secret', response.content.decode())


class ContainerHealthcheckTests(SimpleTestCase):
    def load_healthcheck(self):
        path = Path(__file__).resolve().parents[1] / 'docker' / 'healthcheck.py'
        spec = importlib.util.spec_from_file_location('docker_healthcheck', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_container_healthcheck_accepts_ready_endpoint(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200

            self.assertTrue(module.is_ready())

        urlopen.assert_called_once_with('http://127.0.0.1:8000/readyz/', timeout=4)

    def test_container_healthcheck_rejects_unready_endpoint(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 503

            self.assertFalse(module.is_ready())

    def test_container_healthcheck_hides_probe_error_details(self):
        module = self.load_healthcheck()
        with patch.object(
            module.urllib.request,
            'urlopen',
            side_effect=OSError('secret connection failure'),
        ), patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertFalse(module.is_ready())

        self.assertIn('readiness check failed', stderr.getvalue())
        self.assertNotIn('secret connection failure', stderr.getvalue())

    def test_container_healthcheck_liveness_does_not_touch_dependencies(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200

            self.assertTrue(module.is_alive())

        urlopen.assert_called_once_with('http://127.0.0.1:8000/healthz/', timeout=4)

    def test_container_healthcheck_liveness_reports_failure(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 503

            self.assertFalse(module.is_alive())

    def test_container_healthcheck_liveness_and_readiness_use_different_endpoints(self):
        module = self.load_healthcheck()
        self.assertNotEqual(module.LIVENESS_URL_TEMPLATE, module.READINESS_URL_TEMPLATE)

        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200

            module.is_alive()
            module.is_ready()

        self.assertEqual(
            [call.args[0] for call in urlopen.call_args_list],
            ['http://127.0.0.1:8000/healthz/', 'http://127.0.0.1:8000/readyz/'],
        )

    def test_container_healthcheck_hides_liveness_error_details(self):
        module = self.load_healthcheck()
        with patch.object(
            module.urllib.request,
            'urlopen',
            side_effect=OSError('secret connection failure'),
        ), patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertFalse(module.is_alive())

        self.assertIn('liveness check failed', stderr.getvalue())
        self.assertNotIn('secret connection failure', stderr.getvalue())

    def test_container_healthcheck_defaults_to_liveness(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200

            self.assertEqual(module.main([]), 0)

        urlopen.assert_called_once_with('http://127.0.0.1:8000/healthz/', timeout=4)

    def test_container_healthcheck_liveness_failure_exits_nonzero(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 503

            self.assertEqual(module.main([]), 1)

    def test_container_healthcheck_readiness_mode_probes_ready_endpoint(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200

            self.assertEqual(module.main(['--readiness']), 0)

        urlopen.assert_called_once_with('http://127.0.0.1:8000/readyz/', timeout=4)

    def test_container_healthcheck_readiness_mode_failure_exits_nonzero(self):
        module = self.load_healthcheck()
        with patch.object(module.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 503

            self.assertEqual(module.main(['--readiness']), 1)

        self.assertEqual(urlopen.call_args.args[0], 'http://127.0.0.1:8000/readyz/')

    def test_container_healthcheck_rejects_unknown_mode(self):
        module = self.load_healthcheck()
        with patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                module.parse_args(['--bogus'])

        self.assertNotEqual(caught.exception.code, 0)

    def test_container_healthcheck_rejects_conflicting_modes(self):
        module = self.load_healthcheck()
        with patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                module.parse_args(['--liveness', '--readiness'])

        self.assertNotEqual(caught.exception.code, 0)

    def test_container_healthcheck_cli_liveness_fails_on_dead_port(self):
        # End-to-end: the exact command the Dockerfile HEALTHCHECK runs must exit
        # non-zero when nothing is listening, otherwise the probe is decorative.
        script = Path(__file__).resolve().parents[1] / 'docker' / 'healthcheck.py'
        environment = os.environ.copy()
        environment['PORT'] = '45999'
        result = subprocess.run(
            [sys.executable, str(script), '--liveness'],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn('liveness check failed', result.stderr)

    def test_container_healthcheck_cli_rejects_unknown_flag(self):
        script = Path(__file__).resolve().parents[1] / 'docker' / 'healthcheck.py'
        result = subprocess.run(
            [sys.executable, str(script), '--bogus'],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unrecognized arguments', result.stderr)


class SecurityResponseBaselineTests(SimpleTestCase):
    def assert_security_baseline(self, response):
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')
        self.assertEqual(response.headers['Cross-Origin-Opener-Policy'], 'same-origin')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

    def test_security_baseline_applies_to_json_health_response(self):
        response = self.client.get(reverse('health_check'))

        self.assertEqual(response.status_code, 200)
        self.assert_security_baseline(response)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Strict-Transport-Security', response.headers)

    def test_hsts_is_explicit_and_complete_when_configured(self):
        with override_settings(
            SECURE_HSTS_SECONDS=31_536_000,
            SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
            SECURE_HSTS_PRELOAD=True,
        ):
            response = self.client.get(reverse('health_check'), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assert_security_baseline(response)
        self.assertEqual(
            response.headers['Strict-Transport-Security'],
            'max-age=31536000; includeSubDomains; preload',
        )

    def test_cookies_are_not_readable_from_scripts(self):
        from django.conf import settings

        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)

    def test_invalid_hsts_values_fall_back_to_disabled(self):
        from campus_glimmer.settings import _hsts_seconds

        self.assertEqual(_hsts_seconds('not-a-number'), 0)
        self.assertEqual(_hsts_seconds('-10'), 0)
        with patch('campus_glimmer.settings.os.getenv', return_value='broken'):
            self.assertEqual(_hsts_seconds(None), 0)
