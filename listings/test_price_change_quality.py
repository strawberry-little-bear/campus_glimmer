# -*- coding: utf-8 -*-
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, ItemPriceChange
from .price_change_quality import build_price_change_quality


class PriceChangeQualityTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.start = self.now - timedelta(days=1)
        self.end = self.now + timedelta(days=1)
        self.seller = User.objects.create_user(
            username='price-quality-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='价格轨迹运营分类')

    def make_item(self, *, title='价格轨迹商品', seller=None, category=None):
        return Item.objects.create(
            title=title,
            description='用于出售价格轨迹运营统计测试',
            trade_mode='sale',
            price=Decimal('100.00'),
            category=category or self.category,
            condition='良好',
            seller=seller or self.seller,
            status='sold',
        )

    def make_change(self, item, old_price, new_price, changed_by=None):
        return ItemPriceChange.objects.create(
            item=item,
            old_price=Decimal(old_price),
            new_price=Decimal(new_price),
            changed_by=changed_by or self.seller,
        )

    def test_empty_window_has_no_zero_direction_share(self):
        result = build_price_change_quality(self.start, self.end, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertTrue(result['is_small_sample'])
        self.assertEqual(result['totals']['change_count'], 0)
        self.assertIsNone(result['totals']['drop_share'])
        self.assertIn('不会在这里回填', result['summary'])

    def test_direction_counts_and_magnitude_buckets_are_separate(self):
        item = self.make_item()
        self.make_change(item, '100.00', '95.00')   # 降价 5–10% 边界
        self.make_change(item, '100.00', '90.00')   # 降价 5–10%
        self.make_change(item, '100.00', '50.00')   # 降价 20% 以上
        self.make_change(item, '100.00', '101.00')  # 上涨 0–5%

        result = build_price_change_quality(self.start, self.end, now=self.now)
        rows = {
            (row['direction'], row['bucket']): row['count']
            for row in result['magnitude_rows']
        }

        self.assertEqual(result['totals']['drop_count'], 3)
        self.assertEqual(result['totals']['rise_count'], 1)
        self.assertEqual(rows[('降价', '0–5%')], 0)
        self.assertEqual(rows[('降价', '5–10%')], 1)
        self.assertEqual(rows[('降价', '10–20%')], 1)
        self.assertEqual(rows[('降价', '20% 以上')], 1)
        self.assertEqual(rows[('上涨', '0–5%')], 1)

    def test_zero_base_is_counted_but_never_enters_magnitude(self):
        item = self.make_item()
        self.make_change(item, '0.00', '30.00')
        self.make_change(item, '30.00', '20.00')

        result = build_price_change_quality(self.start, self.end, now=self.now)

        self.assertEqual(result['totals']['zero_base_count'], 1)
        self.assertEqual(result['totals']['drop_count'], 1)
        self.assertEqual(
            sum(row['count'] for row in result['magnitude_rows'] if row['direction'] == '降价'),
            1,
        )

    def test_repeat_item_and_distinct_seller_counts(self):
        other_seller = User.objects.create_user(
            username='price-quality-other-seller', password='safe-password-123',
        )
        once = self.make_item(title='只调一次')
        twice = self.make_item(title='调两次')
        three_times = self.make_item(title='调三次', seller=other_seller)

        self.make_change(once, '40.00', '30.00')
        self.make_change(twice, '40.00', '30.00')
        self.make_change(twice, '30.00', '25.00')
        for old_price, new_price in (('60.00', '50.00'), ('50.00', '40.00'), ('40.00', '35.00')):
            self.make_change(three_times, old_price, new_price)

        result = build_price_change_quality(self.start, self.end, now=self.now)
        repeat_rows = {row['key']: row['item_count'] for row in result['repeat_rows']}

        self.assertEqual(result['totals']['item_count'], 3)
        self.assertEqual(result['totals']['seller_count'], 2)
        self.assertEqual(repeat_rows['exactly_one'], 1)
        self.assertEqual(repeat_rows['exactly_two'], 1)
        self.assertEqual(repeat_rows['three_or_more'], 1)

    def test_categories_use_current_view_and_small_samples_do_not_give_shares(self):
        other_category = Category.objects.create(name='另一个当前分类')
        first = self.make_item()
        second = self.make_item(title='其他分类商品', category=other_category)

        self.make_change(first, '40.00', '30.00')
        self.make_change(second, '80.00', '70.00')

        result = build_price_change_quality(self.start, self.end, now=self.now)
        rows = {row['name']: row for row in result['category_rows']}

        self.assertEqual(rows['价格轨迹运营分类']['change_count'], 1)
        self.assertEqual(rows['另一个当前分类']['change_count'], 1)
        self.assertTrue(rows['价格轨迹运营分类']['is_small_sample'])
        self.assertIsNone(rows['价格轨迹运营分类']['drop_share'])

    def test_enough_samples_enable_direction_share_and_percentiles(self):
        item = self.make_item()
        for ratio in range(1, 13):
            self.make_change(item, '100.00', str(100 - ratio))

        result = build_price_change_quality(self.start, self.end, now=self.now)

        self.assertFalse(result['is_small_sample'])
        self.assertEqual(result['totals']['drop_share'], Decimal('100.00'))
        self.assertEqual(result['directions']['drop']['median_ratio'], Decimal('7.00'))
        self.assertEqual(result['directions']['drop']['p90_ratio'], Decimal('11.00'))

    def test_builder_does_not_change_business_rows(self):
        item = self.make_item()
        change = self.make_change(item, '40.00', '30.00')
        item_updated_at = item.updated_at

        build_price_change_quality(self.start, self.end, now=self.now)

        item.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(item.title, '价格轨迹商品')
        self.assertEqual(item.updated_at, item_updated_at)
        self.assertEqual(change.old_price, Decimal('40.00'))
        self.assertEqual(change.new_price, Decimal('30.00'))


class PriceChangeQualityDashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='price-quality-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.staff)
        self.category = Category.objects.create(name='轨迹面板分类')
        self.item = Item.objects.create(
            title='轨迹面板商品',
            description='用于面板渲染测试',
            trade_mode='sale',
            price='30.00',
            category=self.category,
            condition='良好',
            seller=self.staff,
        )
        ItemPriceChange.objects.create(
            item=self.item,
            old_price=Decimal('40.00'),
            new_price=Decimal('30.00'),
            changed_by=self.staff,
        )

    def test_operations_dashboard_renders_panel_and_boundaries(self):
        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '出售价格轨迹质量')
        self.assertContains(response, '当前分类视图')
        self.assertContains(response, '零元基线')
        self.assertContains(response, '不把降价率解释成成交率')

    def test_operations_dashboard_export_contains_same_sections(self):
        response = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        section = text.split('出售价格轨迹质量,数值', 1)[1].split('重新上架的那件商品', 1)[0]
        self.assertIn('方向与幅度', section)
        self.assertIn('幅度分档', section)
        self.assertIn('重复调价', section)
        self.assertIn('当前分类视图', section)
        self.assertNotIn('轨迹面板商品', section)
        self.assertNotIn('price-quality-staff', section)
