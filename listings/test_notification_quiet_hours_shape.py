# -*- coding: utf-8 -*-
"""免打扰时段形状的口径测试。

这里的每一条都在钉一个容易读错的边界：相等时间不是 0 小时窗口、总闸关着的
时间不叠加、跨午夜窗口不能被数组切片吃掉、没有偏好行不等于关掉免打扰。
"""

from datetime import time

from django.contrib.auth.models import User
from django.test import TestCase

from .models import NotificationPreference
from .notification_quiet_hours_shape import (
    MIN_SAMPLE, build_notification_quiet_hours_shape,
)


def make_user(name):
    return User.objects.create_user(username=name)


def make_preference(name, *, enabled=False, start=None, end=None):
    return NotificationPreference.objects.create(
        user=make_user(name),
        quiet_hours_enabled=enabled,
        quiet_hours_start=start or time(22, 0),
        quiet_hours_end=end or time(8, 0),
    )


class NotificationQuietHoursShapeTests(TestCase):
    def test_no_preference_rows_are_counted_without_reporting_zero_rates(self):
        for name in ('no-preference-a', 'no-preference-b'):
            make_user(name)

        result = build_notification_quiet_hours_shape(days=30)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['user_total'], 2)
        self.assertEqual(result['preference_count'], 0)
        self.assertEqual(result['no_preference_count'], 2)
        self.assertIsNone(result['enabled_share'])

    def test_default_window_is_reported_as_cross_midnight_ten_hours(self):
        make_preference('default', enabled=True)

        result = build_notification_quiet_hours_shape(days=30)

        self.assertEqual(result['enabled_count'], 1)
        self.assertEqual(result['enabled_share'], 100.0)
        self.assertEqual(result['default_window_count'], 1)
        self.assertEqual(result['custom_window_count'], 0)
        directions = {row['key']: row for row in result['direction_rows']}
        self.assertEqual(directions['cross_midnight']['count'], 1)
        self.assertEqual(directions['same_day']['count'], 0)
        durations = {row['label']: row for row in result['duration_rows']}
        self.assertEqual(durations['8-12 小时']['count'], 1)

    def test_same_day_window_keeps_its_direction_and_duration(self):
        make_preference('same-day', enabled=True, start=time(1, 30), end=time(5, 30))

        result = build_notification_quiet_hours_shape(days=30)

        directions = {row['key']: row for row in result['direction_rows']}
        self.assertEqual(directions['same_day']['count'], 1)
        self.assertEqual(directions['cross_midnight']['count'], 0)
        durations = {row['label']: row for row in result['duration_rows']}
        self.assertEqual(durations['4-8 小时']['count'], 1)

    def test_cross_midnight_window_covers_both_sides_of_midnight(self):
        make_preference('cross-midnight', enabled=True, start=time(22, 30), end=time(8, 30))

        result = build_notification_quiet_hours_shape(days=30)
        hours = {row['hour']: row for row in result['hour_rows']}

        self.assertEqual(hours[22]['peak_count'], 1)
        self.assertEqual(hours[22]['average_count'], 0.5)
        self.assertEqual(hours[23]['peak_count'], 1)
        self.assertEqual(hours[23]['average_count'], 1.0)
        self.assertEqual(hours[0]['peak_count'], 1)
        self.assertEqual(hours[0]['average_count'], 1.0)
        self.assertEqual(hours[7]['peak_count'], 1)
        self.assertEqual(hours[7]['average_count'], 1.0)
        self.assertEqual(hours[8]['peak_count'], 1)
        self.assertEqual(hours[8]['average_count'], 0.5)
        self.assertEqual(hours[9]['peak_count'], 0)

    def test_equal_start_and_end_is_quality_noise_not_a_normal_window(self):
        make_preference('equal', enabled=True, start=time(22, 0), end=time(22, 0))

        result = build_notification_quiet_hours_shape(days=30)

        self.assertEqual(result['invalid_equal_count'], 1)
        self.assertTrue(all(row['count'] == 0 for row in result['direction_rows']))
        self.assertTrue(all(row['count'] == 0 for row in result['duration_rows']))
        self.assertTrue(all(row['count'] == 0 for row in result['start_hour_rows']))
        self.assertEqual(result['peak']['count'], 0)
        self.assertIsNone(result['peak']['first_minute'])

    def test_disabled_custom_window_is_counted_but_never_overlaid(self):
        make_preference(
            'disabled-custom', enabled=False,
            start=time(13, 0), end=time(14, 0),
        )

        result = build_notification_quiet_hours_shape(days=30)

        self.assertEqual(result['disabled_custom_count'], 1)
        self.assertEqual(result['enabled_count'], 0)
        self.assertTrue(all(row['count'] == 0 for row in result['duration_rows']))
        self.assertEqual(result['peak']['count'], 0)

    def test_a_user_without_a_preference_row_is_not_read_as_enabled(self):
        make_preference('disabled-default', enabled=False)
        make_user('never-configured')

        result = build_notification_quiet_hours_shape(days=30)

        self.assertEqual(result['preference_count'], 1)
        self.assertEqual(result['no_preference_count'], 1)
        self.assertEqual(result['enabled_count'], 0)

    def test_short_cross_midnight_window_is_overlay_minute_by_minute(self):
        make_preference('half-hour', enabled=True, start=time(23, 30), end=time(0, 30))

        result = build_notification_quiet_hours_shape(days=30)
        hours = {row['hour']: row for row in result['hour_rows']}

        self.assertEqual(hours[23]['average_count'], 0.5)
        self.assertEqual(hours[23]['peak_count'], 1)
        self.assertEqual(hours[0]['average_count'], 0.5)
        self.assertEqual(hours[0]['peak_count'], 1)
        self.assertEqual(hours[1]['average_count'], 0.0)

    def test_start_and_end_hour_tables_keep_all_twenty_four_hours(self):
        make_preference('all-hours', enabled=True, start=time(22, 0), end=time(8, 0))

        result = build_notification_quiet_hours_shape(days=30)

        self.assertEqual([row['hour'] for row in result['start_hour_rows']], list(range(24)))
        self.assertEqual([row['hour'] for row in result['end_hour_rows']], list(range(24)))
        self.assertEqual(sum(row['count'] for row in result['start_hour_rows']), 1)
        self.assertEqual(sum(row['count'] for row in result['end_hour_rows']), 1)

    def test_zero_numerator_with_a_denominator_is_zero_not_unknown(self):
        make_preference('enabled', enabled=True)

        result = build_notification_quiet_hours_shape(days=30)
        hour = next(row for row in result['start_hour_rows'] if row['hour'] == 12)

        self.assertEqual(hour['count'], 0)
        self.assertEqual(hour['share'], 0.0)

    def test_days_is_carried_but_never_filters_the_snapshot(self):
        make_preference('snapshot', enabled=True)

        one_day = build_notification_quiet_hours_shape(days=1)
        ninety_days = build_notification_quiet_hours_shape(days=90)
        one_day.pop('days')
        ninety_days.pop('days')

        self.assertEqual(one_day, ninety_days)

    def test_report_does_not_expose_usernames_or_personal_windows(self):
        make_preference(
            'private-schedule-user', enabled=True,
            start=time(1, 17), end=time(2, 23),
        )

        result = build_notification_quiet_hours_shape(days=30)

        self.assertNotIn('private-schedule-user', repr(result))
        self.assertNotIn('01:17-02:23', repr(result))

    def test_peak_reports_count_and_first_minute(self):
        make_preference('peak-one', enabled=True, start=time(9, 0), end=time(10, 0))
        make_preference('peak-two', enabled=True, start=time(9, 30), end=time(10, 30))

        result = build_notification_quiet_hours_shape(days=30)

        self.assertEqual(result['peak']['count'], 2)
        self.assertEqual(result['peak']['first_minute'], 9 * 60 + 30)
        self.assertEqual(result['peak']['first_minute_label'], '09:30')

    def test_small_sample_threshold_comes_from_the_shared_constant(self):
        result = build_notification_quiet_hours_shape(days=30)

        self.assertEqual(result['min_sample'], MIN_SAMPLE)
        self.assertTrue(all(
            row['is_small_sample'] for row in result['direction_rows']
        ))
