# -*- coding: utf-8 -*-
"""推荐曝光日志先量自己的形状，不伪装成能数出写入失败。"""

from datetime import timedelta

import csv
import io

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, RecommendationImpression
from .recommendation_impression_quality import build_recommendation_impression_quality


class RecommendationImpressionQualityTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.buyer = User.objects.create_user(
            username='impression-quality-buyer', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='impression-quality-other', password='safe-password-123',
        )
        self.seller = User.objects.create_user(
            username='impression-quality-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='曝光质量测试分类')
        self.item = Item.objects.create(
            title='曝光质量测试商品', description='用于推荐曝光日志质量测试。',
            price='32.00', category=self.category, condition='良好',
            seller=self.seller, status='available',
        )

    def create_row(self, *, user=None, reason_key='fresh', score=1.5,
                   position=1, created_at=None, item=None):
        row = RecommendationImpression.objects.create(
            user=user, item=item or self.item, reason_key=reason_key,
            score=score, position=position,
        )
        if created_at is not None:
            RecommendationImpression.objects.filter(pk=row.pk).update(
                created_at=created_at,
            )
        return row

    def result(self, days=30, now=None):
        return build_recommendation_impression_quality(
            days=days, now=now or self.now,
        )

    def test_an_empty_period_keeps_a_fixed_shape(self):
        result = self.result()

        self.assertFalse(result['has_data'])
        self.assertEqual(result['summary']['total_impressions'], 0)
        self.assertEqual(result['summary']['calendar_days'], 30)
        self.assertEqual(result['summary']['active_days'], 0)
        self.assertEqual(result['summary']['zero_days'], 30)
        self.assertIsNone(result['summary']['longest_gap_hours'])
        self.assertEqual(len(result['reason_rows']), 8)
        self.assertEqual(len(result['field_rows']), 6)
        self.assertEqual(set(result), {
            'period_days', 'period_start', 'period_end', 'has_data', 'summary',
            'metrics', 'reason_rows', 'field_rows', 'note',
        })

    def test_period_boundary_and_future_rows_do_not_mix(self):
        result = self.result(now=self.now)
        start = result['period_start']
        self.create_row(created_at=start - timedelta(microseconds=1))
        self.create_row(created_at=start)
        self.create_row(created_at=self.now)
        self.create_row(created_at=self.now + timedelta(seconds=1))

        result = self.result(now=self.now)

        self.assertEqual(result['summary']['total_impressions'], 1)
        self.assertEqual(result['summary']['future_count'], 1)
        self.assertTrue(result['has_data'])

    def test_field_shape_counts_without_misreading_legal_values(self):
        start = self.result()['period_start']
        common = {'created_at': start + timedelta(minutes=5)}
        self.create_row(user=self.buyer, **common)
        self.create_row(reason_key='', **common)
        self.create_row(reason_key='legacy-reason', **common)
        self.create_row(score=0, **common)
        self.create_row(position=0, **common)
        self.create_row(position=9, **common)

        result = self.result()

        summary = result['summary']
        self.assertEqual(summary['anonymous_impressions'], 5)
        self.assertEqual(summary['missing_reason_count'], 1)
        self.assertEqual(summary['unknown_reason_count'], 1)
        self.assertEqual(summary['unknown_reason_key_count'], 1)
        self.assertEqual(summary['zero_score_count'], 1)
        self.assertEqual(summary['invalid_position_count'], 2)
        self.assertEqual(result['reason_rows'][-1]['count'], 1)
        self.assertEqual(result['reason_rows'][-2]['count'], 1)

    def test_repeats_are_extra_rows_for_logged_in_visitors_only(self):
        start = self.result()['period_start']
        common = {'created_at': start + timedelta(minutes=10)}
        self.create_row(user=self.buyer, **common)
        self.create_row(user=self.buyer, **common)
        self.create_row(user=self.other, **common)
        self.create_row(user=None, **common)
        self.create_row(user=None, **common)

        result = self.result()

        self.assertEqual(result['summary']['repeat_pairs'], 1)
        self.assertEqual(result['summary']['repeat_rows'], 1)
        self.assertEqual(result['summary']['logged_in_impressions'], 3)
        self.assertEqual(result['summary']['repeat_share'], 33.3)
        self.assertEqual(result['summary']['anonymous_impressions'], 2)

    def test_time_coverage_and_longest_gap_use_local_days(self):
        result = self.result(days=3)
        start = result['period_start']
        now = start + timedelta(days=2, hours=23)
        self.create_row(created_at=start + timedelta(hours=1))
        self.create_row(created_at=start + timedelta(days=1, hours=2))
        self.create_row(created_at=start + timedelta(days=2, hours=3))

        result = self.result(days=3, now=now)

        self.assertEqual(result['summary']['calendar_days'], 3)
        self.assertEqual(result['summary']['active_days'], 3)
        self.assertEqual(result['summary']['zero_days'], 0)
        self.assertEqual(result['summary']['longest_gap_hours'], 25.0)

    def test_the_report_does_not_expose_people_items_or_raw_unknown_keys(self):
        start = self.result()['period_start']
        self.create_row(
            user=self.buyer, reason_key='legacy-reason',
            created_at=start + timedelta(minutes=1),
        )

        rendered = repr(self.result())

        self.assertNotIn(self.buyer.username, rendered)
        self.assertNotIn(self.seller.username, rendered)
        self.assertNotIn(self.item.title, rendered)
        self.assertNotIn('legacy-reason', rendered)
        self.assertIn('未收录理由', rendered)


class RecommendationImpressionQualityDashboardTests(TestCase):
    def test_dashboard_and_csv_have_the_same_shape_without_private_fields(self):
        staff = User.objects.create_user(
            username='impression-quality-staff',
            password='safe-password-123', is_staff=True,
        )
        seller = User.objects.create_user(username='impression-quality-export-seller')
        buyer = User.objects.create_user(username='impression-quality-export-buyer')
        category = Category.objects.create(name='曝光质量导出分类')
        item = Item.objects.create(
            title='导出不应出现的商品标题', description='用于导出测试。',
            price='41.00', category=category, condition='良好',
            seller=seller, status='available',
        )
        now = timezone.now()
        row = RecommendationImpression.objects.create(
            user=buyer, item=item, reason_key='fresh', score=0, position=1,
        )
        RecommendationImpression.objects.filter(pk=row.pk).update(
            created_at=now - timedelta(days=1),
        )
        self.client.force_login(staff)

        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        html = page.content.decode('utf-8')
        self.assertIn('推荐曝光日志质量', html)
        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        titles = [row[0] if row else '' for row in rows]
        self.assertIn('推荐曝光日志质量', titles)
        section = rows[titles.index('推荐曝光日志质量'):]
        rendered = '\n'.join(','.join(row) for row in section)
        self.assertIn('字段与结构', rendered)
        self.assertIn('理由分布', rendered)
        self.assertIn('重复与覆盖', rendered)
        self.assertNotIn(buyer.username, rendered)
        self.assertNotIn(seller.username, rendered)
        self.assertNotIn(item.title, rendered)
