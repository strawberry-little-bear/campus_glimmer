import json
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
