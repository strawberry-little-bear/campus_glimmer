# -*- coding: utf-8 -*-
"""Keep search numerator/denominator pairing honest.

The position curve and click-through metrics deliberately drop rows that cannot
carry a search relation or a usable position. This module exists to show what
is dropped, so the tests pin down three boundaries: future rows never enter the
main period, clicks may pair with an impression older than the dashboard window,
and missing/orphan rows remain separate shapes rather than one scary "bad row"
number.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, SearchClick, SearchImpression, SearchQuery
from .operations_panel_sections import export_section, panel_html, panel_sections
from .search_impression_pairing_quality import (
    LATENCY_BUCKETS,
    build_search_impression_pairing_quality,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def move_created_at(row, when):
    row.__class__.objects.filter(pk=row.pk).update(created_at=when)
    return row.__class__.objects.get(pk=row.pk)


class SearchImpressionPairingQualityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='pairing-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='配对质量分类')
        self.item = Item.objects.create(
            title='不应导出的商品标题', description='用于配对质量测试。',
            price=Decimal('35.00'), category=self.category, condition='良好',
            seller=self.user, status='available',
        )
        self.now = local_noon()

    def make_search(self, term='配对搜索词'):
        return SearchQuery.objects.create(
            user=self.user, query=term, result_count=12,
        )

    def make_impression(self, search, when, position=1, user=None):
        row = SearchImpression.objects.create(
            search_query=search, item=self.item, position=position, user=user,
        )
        return move_created_at(row, when)

    def make_click(self, search, when, position=1, user=None):
        row = SearchClick.objects.create(
            search_query=search, item=self.item, position=position, user=user,
        )
        return move_created_at(row, when)

    def row_by_label(self, result, label):
        return next(row for row in result['field_rows'] if row['key'] == label)

    def test_empty_history_reports_shapes_without_inventing_zero_rates(self):
        result = build_search_impression_pairing_quality(days=7, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['summary']['total_impressions'], 0)
        self.assertEqual(result['summary']['total_clicks'], 0)
        self.assertIsNone(result['summary']['matched_click_share'])
        self.assertEqual(len(result['field_rows']), 8)
        self.assertEqual(len(result['latency_rows']), len(LATENCY_BUCKETS))

    def test_future_rows_are_counted_separately_from_the_main_period(self):
        search = self.make_search()
        self.make_impression(search, self.now + timedelta(days=1))
        self.make_click(search, self.now + timedelta(days=1))

        result = build_search_impression_pairing_quality(days=7, now=self.now)

        self.assertEqual(result['summary']['future_impression_count'], 1)
        self.assertEqual(result['summary']['future_click_count'], 1)
        self.assertEqual(result['summary']['total_impressions'], 0)
        self.assertEqual(result['summary']['total_clicks'], 0)
        self.assertTrue(result['has_data'])

    def test_a_click_can_pair_with_an_older_impression_outside_the_period(self):
        search = self.make_search()
        self.make_impression(search, self.now - timedelta(days=12), position=3)
        self.make_click(search, self.now - timedelta(days=1), position=3, user=None)

        result = build_search_impression_pairing_quality(days=7, now=self.now)

        self.assertEqual(result['summary']['total_clicks'], 1)
        self.assertEqual(result['summary']['total_impressions'], 0)
        self.assertEqual(result['summary']['matched_click_count'], 1)
        self.assertEqual(result['summary']['matched_click_share'], 100.0)
        self.assertEqual(result['summary']['paired_impression_count'], 1)
        self.assertIsNone(result['summary']['paired_impression_share'])
        over_week = next(
            row for row in result['latency_rows'] if row['label'] == '超过 7 天'
        )
        self.assertEqual(over_week['count'], 1)
        self.assertEqual(over_week['share'], 100.0)

    def test_position_consistency_accepts_any_earlier_position_for_the_pair(self):
        search = self.make_search()
        clicked = self.make_click(search, self.now - timedelta(hours=1), position=2)
        self.make_impression(
            search, clicked.created_at - timedelta(minutes=4), position=1,
        )
        self.make_impression(
            search, clicked.created_at - timedelta(minutes=2), position=2,
        )

        result = build_search_impression_pairing_quality(days=7, now=self.now)

        self.assertEqual(result['summary']['matched_click_count'], 1)
        self.assertEqual(result['summary']['position_match_count'], 1)
        self.assertEqual(result['summary']['position_mismatch_count'], 0)
        within_five = next(
            row for row in result['latency_rows'] if row['label'] == '5 分钟内'
        )
        self.assertEqual(within_five['count'], 1)

    def test_a_usable_click_position_outside_history_is_a_mismatch(self):
        search = self.make_search()
        clicked = self.make_click(search, self.now - timedelta(hours=2), position=9)
        self.make_impression(
            search, clicked.created_at - timedelta(minutes=30), position=1,
        )

        result = build_search_impression_pairing_quality(days=7, now=self.now)

        self.assertEqual(result['summary']['matched_click_count'], 1)
        self.assertEqual(result['summary']['position_match_count'], 0)
        self.assertEqual(result['summary']['position_mismatch_count'], 1)

    def test_missing_relations_zero_positions_and_unpaired_clicks_stay_separate(self):
        search = self.make_search()
        other = self.make_search('另一个不应出现的搜索词')
        start = self.now - timedelta(days=2)

        orphan_impression = SearchImpression.objects.create(
            search_query=None, item=self.item, position=1,
        )
        move_created_at(orphan_impression, start)

        orphan_click = SearchClick.objects.create(
            search_query=None, item=self.item, position=1,
        )
        move_created_at(orphan_click, start + timedelta(minutes=1))

        never_paired_search = self.make_search('没有任何曝光的搜索')
        never_paired = self.make_click(
            never_paired_search, start + timedelta(minutes=2), position=1,
        )

        early_click = self.make_click(
            other, start + timedelta(minutes=3), position=1,
        )
        later_impression = self.make_impression(
            other, start + timedelta(minutes=4), position=1,
        )
        self.assertGreater(later_impression.created_at, early_click.created_at)

        zero_position_click = self.make_click(
            search, start + timedelta(minutes=5), position=0,
        )
        self.make_impression(
            search, zero_position_click.created_at - timedelta(minutes=1), position=1,
        )

        result = build_search_impression_pairing_quality(days=7, now=self.now)
        summary = result['summary']

        self.assertEqual(summary['impression_missing_query_count'], 1)
        self.assertEqual(summary['click_missing_query_count'], 1)
        self.assertEqual(summary['never_paired_click_count'], 1)
        self.assertEqual(summary['click_before_pair_count'], 1)
        self.assertEqual(summary['zero_position_click_count'], 1)
        self.assertEqual(summary['matched_click_count'], 1)
        self.assertEqual(summary['unmatched_click_count'], 3)
        self.assertEqual(
            self.row_by_label(result, 'never_paired_click')['count'], 1,
        )
        self.assertEqual(
            self.row_by_label(result, 'click_before_pair')['count'], 1,
        )

    def test_the_report_does_not_expose_queries_people_or_item_titles(self):
        search = self.make_search('隐私搜索词')
        self.make_impression(
            search, self.now - timedelta(days=1), position=1, user=self.user,
        )
        self.make_click(search, self.now - timedelta(hours=1), position=1)

        rendered = repr(
            build_search_impression_pairing_quality(days=7, now=self.now)
        )

        self.assertNotIn('隐私搜索词', rendered)
        self.assertNotIn(self.user.username, rendered)
        self.assertNotIn(self.item.title, rendered)
        self.assertIn('配对', rendered)

    def test_daily_coverage_counts_either_stream_but_does_not_judge_gaps(self):
        search = self.make_search()
        self.make_impression(search, self.now - timedelta(days=2))
        self.make_click(search, self.now - timedelta(days=1))

        result = build_search_impression_pairing_quality(days=3, now=self.now)

        self.assertEqual(result['summary']['calendar_days'], 3)
        self.assertEqual(result['summary']['impression_active_days'], 1)
        self.assertEqual(result['summary']['click_active_days'], 1)
        self.assertEqual(result['summary']['either_active_days'], 2)
        self.assertEqual(result['summary']['zero_days'], 1)


class SearchImpressionPairingQualityDashboardTests(TestCase):
    """Keep the screen, CSV export, and privacy boundary in the same shape."""

    def test_dashboard_and_csv_have_the_same_shape_without_private_fields(self):
        staff = User.objects.create_user(
            username='pairing-quality-staff', password='safe-password-123',
            is_staff=True,
        )
        seller = User.objects.create_user(
            username='pairing-quality-export-seller', password='safe-password-123',
        )
        category = Category.objects.create(name='配对质量导出分类')
        item = Item.objects.create(
            title='配对导出不应出现的商品标题', description='用于导出测试。',
            price='43.00', category=category, condition='良好',
            seller=seller, status='available',
        )
        now = timezone.localtime().replace(
            hour=12, minute=0, second=0, microsecond=0,
        )
        search = SearchQuery.objects.create(
            user=seller, query='配对导出隐私搜索词', result_count=3,
        )
        impression = SearchImpression.objects.create(
            search_query=search, item=item, position=1, user=seller,
        )
        SearchImpression.objects.filter(pk=impression.pk).update(
            created_at=now - timedelta(days=1),
        )
        click = SearchClick.objects.create(
            search_query=search, item=item, position=1,
        )
        SearchClick.objects.filter(pk=click.pk).update(
            created_at=now - timedelta(hours=2),
        )
        self.client.force_login(staff)

        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        html = page.content.decode('utf-8')
        panel = panel_html(html, '搜索曝光与点击配对质量')
        self.assertIn('字段与结构', panel)
        self.assertIn('配对与延迟', panel)
        self.assertIn('覆盖', panel)

        section = '\n'.join(
            ','.join(row) for row in export_section(
                export.content.decode('utf-8-sig'), '搜索曝光与点击配对质量',
                panel_sections(html),
            )
        )
        self.assertIn('字段与结构', section)
        self.assertIn('配对与延迟', section)
        self.assertIn('覆盖', section)
        self.assertNotIn('配对导出隐私搜索词', section)
        self.assertNotIn(seller.username, section)
        self.assertNotIn(item.title, section)
