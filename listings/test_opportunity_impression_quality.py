# -*- coding: utf-8 -*-
"""钉住机会流曝光日志自己的质量口径。

这些测试关心的不是曝光量，而是忽略率分母这张表的形状：周期边界、类型、目标一致性、
合法重复、同刻重复、未来时间、自然日覆盖以及页面与 CSV 是否说同一套话。
"""

import csv
import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    CampusLocation, Category, DemandPost, LostFoundPost, OpportunityImpression,
)
from .opportunity_impression_quality import (
    FUTURE_TOLERANCE, _target_consistency, build_opportunity_impression_quality,
)


class OpportunityImpressionQualityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='impression-quality-owner', password='safe-password-123',
        )
        self.viewer = User.objects.create_user(
            username='impression-quality-viewer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='机会曝光质量测试分类')
        self.location = CampusLocation.objects.create(name='机会曝光质量测试地点')

    def make_demand(self, title='求购机会曝光质量测试物品'):
        return DemandPost.objects.create(
            requester=self.owner, title=title,
            description='用于机会流曝光日志质量统计', category=self.category,
            location=self.location, max_price='30.00', status='active',
        )

    def make_lost_found_pair(self):
        LostFoundPost.objects.create(
            reporter=self.owner, post_type='lost', title='遗失机会曝光质量测试物品',
            description='用于机会流曝光日志质量统计', category=self.category,
            location=self.location, occurred_at=timezone.now() - timedelta(hours=3),
            status='active',
        )
        return LostFoundPost.objects.create(
            reporter=self.viewer, post_type='found', title='捡到机会曝光质量测试物品',
            description='用于机会流曝光日志质量统计', category=self.category,
            location=self.location, occurred_at=timezone.now() - timedelta(hours=2),
            status='active',
        )

    def make_impression(self, *, target=None, kind='demand', user=None, shown_at=None):
        if target is None:
            target = self.make_demand()
        filters = (
            {'demand': target, 'lost_found_post': None}
            if kind == 'demand'
            else {'demand': None, 'lost_found_post': target}
        )
        return OpportunityImpression.objects.create(
            user=user or self.viewer, kind=kind,
            shown_at=shown_at or timezone.now(), **filters,
        )

    def test_period_window_and_basic_counts_use_landed_rows(self):
        now = timezone.now()
        demand = self.make_demand()
        lost = self.make_lost_found_pair()
        self.make_impression(target=demand, user=self.owner, shown_at=now - timedelta(days=10))
        self.make_impression(target=demand, user=self.viewer, shown_at=now - timedelta(days=2))
        self.make_impression(
            target=lost, kind='lost_found', user=self.viewer,
            shown_at=now - timedelta(days=1),
        )
        self.make_impression(target=demand, user=self.viewer, shown_at=now - timedelta(days=40))

        report = build_opportunity_impression_quality(days=30, now=now)
        summary = report['summary']

        self.assertEqual(summary['total_impressions'], 3)
        self.assertEqual(summary['user_count'], 2)
        self.assertEqual(summary['target_count'], 2)
        self.assertEqual(summary['first_seen_at'], now - timedelta(days=10))
        self.assertEqual(summary['last_seen_at'], now - timedelta(days=1))
        self.assertEqual(summary['calendar_days'], 30)

    def test_kind_rows_keep_current_and_aggregate_bucket_columns(self):
        now = timezone.now()
        demand = self.make_demand()
        lost = self.make_lost_found_pair()
        self.make_impression(target=demand, shown_at=now - timedelta(minutes=20))
        self.make_impression(
            target=lost, kind='lost_found', shown_at=now - timedelta(minutes=10),
        )

        report = build_opportunity_impression_quality(days=30, now=now)
        rows = {row['key']: row for row in report['kind_rows']}

        self.assertEqual(rows['demand']['count'], 1)
        self.assertEqual(rows['lost_found']['count'], 1)
        self.assertEqual(rows['unknown']['count'], 0)
        self.assertEqual(rows['missing']['count'], 0)
        self.assertEqual(sum(row['count'] for row in rows.values()), 2)

    def test_target_consistency_covers_four_mutual_buckets(self):
        normal_demand = _target_consistency({
            'kind': 'demand', 'demand_id': 1, 'lost_found_post_id': None,
        })
        normal_lost = _target_consistency({
            'kind': 'lost_found', 'demand_id': None, 'lost_found_post_id': 1,
        })
        mismatch = _target_consistency({
            'kind': 'demand', 'demand_id': None, 'lost_found_post_id': 1,
        })
        missing = _target_consistency({
            'kind': 'demand', 'demand_id': None, 'lost_found_post_id': None,
        })

        self.assertEqual(normal_demand, 'demand_target')
        self.assertEqual(normal_lost, 'lost_found_target')
        self.assertEqual(mismatch, 'target_mismatch')
        self.assertEqual(missing, 'no_target')

        report = build_opportunity_impression_quality()
        counts = {row['key']: row['count'] for row in report['target_rows']}
        self.assertEqual(counts['target_mismatch'], 0)
        self.assertEqual(counts['no_target'], 0)

    def test_repeats_are_legal_and_show_the_first_seen_denominator(self):
        now = timezone.now()
        demand = self.make_demand()
        for minutes in (30, 20, 10):
            self.make_impression(target=demand, shown_at=now - timedelta(minutes=minutes))

        report = build_opportunity_impression_quality(days=30, now=now)
        summary = report['summary']

        self.assertEqual(summary['total_impressions'], 3)
        self.assertEqual(summary['first_seen_pairs'], 1)
        self.assertEqual(summary['repeat_pairs'], 1)
        self.assertEqual(summary['repeat_rows'], 2)
        self.assertEqual(summary['repeat_share'], 66.7)

    def test_same_timestamp_rows_are_flagged_without_being_deleted(self):
        now = timezone.now()
        demand = self.make_demand()
        same_time = now - timedelta(minutes=10)
        self.make_impression(target=demand, shown_at=same_time)
        self.make_impression(target=demand, shown_at=same_time)

        report = build_opportunity_impression_quality(days=30, now=now)
        summary = report['summary']

        self.assertEqual(summary['same_timestamp_pairs'], 1)
        self.assertEqual(summary['same_timestamp_rows'], 1)
        self.assertEqual(OpportunityImpression.objects.count(), 2)

    def test_future_tolerance_includes_small_skew_and_excludes_real_future(self):
        now = timezone.now()
        demand = self.make_demand()
        self.make_impression(target=demand, shown_at=now + FUTURE_TOLERANCE / 2)
        future = self.make_impression(target=demand, shown_at=now + timedelta(minutes=2))

        report = build_opportunity_impression_quality(days=30, now=now)
        summary = report['summary']

        self.assertEqual(summary['total_impressions'], 1)
        self.assertEqual(summary['future_count'], 1)
        self.assertLess(summary['last_seen_at'], future.shown_at)
        self.assertTrue(report['has_data'])

    def test_daily_rows_follow_tolerance_across_local_midnight(self):
        now = timezone.localtime().replace(hour=23, minute=59, second=50, microsecond=0)
        if timezone.is_naive(now):
            now = timezone.make_aware(now)
        demand = self.make_demand()
        self.make_impression(target=demand, shown_at=now + timedelta(seconds=30))

        report = build_opportunity_impression_quality(days=1, now=now)

        self.assertEqual(report['summary']['total_impressions'], 1)
        self.assertEqual(report['daily_rows'][-1]['date'], timezone.localdate(now + timedelta(seconds=30)))
        self.assertTrue(report['daily_rows'][-1]['has_data'])

    def test_daily_rows_keep_only_the_most_recent_thirty_days(self):
        now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        if timezone.is_naive(now):
            now = timezone.make_aware(now)
        demand = self.make_demand()
        self.make_impression(target=demand, shown_at=now - timedelta(days=89))
        self.make_impression(target=demand, shown_at=now)

        report = build_opportunity_impression_quality(days=90, now=now)

        self.assertEqual(report['daily_row_limit'], 30)
        self.assertEqual(report['daily_row_count'], 30)
        self.assertEqual(report['daily_rows_truncated'], 60)
        self.assertEqual(report['daily_rows'][0]['date'], timezone.localdate(now) - timedelta(days=29))
        self.assertEqual(report['daily_rows'][-1]['date'], timezone.localdate(now))
        self.assertEqual(report['summary']['total_impressions'], 2)


class OpportunityImpressionQualityDashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='impression-quality-staff', password='safe-password-123',
            is_staff=True,
        )
        self.owner = User.objects.create_user(
            username='impression-dashboard-owner', password='safe-password-123',
        )
        self.category = Category.objects.create(name='机会曝光看板测试分类')
        self.location = CampusLocation.objects.create(name='机会曝光看板测试地点')
        self.demand = DemandPost.objects.create(
            requester=self.owner, title='求购机会曝光看板测试物品',
            description='用于页面与导出同构测试', category=self.category,
            location=self.location, max_price='20.00', status='active',
        )
        OpportunityImpression.objects.create(
            user=self.owner, kind='demand', demand=self.demand,
            lost_found_post=None, shown_at=timezone.now() - timedelta(minutes=10),
        )
        self.client.force_login(self.staff)

    def test_page_and_export_use_the_same_quality_boundary(self):
        page = self.client.get(reverse('operations_dashboard'))
        export = self.client.get(reverse('operations_dashboard_export'))
        html = page.content.decode('utf-8')
        text = export.content.decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(text)))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertIn('机会流曝光日志质量', html)
        self.assertIn('写入失败只进日志、不进业务表', html)
        self.assertIn('不按一屏条数倒推缺失', html)
        self.assertTrue(any(row and row[0] == '机会流曝光日志质量' for row in rows))
        self.assertTrue(any(any('' in cell for cell in row) for row in rows))
        self.assertTrue(any(any('' in cell for cell in row) for row in rows))
        self.assertTrue(any(any('' in cell for cell in row) for row in rows))
        self.assertTrue(any(any('' in cell for cell in row) for row in rows))



