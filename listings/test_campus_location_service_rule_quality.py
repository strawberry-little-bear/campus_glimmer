# -*- coding: utf-8 -*-
"""校园开放规则配置审计的口径、页面与导出测试。

这些信号都不是数据库错误：未登记可以代表全天协商开放，重叠会被评估器合并，
跨午夜被次日例外截断也是统一解释器刻意保留的边界。测试要守住的是“配置会
被怎么解释”不能靠猜，页面与导出也不能各说一套。
"""

from datetime import date, datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campus_location_service_rule_quality import (
    RULE_ROW_LIMIT,
    build_campus_location_service_rule_quality,
)
from .models import (
    CampusLocation,
    CampusLocationServiceException,
    CampusLocationServiceExceptionWindow,
    CampusLocationServiceWindow,
)


class CampusLocationServiceRuleQualityTestBase(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 10, 18, 12, 0))
        self.today = date(2026, 10, 18)

    def make_location(self, name, **kwargs):
        return CampusLocation.objects.create(name=name, **kwargs)

    def make_window(self, location, weekday=1, start=time(9), end=time(18), **kwargs):
        return CampusLocationServiceWindow.objects.create(
            location=location,
            weekday=weekday,
            start_time=start,
            end_time=end,
            **kwargs,
        )

    def make_exception(
        self, location, effective_date, exception_type=CampusLocationServiceException.CLOSED,
        reason='规则审计例外', is_active=True,
    ):
        return CampusLocationServiceException.objects.create(
            location=location,
            effective_date=effective_date,
            exception_type=exception_type,
            reason=reason,
            is_active=is_active,
        )

    def make_replacement_window(self, exception, start, end):
        return CampusLocationServiceExceptionWindow.objects.create(
            service_exception=exception,
            start_time=start,
            end_time=end,
        )


class CampusLocationServiceRuleQualityTests(CampusLocationServiceRuleQualityTestBase):
    def test_empty_state_does_not_invent_signals(self):
        panel = build_campus_location_service_rule_quality(now=self.now)

        self.assertFalse(panel['has_data'])
        self.assertEqual(panel['summary'], '当前还没有校园地点或开放规则可审计。')
        self.assertEqual(panel['rule_rows'], [])
        self.assertEqual(panel['location_rows'], [])
        self.assertEqual(panel['metrics']['signal_total'], 0)

    def test_registry_shape_and_exception_lifecycle_are_separate(self):
        registered = self.make_location('已登记地点')
        unregistered = self.make_location('未登记地点')
        inactive = self.make_location('停用地点', is_active=False)
        self.make_window(registered)
        self.make_window(
            registered, weekday=2, start=time(10), end=time(12), is_active=False,
        )
        self.make_exception(registered, self.today - timedelta(days=1))
        self.make_exception(
            unregistered, self.today,
            exception_type=CampusLocationServiceException.REPLACEMENT,
        )
        self.make_exception(inactive, self.today + timedelta(days=1))
        self.make_exception(
            registered, self.today + timedelta(days=2), is_active=False,
        )

        panel = build_campus_location_service_rule_quality(now=self.now)
        metrics = panel['metrics']

        self.assertEqual(metrics['location_total'], 3)
        self.assertEqual(metrics['active_location_count'], 2)
        self.assertEqual(metrics['inactive_location_count'], 1)
        self.assertEqual(metrics['registered_location_count'], 1)
        self.assertEqual(metrics['unregistered_location_count'], 1)
        self.assertEqual(metrics['active_window_count'], 1)
        self.assertEqual(metrics['inactive_window_count'], 1)
        self.assertEqual(metrics['active_exception_count'], 3)
        self.assertEqual(metrics['inactive_exception_count'], 1)
        self.assertEqual(metrics['past_exception_count'], 1)
        self.assertEqual(metrics['today_exception_count'], 1)
        self.assertEqual(metrics['future_exception_count'], 1)
        self.assertEqual(metrics['closed_exception_count'], 2)
        self.assertEqual(metrics['replacement_exception_count'], 1)

    def test_five_configuration_signals_are_explainable(self):
        inactive = self.make_location('A停用但例外仍在', is_active=False)
        self.make_exception(
            inactive, self.today + timedelta(days=1),
            exception_type=CampusLocationServiceException.REPLACEMENT,
        )

        without_interval = self.make_location('B替换没有时段')
        self.make_window(without_interval, weekday=2, start=time(9), end=time(12))
        self.make_exception(
            without_interval, self.today + timedelta(days=2),
            exception_type=CampusLocationServiceException.REPLACEMENT,
        )

        overlap = self.make_location('C替换时段重叠')
        self.make_window(overlap, weekday=2, start=time(9), end=time(12))
        overlap_exception = self.make_exception(
            overlap, self.today + timedelta(days=3),
            exception_type=CampusLocationServiceException.REPLACEMENT,
        )
        self.make_replacement_window(overlap_exception, time(9), time(10))
        self.make_replacement_window(overlap_exception, time(9, 30), time(11))

        midnight = self.make_location('D跨午夜截断')
        self.make_window(
            midnight, weekday=1, start=time(22), end=time(2),
        )
        self.make_exception(
            midnight, date(2026, 10, 20),
            exception_type=CampusLocationServiceException.CLOSED,
        )

        self.make_location('E启用未登记')

        panel = build_campus_location_service_rule_quality(now=self.now)

        self.assertEqual(
            [row['signal'] for row in panel['rule_rows']],
            [
                'inactive_location_exception',
                'replacement_without_window',
                'replacement_overlap',
                'midnight_window_truncated',
                'unregistered_location',
            ],
        )
        self.assertIn('会被解释为全天关闭', panel['rule_rows'][1]['reason'])
        self.assertIn('合并', panel['rule_rows'][2]['reason'])
        self.assertIn('截断到午夜', panel['rule_rows'][3]['reason'])
        self.assertEqual(panel['metrics']['signal_total'], 5)

    def test_adjacent_replacement_windows_are_not_reported_as_overlap(self):
        location = self.make_location('相邻替换时段')
        exception = self.make_exception(
            location, self.today + timedelta(days=1),
            exception_type=CampusLocationServiceException.REPLACEMENT,
        )
        self.make_replacement_window(exception, time(9), time(10))
        self.make_replacement_window(exception, time(10), time(11))
        self.make_window(location, weekday=1, start=time(9), end=time(10))
        self.make_window(location, weekday=1, start=time(10), end=time(11))

        panel = build_campus_location_service_rule_quality(now=self.now)

        self.assertEqual(panel['metrics']['replacement_overlap'], 0)
        self.assertEqual(panel['metrics']['weekly_overlap_or_adjacent_group_count'], 1)
        self.assertNotIn('replacement_overlap', {
            row['signal'] for row in panel['rule_rows']
        })

    def test_cross_midnight_weekly_window_overlap_uses_materialised_time(self):
        location = self.make_location('跨午夜每周重叠')
        self.make_window(location, weekday=1, start=time(9), end=time(10))
        self.make_window(location, weekday=1, start=time(8), end=time(2))

        panel = build_campus_location_service_rule_quality(now=self.now)

        self.assertEqual(panel['metrics']['weekly_overlap_or_adjacent_group_count'], 1)

    def test_page_limit_keeps_the_full_list_for_export(self):
        for index in range(RULE_ROW_LIMIT + 1):
            self.make_location(f'未登记地点 {index:02d}')

        panel = build_campus_location_service_rule_quality(now=self.now)

        self.assertEqual(len(panel['rule_rows']), RULE_ROW_LIMIT + 1)
        self.assertEqual(len(panel['rule_page_rows']), RULE_ROW_LIMIT)
        self.assertEqual(panel['rule_total_count'], RULE_ROW_LIMIT + 1)
        self.assertEqual(panel['rule_page_rows'][0]['location_name'], '未登记地点 00')

    def test_builder_is_read_only_and_stable(self):
        location = self.make_location('只读规则地点')
        window = self.make_window(location, weekday=1, start=time(22), end=time(2))
        exception = self.make_exception(location, date(2026, 10, 20))
        snapshots = {
            model: list(model.objects.values())
            for model in (
                CampusLocation,
                CampusLocationServiceWindow,
                CampusLocationServiceException,
                CampusLocationServiceExceptionWindow,
            )
        }

        panel = build_campus_location_service_rule_quality(now=self.now)
        panel_again = build_campus_location_service_rule_quality(days=90, now=self.now)

        self.assertEqual(panel['metrics'], panel_again['metrics'])
        self.assertEqual(panel['rule_rows'], panel_again['rule_rows'])
        self.assertEqual(snapshots[CampusLocationServiceWindow][0]['id'], window.id)
        self.assertEqual(snapshots[CampusLocationServiceException][0]['id'], exception.id)
        for model, before in snapshots.items():
            self.assertEqual(list(model.objects.values()), before)


class CampusLocationServiceRuleQualityDashboardTests(CampusLocationServiceRuleQualityTestBase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            username='service-rule-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_page_and_csv_share_the_same_configuration_language(self):
        location = self.make_location('导出规则地点')
        exception = self.make_exception(
            location, self.today + timedelta(days=1),
            exception_type=CampusLocationServiceException.REPLACEMENT,
        )

        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        page_text = page.content.decode('utf-8')
        export_text = export.content.decode('utf-8-sig')

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '校园开放规则配置审计')
        self.assertContains(page, '导出规则地点')
        self.assertIn('校园开放规则配置审计', export_text)
        self.assertIn('导出规则地点', export_text)
        self.assertIn(f'{self.today + timedelta(days=1):%Y-%m-%d}', export_text)
        self.assertIn(exception.reason, export_text)
        self.assertIn('会被解释为全天关闭', page_text)
        self.assertIn('会被解释为全天关闭', export_text)
        self.assertIn('不合并规则', page_text)
        self.assertIn('不合并规则', export_text)

    def test_clean_registered_rules_have_an_explicit_zero_signal_state(self):
        location = self.make_location('干净规则地点')
        self.make_window(location)

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '当前开放规则没有需要确认含义的配置信号')
        self.assertContains(response, '配置信号')
        self.assertContains(response, '0')
