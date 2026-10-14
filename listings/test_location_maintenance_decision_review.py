# -*- coding: utf-8 -*-
"""校园地点维护决策回看的口径测试。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .location_maintenance_decision_review import (
    DEFERRED_ROW_LIMIT,
    build_location_maintenance_decision_review,
)
from .models import CampusLocation, CampusLocationMaintenanceDecision


def local_noon():
    return timezone.localtime().replace(
        hour=12, minute=0, second=0, microsecond=0,
    )


class LocationMaintenanceDecisionReviewTests(TestCase):
    def setUp(self):
        self.now = local_noon()
        self.staff = User.objects.create_user(
            username='decision-review-staff', password='safe-password-123',
        )

    def make_signal_location(self, name):
        return CampusLocation.objects.create(
            name=name, building='东区', address='东门内侧',
            description='白天有人经过', safety_note='',
        )

    def make_decision(
        self, location, *, outcome='accepted', created_days_ago,
        updated_days_ago=None, fingerprint='public_safety_gap',
    ):
        decision = CampusLocationMaintenanceDecision.objects.create(
            location=location,
            signal_fingerprint=fingerprint,
            outcome=outcome,
            note='用于回看测试。' if outcome in {'accepted', 'deferred'} else '',
            reviewed_by=self.staff,
        )
        created = self.now - timedelta(days=created_days_ago)
        updated = self.now - timedelta(
            days=created_days_ago if updated_days_ago is None else updated_days_ago,
        )
        CampusLocationMaintenanceDecision.objects.filter(pk=decision.pk).update(
            created_at=created, updated_at=updated,
        )
        decision.refresh_from_db()
        return decision

    def report(self, days=30):
        return build_location_maintenance_decision_review(
            days=days, now=self.now,
        )

    def test_empty_platform_keeps_the_fixed_shape(self):
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['summary']['queued_total'], 0)
        self.assertEqual(report['summary']['reviewed_total'], 0)
        self.assertIsNone(report['summary']['reviewed_share'])
        self.assertEqual(
            [row['key'] for row in report['first_decision_rows']],
            ['current', 'previous', 'earlier'],
        )
        self.assertEqual(len(report['outcome_age_rows']), 9)
        self.assertEqual(report['deferred_rows'], [])
        self.assertEqual(report['deferred_row_hidden_total'], 0)

    def test_clean_location_is_not_a_review_backlog(self):
        CampusLocation.objects.create(
            name='回看干净地点', building='东区', address='东门内侧',
            description='白天有人经过', safety_note='夜间有照明',
        )

        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['summary']['queued_total'], 0)

    def test_only_the_decision_matching_current_signals_is_counted(self):
        location = self.make_signal_location('指纹不匹配地点')
        self.make_decision(
            location, created_days_ago=1, fingerprint='profile_gap',
        )

        report = self.report()

        self.assertTrue(report['has_data'])
        self.assertEqual(report['summary']['queued_total'], 1)
        self.assertEqual(report['summary']['unreviewed_total'], 1)
        self.assertEqual(report['summary']['reviewed_total'], 0)
        self.assertEqual(
            report['summary']['first_decision_current_total'], 0,
        )

    def test_first_decisions_are_split_by_local_period_bounds(self):
        current = self.make_signal_location('本周期决策地点')
        previous = self.make_signal_location('上一周期决策地点')
        earlier = self.make_signal_location('更早决策地点')
        self.make_signal_location('仍待核对地点')
        self.make_decision(current, created_days_ago=1)
        self.make_decision(previous, created_days_ago=35)
        self.make_decision(earlier, created_days_ago=100)

        report = self.report(days=30)

        self.assertEqual(report['summary']['queued_total'], 4)
        self.assertEqual(report['summary']['reviewed_total'], 3)
        self.assertEqual(report['summary']['reviewed_share'], 75.0)
        self.assertEqual(
            report['summary']['first_decision_current_total'], 1,
        )
        self.assertEqual(
            report['summary']['first_decision_previous_total'], 1,
        )
        self.assertEqual(
            report['summary']['first_decision_earlier_total'], 1,
        )
        shares = {
            row['key']: row['queue_share']
            for row in report['first_decision_rows']
        }
        self.assertEqual(shares, {
            'current': 25.0, 'previous': 25.0, 'earlier': 25.0,
        })

    def test_outcome_age_rows_are_complete_and_mutually_exclusive(self):
        handled = self.make_signal_location('本周期已处理地点')
        accepted = self.make_signal_location('上一周期维持地点')
        deferred = self.make_signal_location('更早暂缓地点')
        self.make_decision(
            handled, outcome='handled', created_days_ago=1,
        )
        self.make_decision(
            accepted, outcome='accepted', created_days_ago=40,
        )
        self.make_decision(
            deferred, outcome='deferred', created_days_ago=100,
            updated_days_ago=20,
        )

        report = self.report(days=30)
        totals = {
            (row['outcome'], row['cohort']): row['decision_total']
            for row in report['outcome_age_rows']
        }

        self.assertEqual(totals[('handled', 'current')], 1)
        self.assertEqual(totals[('accepted', 'previous')], 1)
        self.assertEqual(totals[('deferred', 'earlier')], 1)
        self.assertEqual(sum(totals.values()), 3)
        self.assertEqual(report['summary']['deferred_total'], 1)

    def test_deferred_rows_sort_by_oldest_last_update_and_truncate(self):
        names = []
        for index in range(DEFERRED_ROW_LIMIT + 1):
            location = self.make_signal_location(f'暂缓地点{index:02d}')
            self.make_decision(
                location, outcome='deferred', created_days_ago=100 - index,
                updated_days_ago=80 - index,
            )
            names.append(location.name)

        report = self.report()

        self.assertEqual(report['summary']['deferred_total'], 11)
        self.assertEqual(len(report['deferred_rows']), DEFERRED_ROW_LIMIT)
        self.assertEqual(report['deferred_row_hidden_total'], 1)
        self.assertEqual(
            [row['name'] for row in report['deferred_rows']],
            names[:DEFERRED_ROW_LIMIT],
        )
        self.assertEqual(report['deferred_rows'][0]['last_update_age_days'], 80)
        self.assertEqual(report['deferred_rows'][0]['first_decision_age_days'], 100)

    def test_future_timestamps_do_not_create_negative_ages(self):
        location = self.make_signal_location('未来决策地点')
        self.make_decision(
            location, outcome='deferred', created_days_ago=-2,
            updated_days_ago=-1,
        )

        report = self.report()
        row = report['deferred_rows'][0]

        self.assertEqual(row['first_decision_age_days'], 0)
        self.assertEqual(row['last_update_age_days'], 0)

    def test_invalid_period_falls_back_and_the_module_is_read_only(self):
        location = self.make_signal_location('只读决策地点')
        decision = self.make_decision(
            location, outcome='accepted', created_days_ago=1,
        )
        before = (
            list(CampusLocation.objects.values('id', 'name', 'building', 'address', 'description', 'is_active', 'is_public', 'safety_note', 'sort_order')),
            list(
                CampusLocationMaintenanceDecision.objects.values(
                    'id', 'outcome', 'note', 'signal_fingerprint',
                    'created_at', 'updated_at',
                )
            ),
        )

        report = self.report(days=13)

        self.assertEqual(report['days'], 30)
        self.assertEqual(
            report['current_period_start'].time(),
            timezone.datetime.min.time(),
        )
        after = (
            list(CampusLocation.objects.values('id', 'name', 'building', 'address', 'description', 'is_active', 'is_public', 'safety_note', 'sort_order')),
            list(
                CampusLocationMaintenanceDecision.objects.values(
                    'id', 'outcome', 'note', 'signal_fingerprint',
                    'created_at', 'updated_at',
                )
            ),
        )
        self.assertEqual(after, before)
        self.assertEqual(report['summary']['reviewed_total'], 1)
        self.assertEqual(decision.outcome, 'accepted')


class LocationMaintenanceDecisionReviewDashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='review-dashboard-staff', password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_operations_dashboard_renders_the_review_panel(self):
        location = CampusLocation.objects.create(
            name='回看面板地点', building='东区', address='东门内侧',
            description='白天有人经过', safety_note='',
        )
        decision = CampusLocationMaintenanceDecision.objects.create(
            location=location, signal_fingerprint='public_safety_gap',
            outcome='deferred', note='等待开学后再补提示。',
            reviewed_by=self.staff,
        )
        stamp = timezone.now() - timedelta(days=40)
        CampusLocationMaintenanceDecision.objects.filter(pk=decision.pk).update(
            created_at=stamp, updated_at=stamp,
        )

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园地点维护决策回看')
        self.assertContains(response, '回看面板地点')
        self.assertContains(response, '更早首次决策')
        self.assertContains(response, '不计算跨周期处理率')
        self.assertContains(response, '只观察停留，不自动催办')
        self.assertNotContains(response, '等待开学后再补提示')

    def test_operations_dashboard_export_contains_the_review_sections(self):
        location = CampusLocation.objects.create(
            name='回看导出地点', building='东区', address='东门内侧',
            description='白天有人经过', safety_note='',
        )
        decision = CampusLocationMaintenanceDecision.objects.create(
            location=location, signal_fingerprint='public_safety_gap',
            outcome='accepted', note='该入口无需额外提示。',
            reviewed_by=self.staff,
        )
        stamp = timezone.now() - timedelta(days=1)
        CampusLocationMaintenanceDecision.objects.filter(pk=decision.pk).update(
            created_at=stamp, updated_at=stamp,
        )

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('校园地点维护决策回看', text)
        self.assertIn('首次决策时间,决策数,当前队列占比（%）,已处理,维持现状,暂缓处理,读法', text)
        self.assertIn('处理结果,本周期首次决策,上一周期首次决策,更早首次决策,合计', text)
        self.assertIn('暂缓地点,主优先级,全部信号,首次决策,最后更新,最后更新间隔,读法', text)
        self.assertIn('不计算处理率、不给升降方向', text)
        self.assertNotIn('该入口无需额外提示', text)
        self.assertNotIn('review-dashboard-staff', text)
