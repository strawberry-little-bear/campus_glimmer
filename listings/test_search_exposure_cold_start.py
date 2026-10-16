# -*- coding: utf-8 -*-
"""Keep the new-listing search cold-start cohort honest.

This report deliberately starts before click-through: a listing that is never
rendered in search cannot have a click rate. The tests therefore pin down the
observation buffer, latency boundaries, invalid-time shapes, category rollups,
privacy boundary, and the fact that the page and CSV consume the same payload.
"""

import csv
import io
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, SearchClick, SearchImpression, SearchQuery
from .search_exposure_cold_start import (
    ITEM_ROW_LIMIT, LATENCY_BUCKETS, build_search_exposure_cold_start,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def move_created_at(row, when):
    row.__class__.objects.filter(pk=row.pk).update(created_at=when)
    return row.__class__.objects.get(pk=row.pk)


class SearchExposureColdStartTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='cold-start-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='冷启动分类')
        self.now = local_noon()

    def make_item(self, created_at, *, title='冷启动商品', category=None, status='available', trade_mode='sale'):
        item = Item.objects.create(
            title=title,
            description='用于新商品搜索曝光冷启动测试。',
            price=Decimal('35.00'),
            category=category or self.category,
            condition='良好',
            seller=self.seller,
            status=status,
            trade_mode=trade_mode,
        )
        return move_created_at(item, created_at)

    def make_impression(self, item, when, *, search=None):
        row = SearchImpression.objects.create(
            search_query=search, item=item, position=1,
        )
        return move_created_at(row, when)

    def make_click(self, item, when, *, search=None):
        row = SearchClick.objects.create(
            search_query=search, item=item, position=1,
        )
        return move_created_at(row, when)

    def test_empty_history_does_not_invent_zero_rates(self):
        result = build_search_exposure_cold_start(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['metrics']['observed_item_count'], 0)
        self.assertEqual(result['metrics']['cohort_item_count'], 0)
        self.assertEqual(len(result['latency_rows']), len(LATENCY_BUCKETS) + 1)
        self.assertTrue(all(row['share'] is None for row in result['latency_rows']))
        self.assertEqual(result['category_rows'], [])
        self.assertIn('还没有新发布商品', result['summary'])

    def test_items_inside_the_observation_buffer_are_registered_but_not_judged(self):
        self.make_item(self.now - timedelta(hours=49))
        self.make_item(self.now - timedelta(hours=10), title='未成熟商品')

        result = build_search_exposure_cold_start(days=30, now=self.now)

        self.assertTrue(result['has_data'])
        self.assertEqual(result['metrics']['observed_item_count'], 2)
        self.assertEqual(result['metrics']['immature_item_count'], 1)
        self.assertEqual(result['metrics']['cohort_item_count'], 1)
        self.assertEqual(result['metrics']['unexposed_item_count'], 1)
        self.assertIn('最新发布只登记为未成熟', result['period_note'])

    def test_latency_bucket_order_and_boundaries_are_stable(self):
        boundaries = (1, 6, 24, 72, 73)
        for hours in boundaries:
            item = self.make_item(
                self.now - timedelta(hours=49 + hours),
                title=f'{hours} 小时商品',
            )
            self.make_impression(item, item.created_at + timedelta(hours=hours))
        self.make_item(self.now - timedelta(days=4), title='始终未曝光商品')

        result = build_search_exposure_cold_start(days=30, now=self.now)

        self.assertEqual(
            [row['label'] for row in result['latency_rows']],
            ['1 小时内', '1—6 小时', '6—24 小时', '1—3 天', '超过 3 天', '观察 48 小时后未曝光'],
        )
        self.assertEqual([row['count'] for row in result['latency_rows']], [1, 1, 1, 1, 1, 1])
        self.assertEqual([row['share'] for row in result['latency_rows']], [16.7] * 6)
        self.assertEqual(result['metrics']['exposed_item_count'], 5)
        self.assertEqual(result['metrics']['unexposed_item_count'], 1)

    def test_only_clicks_at_or_after_the_first_exposure_count_as_response(self):
        item = self.make_item(self.now - timedelta(days=3))
        first_exposure = self.make_impression(item, item.created_at + timedelta(hours=2))
        self.make_click(item, first_exposure.created_at - timedelta(hours=1))
        self.make_click(item, first_exposure.created_at + timedelta(hours=1))

        result = build_search_exposure_cold_start(days=30, now=self.now)
        row = result['item_rows'][0]

        self.assertTrue(row['has_click'])
        self.assertTrue(row['has_click_after_exposure'])
        self.assertEqual(row['early_click_count'], 1)
        self.assertEqual(result['metrics']['clicked_after_exposure_item_count'], 1)
        self.assertEqual(result['metrics']['early_click_count'], 1)
        self.assertIn('早于首曝的点击', row['shape'])

    def test_an_impression_before_publication_never_becomes_negative_latency(self):
        item = self.make_item(self.now - timedelta(days=3))
        self.make_impression(item, item.created_at - timedelta(hours=2))
        self.make_impression(item, item.created_at + timedelta(hours=3))

        result = build_search_exposure_cold_start(days=30, now=self.now)
        row = result['item_rows'][0]

        self.assertEqual(row['first_exposure_hours'], 3.0)
        self.assertEqual(row['early_impression_count'], 1)
        self.assertEqual(result['metrics']['early_impression_count'], 1)
        self.assertIn('早于发布的曝光', row['shape'])

    def test_a_click_without_exposure_is_a_shape_not_a_response(self):
        item = self.make_item(self.now - timedelta(days=3))
        self.make_click(item, item.created_at + timedelta(hours=2))

        result = build_search_exposure_cold_start(days=30, now=self.now)
        row = result['item_rows'][0]

        self.assertFalse(row['has_exposure'])
        self.assertTrue(row['has_click'])
        self.assertFalse(row['has_click_after_exposure'])
        self.assertEqual(result['metrics']['click_without_exposure_item_count'], 1)
        self.assertEqual(result['metrics']['clicked_after_exposure_item_count'], 0)
        self.assertEqual(row['shape'], '无曝光但有点击')

    def test_category_rows_keep_cohorts_exposure_and_clicks_separate(self):
        slow_category = Category.objects.create(name='慢启动分类')
        fast_category = Category.objects.create(name='快启动分类')
        slow_one = self.make_item(self.now - timedelta(days=4), category=slow_category)
        slow_two = self.make_item(self.now - timedelta(days=5), category=slow_category)
        self.make_item(self.now - timedelta(days=6), category=slow_category, title='未曝光商品')
        fast = self.make_item(self.now - timedelta(days=3), category=fast_category)
        for item, hours in ((slow_one, 2), (slow_two, 10), (fast, 1)):
            exposure = self.make_impression(item, item.created_at + timedelta(hours=hours))
            self.make_click(item, exposure.created_at + timedelta(minutes=5))

        result = build_search_exposure_cold_start(days=30, now=self.now)
        rows = {row['category_name']: row for row in result['category_rows']}

        self.assertEqual(rows['慢启动分类']['cohort_count'], 3)
        self.assertEqual(rows['慢启动分类']['exposed_count'], 2)
        self.assertEqual(rows['慢启动分类']['unexposed_count'], 1)
        self.assertEqual(rows['慢启动分类']['clicked_after_exposure_count'], 2)
        self.assertEqual(rows['慢启动分类']['median_first_exposure_hours'], 2.0)
        self.assertEqual(rows['慢启动分类']['p90_first_exposure_hours'], 10.0)
        self.assertEqual(rows['慢启动分类']['unexposed_share'], 33.3)
        self.assertEqual(rows['快启动分类']['unexposed_share'], 0.0)

    def test_page_rows_are_limited_but_the_full_list_stays_available(self):
        for index in range(ITEM_ROW_LIMIT + 2):
            self.make_item(self.now - timedelta(days=3, hours=index), title=f'清单商品 {index}')

        result = build_search_exposure_cold_start(days=30, now=self.now)

        self.assertEqual(result['item_total_count'], ITEM_ROW_LIMIT + 2)
        self.assertEqual(len(result['item_rows']), ITEM_ROW_LIMIT + 2)
        self.assertEqual(len(result['item_page_rows']), ITEM_ROW_LIMIT)

    def test_the_report_is_read_only_repeatable_and_privacy_conscious(self):
        search = SearchQuery.objects.create(
            user=self.seller, query='冷启动隐私搜索词', result_count=3,
        )
        item = self.make_item(self.now - timedelta(days=3), title='不应出现的商品标题')
        exposure = self.make_impression(item, item.created_at + timedelta(hours=2), search=search)
        self.make_click(item, exposure.created_at + timedelta(minutes=5), search=search)

        before = (Item.objects.count(), SearchImpression.objects.count(), SearchClick.objects.count())
        first = build_search_exposure_cold_start(days=30, now=self.now)
        second = build_search_exposure_cold_start(days=30, now=self.now)
        after = (Item.objects.count(), SearchImpression.objects.count(), SearchClick.objects.count())

        self.assertEqual(before, after)
        self.assertEqual(first, second)
        rendered = repr(first)
        self.assertNotIn('不应出现的商品标题', rendered)
        self.assertNotIn('冷启动隐私搜索词', rendered)
        self.assertNotIn(self.seller.username, rendered)
        self.assertIn('冷启动分类', rendered)


class SearchExposureColdStartDashboardTests(TestCase):
    """The page and CSV must stay on the same read-only cohort contract."""

    def setUp(self):
        self.now = local_noon()
        self.staff = User.objects.create_user(
            username='cold-start-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='cold-start-export-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='冷启动导出分类')
        self.client.force_login(self.staff)

    def test_dashboard_and_csv_use_the_same_language_and_complete_list(self):
        item = Item.objects.create(
            title='导出不应出现的商品标题',
            description='用于冷启动导出测试。',
            price='43.00',
            category=self.category,
            condition='良好',
            seller=self.seller,
            status='available',
        )
        Item.objects.filter(pk=item.pk).update(created_at=self.now - timedelta(days=3))
        item.refresh_from_db()
        search = SearchQuery.objects.create(
            user=self.seller, query='导出不应出现的搜索词', result_count=3,
        )
        impression = SearchImpression.objects.create(
            search_query=search, item=item, position=1,
        )
        SearchImpression.objects.filter(pk=impression.pk).update(
            created_at=item.created_at + timedelta(hours=2),
        )

        page = self.client.get(reverse('operations_dashboard'))
        export = self.client.get(reverse('operations_dashboard_export'))
        page_text = page.content.decode('utf-8')
        export_text = export.content.decode('utf-8-sig')

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        for text in (page_text, export_text):
            self.assertIn('新商品搜索曝光冷启动', text)
            self.assertIn('首曝延迟分桶', text)
            self.assertIn('分类冷启动', text)
            self.assertIn('新商品清单', text)
            self.assertIn('观察 48 小时后未曝光', text)
        cold_start_html = re.search(
            r'<section[^>]*search-exposure-cold-start-panel.*?<section[^>]*refresh-response-panel',
            page_text,
            re.S,
        ).group(0)
        cold_start_title = list(re.finditer(r'(?m)^新商品搜索曝光冷启动\r?$', export_text))[-1]
        refresh_title = next(
            match for match in re.finditer(r'(?m)^商品刷新后的搜索回响\r?$', export_text)
            if match.start() > cold_start_title.end()
        )
        cold_start_csv = export_text[cold_start_title.end():refresh_title.start()]
        for text in (cold_start_html, cold_start_csv):
            self.assertNotIn('导出不应出现的商品标题', text)
            self.assertNotIn('导出不应出现的搜索词', text)
            self.assertNotIn(self.seller.username, text)

        rows = list(csv.reader(io.StringIO(export_text)))
        section = next(index for index, row in enumerate(rows) if row and row[0] == '新商品搜索曝光冷启动')
        latency_head = next(index for index in range(section, len(rows)) if rows[index][:3] == ['首曝延迟分桶', '商品数', '占比（%）'])
        category_head = next(index for index in range(section, len(rows)) if rows[index][:8] == [
            '分类冷启动', '队列商品', '已首曝', '未首曝', '首曝后点击', '中位等待（小时）', 'P90 等待（小时）', '未首曝占比（%）',
        ])
        list_head = next(index for index in range(section, len(rows)) if rows[index][:10] == [
            '新商品清单', '分类', '交易方式', '状态', '发布时间', '首曝时间', '首曝延迟（小时）', '点击', '信号',
        ])
        item_rows = []
        for row in rows[list_head + 1:]:
            if row and row[0] == '边界说明':
                break
            if row and row[0] == str(item.pk):
                item_rows.append(row)
        self.assertEqual(len(item_rows), 1)
        self.assertLess(latency_head, category_head)
        self.assertLess(category_head, list_head)
