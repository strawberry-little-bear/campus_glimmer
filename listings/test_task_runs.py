# -*- coding: utf-8 -*-
"""Cover the task run ledger and the health aggregation built from it.

The important cases are the ones the ledger exists for. A run that raises must
still leave a row behind, otherwise the one situation worth recording is the
one that gets lost, and the exception must keep propagating so a broken command
still fails loudly instead of being quietly absorbed. A dry run must be
recorded but excluded from the success rate, because a run that sends nothing
cannot vouch for the schedule.
"""

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import AcademicTerm, TaskRun
from .task_run_health import build_task_run_health
from .task_runs import METRIC_FIELDS, normalise_metrics, record_task_run


class NormaliseMetricsTests(TestCase):
    """Only comparable scalars survive the mapping."""

    def test_keeps_known_numeric_fields(self):
        result = {'sent': 3, 'skipped': 1, 'failed': 0, 'marked': 2, 'expired': 5, 'reminded': 4}
        self.assertEqual(normalise_metrics(result), result)

    def test_drops_unknown_keys(self):
        # A command may return anything; anything that does not map onto a
        # comparable scalar is left in the command's own output.
        result = {'sent': 3, 'reason': 'no_alerts', 'alerts': 7, 'quiet': 2}
        self.assertEqual(normalise_metrics(result), {'sent': 3})

    def test_rejects_booleans(self):
        # True stored as 1 would be indistinguishable from a genuine count.
        self.assertEqual(normalise_metrics({'sent': True, 'failed': False}), {})

    def test_rejects_non_dict(self):
        self.assertEqual(normalise_metrics(None), {})
        self.assertEqual(normalise_metrics([1, 2, 3]), {})

    def test_rejects_non_numeric_values(self):
        self.assertEqual(normalise_metrics({'sent': '3', 'skipped': None}), {})


class RecordTaskRunTests(TestCase):
    """The context manager must record both outcomes and stay out of the way."""

    def test_records_a_successful_run_with_metrics(self):
        with record_task_run('expire_items') as run:
            run.metrics = normalise_metrics({'expired': 4})

        row = TaskRun.objects.get()
        self.assertEqual(row.name, 'expire_items')
        self.assertEqual(row.status, 'succeeded')
        self.assertEqual(row.metrics, {'expired': 4})
        self.assertEqual(row.note, '')
        self.assertGreaterEqual(row.duration_ms, 0)
        self.assertLessEqual(row.started_at, row.finished_at)

    def test_records_a_failed_run_and_reraises(self):
        with self.assertRaises(CommandError):
            with record_task_run('expire_items') as run:
                run.metrics = normalise_metrics({'expired': 1})
                raise CommandError('调度命令炸了')

        row = TaskRun.objects.get()
        self.assertEqual(row.status, 'failed')
        # The evidence survives, and the exception is not swallowed.
        self.assertIn('CommandError', row.note)
        self.assertIn('调度命令炸了', row.note)
        self.assertEqual(row.metrics, {'expired': 1})

    def test_dry_run_is_marked_not_counted_as_a_normal_run(self):
        with record_task_run('send_lifecycle_reminders', dry_run=True) as run:
            run.metrics = normalise_metrics({'sent': 9})

        row = TaskRun.objects.get()
        self.assertEqual(row.status, 'succeeded')
        self.assertEqual(row.note, 'dry-run')
        self.assertEqual(row.metrics, {'sent': 9})

    def test_long_error_note_is_truncated(self):
        with self.assertRaises(ValueError):
            with record_task_run('expire_items'):
                raise ValueError('长' * 400)

        self.assertEqual(len(TaskRun.objects.get().note), 255)

    def test_metric_fields_list_is_stable(self):
        self.assertEqual(METRIC_FIELDS, ('sent', 'skipped', 'failed', 'marked', 'expired', 'reminded'))


class TaskRunHealthTests(TestCase):
    """Aggregation signals a single row cannot show on its own."""

    def make_run(self, name, status='succeeded', minutes_ago=0, metrics=None, note=''):
        started = timezone.now() - timedelta(minutes=minutes_ago)
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=started,
            finished_at=started,
            duration_ms=120,
            metrics=metrics or {},
            note=note,
        )

    def test_no_runs_reports_nothing_to_worry_about_yet(self):
        data = build_task_run_health(days=7)
        self.assertEqual(data['total_runs'], 0)
        self.assertEqual(data['success_rate'], None)
        self.assertEqual(data['commands'][0]['run_count'], 0)
        self.assertTrue(data['commands'][0]['is_stale'])

    def test_success_rate_and_totals(self):
        self.make_run('expire_items', metrics={'expired': 3})
        self.make_run('expire_items', metrics={'expired': 2})
        self.make_run('send_operations_digest', status='failed', metrics={'failed': 1})

        data = build_task_run_health(days=7)
        self.assertEqual(data['total_runs'], 3)
        self.assertEqual(data['total_failed'], 1)
        self.assertEqual(round(data['success_rate'], 4), round(2 / 3, 4))

    def test_consecutive_failures_counts_back_to_the_last_success(self):
        self.make_run('expire_items', status='succeeded', minutes_ago=300)
        self.make_run('expire_items', status='failed', minutes_ago=30)
        self.make_run('expire_items', status='failed', minutes_ago=10)

        row = {r['name']: r for r in build_task_run_health(days=7)['commands']}['expire_items']
        self.assertEqual(row['consecutive_failures'], 2)
        self.assertEqual(row['last_status'], 'failed')

    def test_dry_runs_are_excluded_from_the_statistics(self):
        self.make_run('send_lifecycle_reminders', metrics={'sent': 4}, note='dry-run')

        data = build_task_run_health(days=7)
        self.assertEqual(data['total_runs'], 0)
        self.assertIn('没有已记录的运行', data['summary'])

    def test_staleness_uses_the_last_successful_run(self):
        # A failing command still runs; silence must be measured from success,
        # otherwise five failures in a row would look perfectly healthy.
        self.make_run('expire_items', status='failed', minutes_ago=10)
        self.make_run('expire_items', status='succeeded', minutes_ago=60 * 24 * 5)

        row = {r['name']: r for r in build_task_run_health(days=7)['commands']}['expire_items']
        self.assertGreater(row['silence_days'], 3)
        self.assertTrue(row['is_stale'])
        self.assertIn('没有成功运行', ' '.join(build_task_run_health(days=7)['recommendations']))

    def test_recommendations_name_the_offending_commands(self):
        self.make_run('expire_items', status='failed')
        self.make_run('expire_items', status='failed')
        self.make_run('expire_items', status='failed')
        self.make_run('send_operations_digest', status='succeeded')

        recommendations = ' '.join(build_task_run_health(days=7)['recommendations'])
        self.assertIn('expire_items', recommendations)
        self.assertNotIn('send_operations_digest', recommendations)

    def test_metrics_are_summed_across_runs(self):
        self.make_run('expire_items', metrics={'expired': 3})
        self.make_run('expire_items', metrics={'expired': 4})

        row = {r['name']: r for r in build_task_run_health(days=7)['commands']}['expire_items']
        self.assertEqual(row['metrics_total'], {'expired': 7})
        self.assertEqual(row['avg_duration_ms'], 120)

    def test_older_runs_fall_outside_the_period(self):
        self.make_run('expire_items', minutes_ago=60 * 24 * 30)
        self.assertEqual(build_task_run_health(days=7)['total_runs'], 0)
        self.assertEqual(build_task_run_health(days=365)['total_runs'], 1)


class CommandLedgerTests(TestCase):
    """Real commands must leave a row behind, including when they blow up."""

    def test_expire_items_records_a_run(self):
        call_command('expire_items')
        row = TaskRun.objects.get()
        self.assertEqual(row.name, 'expire_items')
        self.assertEqual(row.status, 'succeeded')
        self.assertIn('expired', row.metrics)

    def test_dry_run_lifecycle_reminders_is_marked(self):
        call_command('send_lifecycle_reminders', '--dry-run')
        row = TaskRun.objects.get()
        self.assertEqual(row.note, 'dry-run')
        self.assertEqual(row.status, 'succeeded')

    def test_order_timeouts_merges_both_services_into_one_run(self):
        call_command('process_order_timeouts')
        row = TaskRun.objects.get()
        self.assertEqual(row.name, 'process_order_timeouts')
        self.assertEqual(row.status, 'succeeded')
        self.assertIn('reminded', row.metrics)
        self.assertIn('expired', row.metrics)

    def test_sync_academic_calendar_records_a_run(self):
        AcademicTerm.objects.create(
            name='台账秋季学期', slug='ledger-autumn', kind='autumn',
            starts_on=timezone.now().date() - timedelta(days=20),
            ends_on=timezone.now().date() + timedelta(days=100),
        )
        call_command('sync_academic_calendar')
        row = TaskRun.objects.get()
        self.assertEqual(row.name, 'sync_academic_calendar')
        self.assertEqual(row.status, 'succeeded')
        # 台账只认可比较的标量，这里把新增阶段与刷新快照合成一个处理量。
        self.assertIn('marked', row.metrics)

    def test_command_that_returns_early_still_records_a_run(self):
        # 没有启用中的学期时命令会提前返回，提前返回也是一次真实的运行。
        call_command('sync_academic_calendar')
        row = TaskRun.objects.get()
        self.assertEqual(row.name, 'sync_academic_calendar')
        self.assertEqual(row.status, 'succeeded')

    def test_operations_digest_records_a_run(self):
        call_command('send_operations_digest')
        row = TaskRun.objects.get()
        self.assertEqual(row.name, 'send_operations_digest')
        self.assertEqual(row.status, 'succeeded')
