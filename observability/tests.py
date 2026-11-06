from datetime import timedelta
from io import StringIO
import json
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from campus_glimmer.alerts import AlertEngine, Rule
from observability.delivery import AlertDeliveryQueue
from observability.models import AlertNotificationDelivery


class _FakeProbe:
    def __init__(self, on=True):
        self.on = on

    def __call__(self, _context):
        return self.on, 1.0, '测试条件'


def _event(occurred_at, *, name='QueueRule', event='firing', state='firing'):
    return {
        'event': event,
        'name': name,
        'severity': 'critical',
        'summary': '测试告警',
        'state': state,
        'value': 1.0,
        'detail': '测试条件',
        'since': occurred_at,
        'occurred_at': occurred_at,
        'active_seconds': 0.0,
        'instance': 'test-instance',
    }


def _delivered():
    return {'delivered': 1, 'failed': 0, 'skipped': 0}


def _failed():
    return {'delivered': 0, 'failed': 1, 'skipped': 0}


class AlertDeliveryQueueTests(TestCase):
    def _queue(self, **overrides):
        values = {
            'max_attempts': 3,
            'retry_base_seconds': 10,
            'retry_max_seconds': 60,
            'dedupe_seconds': 3600,
            'lease_seconds': 120,
        }
        values.update(overrides)
        return AlertDeliveryQueue(**values)

    def test_immediate_delivery_persists_the_exact_event(self):
        now = timezone.now()
        event = _event(now.timestamp())
        notifier = MagicMock()
        notifier.deliver.return_value = _delivered()
        outcomes = self._queue().enqueue_and_deliver([event], notifier, now=now)

        row = AlertNotificationDelivery.objects.get()
        self.assertEqual(outcomes['delivered'], 1)
        self.assertEqual(row.status, AlertNotificationDelivery.DELIVERED)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.payload, event)
        self.assertIsNotNone(row.delivered_at)
        notifier.deliver.assert_called_once()

    def test_duplicate_transition_is_retried_only_after_backoff(self):
        now = timezone.now()
        event = _event(now.timestamp())
        notifier = MagicMock()
        notifier.deliver.side_effect = [_failed(), _delivered()]
        queue = self._queue(max_attempts=3)

        first = queue.enqueue_and_deliver([event], notifier, now=now)
        row = AlertNotificationDelivery.objects.get()
        self.assertEqual(first['failed'], 1)
        self.assertEqual(row.status, AlertNotificationDelivery.PENDING)
        self.assertEqual(row.next_attempt_at, now + timedelta(seconds=10))

        duplicate = queue.enqueue_and_deliver([event], notifier, now=now + timedelta(seconds=1))
        self.assertEqual(duplicate['deduplicated'], 1)
        self.assertEqual(notifier.deliver.call_count, 1)

        retried = queue.enqueue_and_deliver(
            [event], notifier, now=now + timedelta(seconds=10),
        )
        row.refresh_from_db()
        self.assertEqual(retried['delivered'], 1)
        self.assertEqual(row.status, AlertNotificationDelivery.DELIVERED)
        self.assertEqual(row.attempts, 2)

    def test_retry_budget_exhausts_without_reactivating_a_terminal_row(self):
        now = timezone.now()
        event = _event(now.timestamp())
        notifier = MagicMock()
        notifier.deliver.return_value = _failed()
        queue = self._queue(max_attempts=2)

        queue.enqueue_and_deliver([event], notifier, now=now)
        second = queue.enqueue_and_deliver(
            [event], notifier, now=now + timedelta(seconds=10),
        )
        row = AlertNotificationDelivery.objects.get()
        self.assertEqual(second['failed'], 1)
        self.assertEqual(row.status, AlertNotificationDelivery.EXHAUSTED)
        self.assertEqual(row.attempts, 2)

        later = queue.enqueue_and_deliver(
            [event], notifier, now=now + timedelta(seconds=30),
        )
        self.assertEqual(later['deduplicated'], 1)
        self.assertEqual(notifier.deliver.call_count, 2)

    def test_snapshot_and_pruning_keep_the_ledger_bounded(self):
        now = timezone.now()
        event = _event(now.timestamp())
        pending = AlertNotificationDelivery.objects.create(
            dedupe_key='pending', event='firing', rule_name='Pending', state='firing',
            payload=event, status=AlertNotificationDelivery.PENDING,
            next_attempt_at=now, attempts=1,
        )
        delivered = AlertNotificationDelivery.objects.create(
            dedupe_key='delivered', event='firing', rule_name='Delivered', state='firing',
            payload=event, status=AlertNotificationDelivery.DELIVERED,
            next_attempt_at=now, delivered_at=now, attempts=1,
        )
        AlertNotificationDelivery.objects.filter(pk=delivered.pk).update(
            updated_at=now - timedelta(days=40),
        )
        snapshot = self._queue().snapshot(now=now)
        self.assertEqual(snapshot['status'][AlertNotificationDelivery.PENDING], 1)
        self.assertEqual(snapshot['status'][AlertNotificationDelivery.DELIVERED], 1)
        self.assertEqual(snapshot['due'], 1)

        deleted = self._queue().prune_terminal(before=now - timedelta(days=30))
        self.assertEqual(deleted, 1)
        self.assertTrue(AlertNotificationDelivery.objects.filter(pk=pending.pk).exists())
        self.assertFalse(AlertNotificationDelivery.objects.filter(pk=delivered.pk).exists())


class AlertDeliveryEngineIntegrationTests(TestCase):
    def test_two_engines_collapse_the_same_transition_into_one_delivery(self):
        now = timezone.now().timestamp()
        notifier = MagicMock()
        notifier.deliver.return_value = _delivered()
        queue = AlertDeliveryQueue(
            max_attempts=3, retry_base_seconds=10, retry_max_seconds=60,
            dedupe_seconds=60, lease_seconds=120,
        )
        first = AlertEngine(
            rules=[Rule('Shared', 'critical', '同一故障', 0, _FakeProbe())],
            notifier=notifier,
            delivery_queue=queue,
        )
        second = AlertEngine(
            rules=[Rule('Shared', 'critical', '同一故障', 0, _FakeProbe())],
            notifier=notifier,
            delivery_queue=queue,
        )

        first.evaluate([], now=now)
        second.evaluate([], now=now + 1.0)

        self.assertEqual(AlertNotificationDelivery.objects.count(), 1)
        self.assertEqual(notifier.deliver.call_count, 1)
        self.assertEqual(second.counters()['notifications']['deduplicated'], 1)

    def test_queue_failure_falls_back_to_direct_delivery_and_degrades_snapshot(self):
        class BrokenQueue:
            def enqueue_and_deliver(self, *_args, **_kwargs):
                raise OperationalError('outbox unavailable')

            def snapshot(self):
                raise OperationalError('outbox unavailable')

        notifier = MagicMock()
        notifier.deliver.return_value = _delivered()
        engine = AlertEngine(
            rules=[Rule('Fallback', 'critical', '降级测试', 0, _FakeProbe())],
            notifier=notifier,
            delivery_queue=BrokenQueue(),
        )
        with self.assertLogs('campus_glimmer.alerts', level='WARNING'):
            state = engine.evaluate([], now=timezone.now().timestamp())
            snapshot = engine.delivery_snapshot()

        self.assertEqual(state[0]['state'], 'firing')
        self.assertEqual(engine.counters()['notifications']['delivered'], 1)
        notifier.deliver.assert_called_once()
        self.assertTrue(snapshot['enabled'])
        self.assertTrue(snapshot['degraded'])


class FlushAlertNotificationsCommandTests(TestCase):
    @override_settings(ALERT_WEBHOOK_URL='https://alerts.example.test/hook')
    def test_command_flushes_due_rows_and_prunes_old_terminal_rows(self):
        now = timezone.now()
        event = _event(now.timestamp())
        due = AlertNotificationDelivery.objects.create(
            dedupe_key='due', event='firing', rule_name='Due', state='firing',
            payload=event, status=AlertNotificationDelivery.PENDING,
            next_attempt_at=now - timedelta(seconds=1), attempts=0,
        )
        old = AlertNotificationDelivery.objects.create(
            dedupe_key='old', event='firing', rule_name='Old', state='firing',
            payload=event, status=AlertNotificationDelivery.DELIVERED,
            next_attempt_at=now, delivered_at=now, attempts=1,
        )
        AlertNotificationDelivery.objects.filter(pk=old.pk).update(
            updated_at=now - timedelta(days=2),
        )
        notifier = MagicMock()
        notifier.deliver.return_value = _delivered()
        stdout = StringIO()

        with patch(
            'observability.management.commands.flush_alert_notifications.AlertNotifier.from_settings',
            return_value=notifier,
        ):
            call_command(
                'flush_alert_notifications', '--limit', '10', '--prune-days', '1',
                stdout=stdout,
            )

        due.refresh_from_db()
        result = json.loads(stdout.getvalue())
        self.assertEqual(due.status, AlertNotificationDelivery.DELIVERED)
        self.assertEqual(result['delivered'], 1)
        self.assertEqual(result['pruned'], 1)
        self.assertFalse(AlertNotificationDelivery.objects.filter(pk=old.pk).exists())

    @override_settings(ALERT_WEBHOOK_URL='')
    def test_command_refuses_to_run_without_a_webhook(self):
        with self.assertRaises(CommandError):
            call_command('flush_alert_notifications', stdout=StringIO())
