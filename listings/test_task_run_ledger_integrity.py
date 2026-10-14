# -*- coding: utf-8 -*-
"""定时任务台账完整性审计的口径测试。"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from .models import TaskRun
from .task_run_ledger_integrity import (
    ISSUE_ROW_LIMIT,
    ISSUE_ORDER,
    _time_issues,
    build_task_run_ledger_integrity,
)


def local_noon():
    return timezone.localtime().replace(
        hour=12, minute=0, second=0, microsecond=0,
    )


class TaskRunLedgerIntegrityTests(TestCase):
    def setUp(self):
        self.now = local_noon()

    def make_run(
        self, *, name='send_lifecycle_reminders', status='succeeded',
        started_minutes_ago=10, finished_minutes_ago=9,
        duration_ms=60000, metrics=None, note='',
    ):
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=self.now - timedelta(minutes=started_minutes_ago),
            finished_at=self.now - timedelta(minutes=finished_minutes_ago),
            duration_ms=duration_ms,
            metrics={} if metrics is None else metrics,
            note=note,
        )

    def report(self, days=30):
        return build_task_run_ledger_integrity(days=days, now=self.now)

    def issue_types(self, report):
        return {
            row['key']: row['row_count']
            for row in report['issue_type_rows']
        }

    def test_empty_ledger_keeps_the_fixed_shape(self):
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['summary']['run_total'], 0)
        self.assertIsNone(report['summary']['issue_row_share'])
        self.assertIsNone(report['summary']['metric_readable_share'])
        self.assertEqual([row['key'] for row in report['issue_type_rows']], list(ISSUE_ORDER))
        self.assertEqual(report['command_rows'], [])
        self.assertEqual(report['issue_rows'], [])
        self.assertEqual(report['issue_row_hidden_total'], 0)

    def test_clean_success_failure_and_dry_run_rows_are_not_issues(self):
        self.make_run(metrics={'sent': 2, 'skipped': 1})
        self.make_run(
            status='failed', started_minutes_ago=30,
            finished_minutes_ago=29, duration_ms=60000,
            metrics={}, note='ValueError: bad config',
        )
        self.make_run(
            started_minutes_ago=50, finished_minutes_ago=49,
            duration_ms=60000, metrics={}, note='dry-run',
        )

        report = self.report()

        self.assertTrue(report['has_data'])
        self.assertEqual(report['summary']['run_total'], 3)
        self.assertEqual(report['summary']['issue_row_total'], 0)
        self.assertEqual(report['summary']['clean_row_total'], 3)
        self.assertEqual(report['summary']['metric_required_total'], 1)
        self.assertEqual(report['summary']['metric_readable_total'], 1)
        self.assertEqual(report['summary']['metric_readable_share'], 100.0)

    def test_finish_before_start_is_reported(self):
        self.make_run(
            started_minutes_ago=10, finished_minutes_ago=11,
            duration_ms=0, metrics={'sent': 1},
        )

        report = self.report()

        self.assertEqual(report['summary']['issue_row_total'], 1)
        self.assertIn('finish_before_start', report['issue_rows'][0]['issue_keys'])

    def test_future_timestamp_is_reported_without_small_clock_drift(self):
        self.make_run(
            started_minutes_ago=1, finished_minutes_ago=-2,
            duration_ms=180000, metrics={'sent': 1},
        )

        report = self.report()

        self.assertEqual(report['issue_rows'][0]['issue_keys'], ['future_time'])

    def test_negative_duration_is_recognised_before_reaching_the_database(self):
        # SQLite 会执行 PositiveIntegerField 的 CHECK 约束，负数无法通过 ORM
        # 落库；这里直接测审计函数，覆盖数据库约束之外的防口径。
        issues = _time_issues(
            {
                'started_at': self.now - timedelta(minutes=1),
                'finished_at': self.now,
                'duration_ms': -1,
            },
            self.now,
        )

        self.assertIn('negative_duration', issues)

    def test_only_a_serious_duration_mismatch_is_reported(self):
        clean = self.make_run(
            started_minutes_ago=10, finished_minutes_ago=9,
            duration_ms=61000, metrics={'sent': 1},
        )
        self.assertIsNotNone(clean)
        self.make_run(
            started_minutes_ago=30, finished_minutes_ago=20,
            duration_ms=1, metrics={'sent': 1},
        )

        report = self.report()

        self.assertEqual(report['summary']['issue_row_total'], 1)
        self.assertEqual(report['issue_rows'][0]['issue_keys'], ['duration_mismatch'])

    def test_unknown_status_and_missing_failure_note_are_reported(self):
        self.make_run(status='cancelled', metrics={'sent': 1})
        self.make_run(
            status='failed', started_minutes_ago=20,
            finished_minutes_ago=19, metrics={}, note='  ',
        )

        report = self.report()
        counts = self.issue_types(report)

        self.assertEqual(counts['invalid_status'], 1)
        self.assertEqual(counts['missing_failure_note'], 1)
        self.assertEqual(report['summary']['issue_row_total'], 2)

    def test_successful_non_dry_run_requires_readable_metrics(self):
        self.make_run(metrics={})

        report = self.report()

        self.assertEqual(report['summary']['metric_required_total'], 1)
        self.assertEqual(report['summary']['metric_readable_total'], 0)
        self.assertEqual(report['summary']['metric_readable_share'], 0.0)
        self.assertIn('invalid_metrics', report['issue_rows'][0]['issue_keys'])

    def test_boolean_negative_and_nested_metric_values_are_invalid(self):
        self.make_run(metrics={'sent': True})
        self.make_run(
            started_minutes_ago=20, finished_minutes_ago=19,
            metrics={'sent': -1},
        )
        self.make_run(
            started_minutes_ago=30, finished_minutes_ago=29,
            metrics={'sent': {'total': 1}},
        )

        report = self.report()

        self.assertEqual(report['summary']['issue_row_total'], 3)
        self.assertEqual(report['summary']['metric_readable_total'], 0)
        row = report['command_rows'][0]
        self.assertEqual(row['metric_issue_row_count'], 3)
        self.assertEqual(row['invalid_metric_value_count'], 3)

    def test_unknown_metric_keys_are_counted_but_do_not_make_a_row_invalid(self):
        self.make_run(metrics={'sent': 1, 'future_count': 2})

        report = self.report()

        self.assertEqual(report['summary']['issue_row_total'], 0)
        self.assertEqual(report['summary']['unknown_metric_key_total'], 1)
        self.assertEqual(report['command_rows'][0]['unknown_metric_key_count'], 1)
        self.assertEqual(report['summary']['metric_readable_share'], 100.0)

    def test_unknown_command_is_listed_without_being_an_issue(self):
        self.make_run(name='future_command', metrics={'sent': 1})

        report = self.report()

        self.assertEqual(report['summary']['unknown_command_total'], 1)
        self.assertEqual(report['summary']['known_command_total'], 0)
        self.assertEqual(report['summary']['issue_row_total'], 0)
        self.assertFalse(report['command_rows'][0]['is_known'])

    def test_issue_list_is_limited_and_hidden_count_keeps_the_total(self):
        for index in range(ISSUE_ROW_LIMIT + 3):
            self.make_run(
                started_minutes_ago=index + 1,
                finished_minutes_ago=index,
                duration_ms=1,
                metrics={'sent': 1},
            )

        report = self.report()

        self.assertEqual(len(report['issue_rows']), ISSUE_ROW_LIMIT)
        self.assertEqual(report['issue_row_hidden_total'], 3)
        self.assertEqual(report['summary']['issue_row_total'], ISSUE_ROW_LIMIT + 3)

    def test_invalid_period_falls_back_and_the_module_is_read_only(self):
        run = self.make_run(metrics={'sent': 1})
        before = list(TaskRun.objects.order_by('id').values(
            'id', 'name', 'status', 'started_at', 'finished_at',
            'duration_ms', 'metrics', 'note',
        ))

        report = self.report(days=13)

        self.assertEqual(report['days'], 30)
        self.assertEqual(
            report['window_start'].time(), timezone.datetime.min.time(),
        )
        after = list(TaskRun.objects.order_by('id').values(
            'id', 'name', 'status', 'started_at', 'finished_at',
            'duration_ms', 'metrics', 'note',
        ))
        self.assertEqual(after, before)
        self.assertEqual(run.status, 'succeeded')
        self.assertEqual(report['summary']['issue_row_total'], 0)
