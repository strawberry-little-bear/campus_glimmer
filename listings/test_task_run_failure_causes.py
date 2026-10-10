# -*- coding: utf-8 -*-
"""Classify failed task runs by what kind of problem they were.

The ledger records the exception name in the note of every failed run,
which means the shape of a failure is available but has never been
aggregated. These tests pin down the two properties that make the
aggregation usable: the closed set of exception names maps onto causes
without guessing, and the result never decides severity on its own.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import TaskRun
from .task_run_failure_causes import (
    CAUSE_CONFIG,
    CAUSE_DATA,
    CAUSE_EXTERNAL,
    CAUSE_UNKNOWN,
    build_task_run_failure_causes,
    classify_failure_note,
)


class ClassifyFailureNoteTests(TestCase):
    """The note format is the only input, so parsing must be strict."""

    def test_exception_name_before_the_first_colon_decides(self):
        self.assertEqual(classify_failure_note('CommandError: 给定参数错误'), CAUSE_CONFIG)
        self.assertEqual(classify_failure_note('IntegrityError: NOT NULL'), CAUSE_DATA)
        self.assertEqual(classify_failure_note('OperationalError: server closed'), CAUSE_EXTERNAL)

    def test_a_note_without_an_exception_name_is_not_guessed(self):
        # A bare message says nothing about which kind of problem this was.
        self.assertEqual(classify_failure_note('今天没有到期订单'), CAUSE_UNKNOWN)
        self.assertEqual(classify_failure_note('连接被重置'), CAUSE_UNKNOWN)

    def test_an_empty_note_is_unknown_rather_than_absent(self):
        self.assertEqual(classify_failure_note(''), CAUSE_UNKNOWN)
        self.assertEqual(classify_failure_note(None), CAUSE_UNKNOWN)

    def test_a_first_token_that_is_not_a_name_is_unknown(self):
        # "warning: something" looks like a note but is not an exception name.
        self.assertEqual(classify_failure_note('warning: mail backlog grew'), CAUSE_UNKNOWN)

    def test_unregistered_exception_names_stay_unknown(self):
        # Project exceptions are not on the map, and inventing a category
        # for them would send the operator looking in the wrong place.
        self.assertEqual(classify_failure_note('OrderTransitionError: 状态不允许'), CAUSE_UNKNOWN)
        self.assertEqual(classify_failure_note('SomethingElse: x'), CAUSE_UNKNOWN)

    def test_a_colon_inside_the_message_does_not_confuse_the_split(self):
        self.assertEqual(
            classify_failure_note('OperationalError: could not connect: timeout'),
            CAUSE_EXTERNAL,
        )


class TaskRunFailureCausesTests(TestCase):
    """Aggregation answers "which kind of failure dominated this period"."""

    def make_run(self, name, status='succeeded', minutes_ago=0, note=''):
        started = timezone.now() - timedelta(minutes=minutes_ago)
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=started,
            finished_at=started,
            duration_ms=120,
            metrics={},
            note=note,
        )

    def cause_of(self, data, cause):
        return next((row for row in data['causes'] if row['cause'] == cause), None)

    def test_no_failures_means_no_causes(self):
        self.make_run('expire_items')

        data = build_task_run_failure_causes(days=7)
        self.assertEqual(data['total_failed'], 0)
        self.assertEqual(data['causes'], [])
        self.assertIsNone(data['top_cause'])
        self.assertFalse(data['has_data'])

    def test_failures_are_grouped_by_cause(self):
        self.make_run('expire_items', status='failed', note='OperationalError: db gone')
        self.make_run('expire_items', status='failed', note='OperationalError: db gone again')
        self.make_run('send_operations_digest', status='failed', note='CommandError: no settings')

        data = build_task_run_failure_causes(days=7)
        self.assertEqual(data['total_failed'], 3)
        self.assertEqual(data['classified_count'], 3)
        self.assertEqual(data['unclassified_count'], 0)

        external = self.cause_of(data, CAUSE_EXTERNAL)
        config = self.cause_of(data, CAUSE_CONFIG)
        self.assertEqual(external['count'], 2)
        self.assertEqual(config['count'], 1)
        # The command driving the category is visible without a second query.
        self.assertEqual(external['command_rows'], [{'name': 'expire_items', 'count': 2}])
        self.assertEqual(external['command_count'], 1)

    def test_causes_are_ordered_by_count_so_the_largest_leads(self):
        for index in range(3):
            self.make_run('expire_items', status='failed', note='IntegrityError: x')
        self.make_run('send_operations_digest', status='failed', note='CommandError: y')

        data = build_task_run_failure_causes(days=7)
        self.assertEqual(data['top_cause'], CAUSE_DATA)
        self.assertEqual(data['causes'][0]['cause'], CAUSE_DATA)
        self.assertIn('3', data['summary'])

    def test_unclassified_failures_are_counted_separately(self):
        self.make_run('expire_items', status='failed', note='IntegrityError: x')
        self.make_run('expire_items', status='failed', note='今天没有到期订单')
        self.make_run('send_operations_digest', status='failed', note='')

        data = build_task_run_failure_causes(days=7)
        self.assertEqual(data['total_failed'], 3)
        self.assertEqual(data['classified_count'], 1)
        self.assertEqual(data['unclassified_count'], 2)
        unknown = self.cause_of(data, CAUSE_UNKNOWN)
        self.assertEqual(unknown['count'], 2)
        self.assertEqual(unknown['command_count'], 2)

    def test_dry_runs_are_excluded(self):
        # A run that sends nothing cannot vouch for the schedule, and its
        # failures say nothing about it either.
        self.make_run('send_lifecycle_reminders', status='failed', note='OperationalError: x')
        self.make_run('send_lifecycle_reminders', status='failed', note='dry-run')

        data = build_task_run_failure_causes(days=7)
        self.assertEqual(data['total_failed'], 1)
        self.assertEqual(data['causes'][0]['count'], 1)

    def test_successful_runs_are_not_counted_as_failures(self):
        self.make_run('expire_items', note='CommandError: stale note')
        data = build_task_run_failure_causes(days=7)
        self.assertEqual(data['total_failed'], 0)

    def test_older_runs_fall_outside_the_period(self):
        self.make_run('expire_items', status='failed', minutes_ago=60 * 24 * 30, note='CommandError: x')
        self.assertEqual(build_task_run_failure_causes(days=7)['total_failed'], 0)
        self.assertEqual(build_task_run_failure_causes(days=365)['total_failed'], 1)

    def test_last_failure_at_and_note_come_from_the_newest_row(self):
        self.make_run('expire_items', status='failed', minutes_ago=120, note='OperationalError: old')
        self.make_run('expire_items', status='failed', minutes_ago=5, note='OperationalError: new')

        external = self.cause_of(build_task_run_failure_causes(days=7), CAUSE_EXTERNAL)
        self.assertEqual(external['last_note'], 'OperationalError: new')

    def test_command_rows_are_sorted_by_count(self):
        self.make_run('send_operations_digest', status='failed', note='CommandError: a')
        self.make_run('send_operations_digest', status='failed', note='CommandError: b')
        self.make_run('expire_items', status='failed', note='CommandError: c')

        config = self.cause_of(build_task_run_failure_causes(days=7), CAUSE_CONFIG)
        self.assertEqual(
            [row['name'] for row in config['command_rows']],
            ['send_operations_digest', 'expire_items'],
        )

    def test_every_cause_carries_a_label_and_a_hint(self):
        # The label and the hint are what the page shows; a category with
        # no explanation is just a rename of the exception name.
        notes = {
            CAUSE_CONFIG: 'CommandError: x',
            CAUSE_DATA: 'IntegrityError: x',
            CAUSE_EXTERNAL: 'OperationalError: x',
            CAUSE_UNKNOWN: '今天没有到期订单',
        }
        for note in notes.values():
            self.make_run('expire_items', status='failed', note=note)

        data = build_task_run_failure_causes(days=7)
        self.assertEqual(len(data['causes']), 4)
        for row in data['causes']:
            self.assertTrue(row['label'])
            self.assertTrue(row['hint'])

    def test_classification_never_decides_severity(self):
        # The same exception name can be a fatal misconfiguration or a
        # normal early return, so a category must not carry a verdict.
        self.make_run('expire_items', status='failed', note='CommandError: x')
        self.make_run('send_operations_digest', status='failed', note='OperationalError: x')

        data = build_task_run_failure_causes(days=7)
        for row in data['causes']:
            self.assertNotIn('severity', row)
            self.assertNotIn('severity_label', row)
            self.assertNotIn('should_retry', row)
            self.assertNotIn('is_critical', row)
        # The rows stay available so the operator can go read the notes.
        self.assertTrue(all(row['command_rows'] for row in data['causes']))

    def test_summary_reports_the_total_and_the_largest_cause(self):
        self.make_run('expire_items', status='failed', note='IntegrityError: x')
        self.make_run('expire_items', status='failed', note='IntegrityError: y')

        data = build_task_run_failure_causes(days=7)
        self.assertIn('2', data['summary'])
        self.assertIn(data['causes'][0]['label'], data['summary'])

    def test_summary_mentions_the_missing_exception_names(self):
        # When nothing can be classified the sentence must say so rather
        # than naming a cause that does not exist.
        self.make_run('expire_items', status='failed', note='今天没有到期订单')
        data = build_task_run_failure_causes(days=7)
        self.assertIn('1', data['summary'])
        self.assertIn('备注', data['summary'])



class DashboardIntegrationTests(TestCase):
    """The panel must actually render on the operations dashboard."""

    def make_run(self, name, status='succeeded', minutes_ago=0, note=''):
        started = timezone.now() - timedelta(minutes=minutes_ago)
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=started,
            finished_at=started,
            duration_ms=120,
            metrics={},
            note=note,
        )

    def test_dashboard_renders_the_cause_panel(self):
        staff = User.objects.create_user(
            username='cause-staff', password='safe-password-123', is_staff=True,
        )
        self.make_run('expire_items', status='failed', note='OperationalError: db gone')
        self.make_run('expire_items', status='failed', note='OperationalError: db gone again')
        self.make_run('send_operations_digest', status='failed', note='CommandError: no settings')
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '定时任务失败原因分类')
        self.assertContains(response, '各类原因的失败次数')
        # The note survives to the page so the operator can go read it.
        self.assertContains(response, 'OperationalError: db gone again')
        causes = response.context['task_run_failure_causes']
        self.assertEqual(causes['total_failed'], 3)
        self.assertEqual(causes['causes'][0]['cause'], CAUSE_EXTERNAL)

    def test_dashboard_renders_the_empty_panel(self):
        staff = User.objects.create_user(
            username='cause-staff-empty', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '周期内没有失败的运行记录')
        self.assertFalse(response.context['task_run_failure_causes']['has_data'])

    def test_dashboard_raises_no_alert_for_a_handful_of_failures(self):
        # A single failure is a normal part of running a schedule; the
        # alert exists for a repeated pattern, not for any failure at all.
        staff = User.objects.create_user(
            username='cause-staff-one', password='safe-password-123', is_staff=True,
        )
        self.make_run('expire_items', status='failed', note='OperationalError: db gone')
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        keys = [alert['key'] for alert in response.context['operational_alerts']]
        self.assertNotIn('task_run_external_failures', keys)
        self.assertNotIn('task_run_repeated_failures', keys)

    def test_dashboard_alert_for_repeated_external_failures(self):
        staff = User.objects.create_user(
            username='cause-staff-many', password='safe-password-123', is_staff=True,
        )
        for index in range(4):
            self.make_run('send_operations_digest', status='failed', note='OperationalError: db gone')
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        alert = next(
            (row for row in response.context['operational_alerts']
             if row['key'] == 'task_run_external_failures'),
            None,
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert['severity'], 'warning')
        self.assertIn('外部依赖', alert['title'])
        # The wording must suggest retrying and observing, not a verdict.
        self.assertIn('重试', alert['message'])
        self.assertNotIn('自动', alert['message'])

    def test_dashboard_alert_for_repeated_config_failures(self):
        staff = User.objects.create_user(
            username='cause-staff-cfg', password='safe-password-123', is_staff=True,
        )
        for index in range(3):
            self.make_run('send_operations_digest', status='failed', note='CommandError: bad settings')
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        alert = next(
            (row for row in response.context['operational_alerts']
             if row['key'] == 'task_run_repeated_failures'),
            None,
        )
        self.assertIsNotNone(alert)
        # The message must not claim the category settles the question.
        self.assertIn('不代表', alert['message'])
