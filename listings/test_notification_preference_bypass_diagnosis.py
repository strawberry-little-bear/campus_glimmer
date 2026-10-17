# -*- coding: utf-8 -*-
"""钉住通知偏好关闭后的异常链路诊断口径。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Notification, NotificationPreference
from .notification_preference_bypass_diagnosis import (
    SUMMARY_WRITERS, build_notification_preference_bypass_diagnosis,
)
from .notification_preference_usage import _preference_fields


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def set_notification_time(notification, moment):
    Notification.objects.filter(pk=notification.pk).update(
        created_at=moment, last_occurred_at=moment,
    )


class NotificationPreferenceBypassDiagnosisTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.days = 30

    def make_preference(self, username, *, field='order_created', updated_at=None):
        user = make_user(username)
        preference = NotificationPreference.objects.create(user=user, **{field: False})
        if updated_at is not None:
            NotificationPreference.objects.filter(pk=preference.pk).update(
                updated_at=updated_at,
            )
        return user, preference

    def make_notification(self, recipient, *, kind='order_created', moment):
        notification = Notification.objects.create(
            recipient=recipient, kind=kind, title='标题', message='内容',
            last_occurred_at=moment,
        )
        set_notification_time(notification, moment)
        return notification

    def row(self, report, field='order_created'):
        return next(row for row in report['rows'] if row['field'] == field)

    def test_every_notification_switch_is_reported_with_a_writer(self):
        user, _ = self.make_preference('coverage-owner', updated_at=self.now)
        self.make_notification(user, moment=self.now - timedelta(minutes=1))

        report = build_notification_preference_bypass_diagnosis(
            days=self.days, now=self.now,
        )

        self.assertEqual(report['field_total'], 27)
        self.assertEqual(
            [row['field'] for row in report['rows']], list(_preference_fields()),
        )
        self.assertTrue(all(row['writer'] for row in report['rows']))
        self.assertEqual(
            {row['field'] for row in report['rows'] if row['path_label'] == '汇总写入'},
            set(SUMMARY_WRITERS),
        )

    def test_notification_after_last_preference_write_is_a_suspicious_lower_bound(self):
        updated_at = self.now - timedelta(days=2)
        user, _ = self.make_preference(
            'after-write-owner', updated_at=updated_at,
        )
        self.make_notification(user, moment=self.now - timedelta(days=1))

        report = build_notification_preference_bypass_diagnosis(
            days=self.days, now=self.now,
        )
        row = self.row(report)

        self.assertEqual(row['seen_users'], 1)
        self.assertEqual(row['consistent_users'], 0)
        self.assertEqual(row['suspicious_users'], 1)
        self.assertEqual(row['suspicious_rows'], 1)
        self.assertEqual(report['totals']['suspicious_users'], 1)
        self.assertIn('可疑下界', report['summary'])

    def test_notification_before_last_preference_write_stays_consistent(self):
        updated_at = self.now - timedelta(days=1)
        user, _ = self.make_preference(
            'consistent-owner', updated_at=updated_at,
        )
        self.make_notification(user, moment=self.now - timedelta(days=2))

        report = build_notification_preference_bypass_diagnosis(
            days=self.days, now=self.now,
        )
        row = self.row(report)

        self.assertEqual(row['seen_users'], 1)
        self.assertEqual(row['consistent_users'], 1)
        self.assertEqual(row['suspicious_users'], 0)
        self.assertEqual(row['suspicious_rows'], 0)

    def test_mixed_times_count_a_user_once_and_do_not_double_consistent(self):
        updated_at = self.now - timedelta(days=3)
        user, _ = self.make_preference('mixed-owner', updated_at=updated_at)
        self.make_notification(user, moment=self.now - timedelta(days=4))
        self.make_notification(user, moment=self.now - timedelta(days=2))

        report = build_notification_preference_bypass_diagnosis(
            days=self.days, now=self.now,
        )
        row = self.row(report)

        self.assertEqual(row['seen_rows'], 2)
        self.assertEqual(row['seen_users'], 1)
        self.assertEqual(row['consistent_users'], 0)
        self.assertEqual(row['suspicious_users'], 1)
        self.assertEqual(row['suspicious_rows'], 1)

    def test_window_and_future_notifications_are_excluded(self):
        updated_at = self.now - timedelta(days=40)
        user, _ = self.make_preference('window-owner', updated_at=updated_at)
        self.make_notification(user, moment=self.now - timedelta(days=40))
        self.make_notification(user, moment=self.now + timedelta(minutes=1))

        report = build_notification_preference_bypass_diagnosis(
            days=self.days, now=self.now,
        )
        row = self.row(report)

        self.assertEqual(row['seen_rows'], 0)
        self.assertEqual(row['seen_users'], 0)
        self.assertEqual(row['consistent_users'], 0)
        self.assertEqual(row['suspicious_users'], 0)
        self.assertFalse(report['has_candidates'])

    def test_missing_last_occurred_at_falls_back_to_created_at(self):
        updated_at = self.now - timedelta(days=2)
        user, _ = self.make_preference('fallback-owner', updated_at=updated_at)
        notification = Notification.objects.create(
            recipient=user, kind='order_created', title='标题', message='内容',
        )
        Notification.objects.filter(pk=notification.pk).update(
            created_at=self.now - timedelta(days=1), last_occurred_at=None,
        )

        report = build_notification_preference_bypass_diagnosis(
            days=self.days, now=self.now,
        )
        row = self.row(report)

        self.assertEqual(row['seen_users'], 1)
        self.assertEqual(row['suspicious_users'], 1)

    def test_user_without_preference_row_is_coverage_not_failure(self):
        user = make_user('default-switch-user')
        self.make_notification(user, moment=self.now - timedelta(days=1))

        report = build_notification_preference_bypass_diagnosis(
            days=self.days, now=self.now,
        )

        self.assertFalse(report['has_data'])
        self.assertEqual(report['coverage']['no_preference_users'], 1)
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['totals']['suspicious_users'], 0)

    def test_dashboard_and_export_are_staff_only_and_do_not_expose_usernames(self):
        updated_at = self.now - timedelta(days=2)
        user, _ = self.make_preference(
            'private-preference-owner', updated_at=updated_at,
        )
        self.make_notification(user, moment=self.now - timedelta(days=1))
        staff = User.objects.create_user(
            username='bypass-staff', password='safe-password-123', is_staff=True,
        )
        ordinary = make_user('bypass-ordinary')

        self.client.force_login(ordinary)
        self.assertEqual(self.client.get(reverse('operations_dashboard')).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('operations_dashboard_export')).status_code, 403,
        )

        self.client.force_login(staff)
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('通知偏好关了为什么还看得到', html)
        self.assertNotIn('private-preference-owner', html)

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(export.status_code, 200)
        text = export.content.decode('utf-8-sig')
        self.assertIn('通知偏好关了为什么还看得到', text)
        self.assertNotIn('private-preference-owner', text)
        self.assertIn('偏好写入后仍出现', text)
