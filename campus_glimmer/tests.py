import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch

from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse


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
    def test_ready_check_reports_database_as_ok(self):
        response = self.client.get(reverse('ready_check'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(
            json.loads(response.content),
            {
                'status': 'ok',
                'service': 'campus-glimmer',
                'checks': {'database': 'ok'},
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
                'checks': {'database': 'unavailable'},
            },
        )
        self.assertNotIn('secret.sqlite3', response.content.decode())
        self.assertNotIn('database is locked', response.content.decode())
        self.assertIn('OperationalError', '\n'.join(logged.output))
        self.assertNotIn('secret.sqlite3', '\n'.join(logged.output))


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
