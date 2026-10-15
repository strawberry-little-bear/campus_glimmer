# -*- coding: utf-8 -*-
import csv
import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from listings.models import (
    Category, Favorite, Item, ItemPriceChange, Order, SearchClick,
    SearchImpression,
)
from listings.price_response import (
    MAGNITUDE_BUCKETS, MIN_GROUP_CHANGES, build_price_response,
)


class PriceResponseTests(TestCase):
    """Keep the price comparison observational, auditable, and identity-free."""

    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='price-response-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='price-response-buyer', password='safe-password-123',
        )
        self.buyer_two = User.objects.create_user(
            username='price-response-buyer-two', password='safe-password-123',
        )
        self.category = Category.objects.create(name='价格回响测试分类')

    def make_item(
        self, *, title='价格回响商品', trade_mode='sale', created_at=None,
        price='80.00',
    ):
        created_at = created_at or self.now - timedelta(days=8)
        item = Item.objects.create(
            title=title,
            description='用于价格调整后的搜索回响统计。',
            trade_mode=trade_mode,
            price=price,
            category=self.category,
            condition='九成新',
            seller=self.seller,
        )
        Item.objects.filter(pk=item.pk).update(created_at=created_at)
        item.refresh_from_db()
        return item

    def make_change(self, item, *, old_price='80.00', new_price='60.00', changed_at=None):
        changed_at = changed_at or self.now - timedelta(days=4)
        change = ItemPriceChange.objects.create(
            item=item, old_price=old_price, new_price=new_price,
        )
        ItemPriceChange.objects.filter(pk=change.pk).update(created_at=changed_at)
        change.refresh_from_db()
        return change

    def create_event(self, model, *, item, occurred_at):
        if model is SearchImpression:
            row = model.objects.create(item=item, position=1)
        elif model is SearchClick:
            row = model.objects.create(item=item, position=1)
        elif model is Favorite:
            row = model.objects.create(user=self.buyer, item=item)
        else:
            row = Order.objects.create(
                item=item, buyer=self.buyer, seller=self.seller,
                agreed_price=item.price,
            )
        model.objects.filter(pk=row.pk).update(created_at=occurred_at)
        return row

    def test_empty_period_keeps_a_stable_contract(self):
        result = build_price_response(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['summary']['observed_changes'], 0)
        self.assertEqual([row['label'] for row in result['direction_rows']], ['降价', '上涨', '零元基线'])
        self.assertEqual(len(result['magnitude_rows']), len(MAGNITUDE_BUCKETS) * 2)
        self.assertFalse(result['overall']['has_sample'])
        self.assertEqual(result['overall']['changes'], 0)

    def test_windows_are_adjacent_and_the_shared_endpoint_belongs_to_after(self):
        item = self.make_item(title='边界价格回响商品')
        change = self.make_change(item)
        before_start = change.created_at - timedelta(hours=72)
        after_end = change.created_at + timedelta(hours=72)

        self.create_event(SearchImpression, item=item, occurred_at=before_start)
        self.create_event(SearchImpression, item=item, occurred_at=change.created_at)
        self.create_event(SearchImpression, item=item, occurred_at=after_end)
        self.create_event(SearchClick, item=item, occurred_at=before_start)
        self.create_event(SearchClick, item=item, occurred_at=change.created_at)
        favorite_before = Favorite.objects.create(user=self.buyer, item=item)
        Favorite.objects.filter(pk=favorite_before.pk).update(created_at=before_start)
        favorite_after = Favorite.objects.create(user=self.buyer_two, item=item)
        Favorite.objects.filter(pk=favorite_after.pk).update(created_at=change.created_at)
        order_before = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            agreed_price=item.price, status='completed',
        )
        Order.objects.filter(pk=order_before.pk).update(created_at=before_start)
        self.create_event(Order, item=item, occurred_at=change.created_at)

        result = build_price_response(days=30, now=self.now)
        row = result['direction_rows'][0]

        self.assertEqual(row['changes'], 1)
        self.assertEqual(row['before_impressions'], 1)
        self.assertEqual(row['after_impressions'], 1)
        self.assertEqual(row['before_click_rate'], 100.0)
        self.assertEqual(row['after_click_rate'], 100.0)
        self.assertEqual(row['before_favorites'], 1)
        self.assertEqual(row['after_favorites'], 1)
        self.assertEqual(row['before_orders'], 1)
        self.assertEqual(row['after_orders'], 1)

    def test_immature_change_is_counted_but_not_compared(self):
        item = self.make_item()
        self.make_change(item, changed_at=self.now - timedelta(hours=71))

        result = build_price_response(days=30, now=self.now)

        self.assertEqual(result['summary']['immature_changes'], 1)
        self.assertEqual(result['summary']['eligible_changes'], 0)
        self.assertEqual(result['overall']['changes'], 0)

    def test_publication_boundary_without_a_full_pre_window_is_excluded(self):
        changed_at = self.now - timedelta(days=4)
        item = self.make_item(created_at=changed_at - timedelta(hours=36))
        self.make_change(item, changed_at=changed_at)

        result = build_price_response(days=30, now=self.now)

        self.assertEqual(result['summary']['no_full_pre_window_changes'], 1)
        self.assertEqual(result['summary']['eligible_changes'], 0)

    def test_change_at_or_before_publication_is_an_invalid_timestamp(self):
        item = self.make_item(created_at=self.now - timedelta(days=3))
        self.make_change(item, changed_at=self.now - timedelta(days=4))

        result = build_price_response(days=30, now=self.now)

        self.assertEqual(result['summary']['invalid_timestamp_changes'], 1)
        self.assertEqual(result['summary']['eligible_changes'], 0)

    def test_adjacent_change_on_the_same_item_is_not_compared_twice(self):
        item = self.make_item()
        self.make_change(item, changed_at=self.now - timedelta(days=4))
        self.make_change(
            item, old_price='60.00', new_price='50.00',
            changed_at=self.now - timedelta(hours=70),
        )

        result = build_price_response(days=30, now=self.now)

        self.assertEqual(result['summary']['adjacent_change_changes'], 1)
        self.assertEqual(result['summary']['immature_changes'], 1)
        self.assertEqual(result['summary']['eligible_changes'], 0)

    def test_current_non_sale_items_are_counted_separately(self):
        for trade_mode in ('free', 'borrow'):
            item = self.make_item(trade_mode=trade_mode)
            self.make_change(item)

        result = build_price_response(days=30, now=self.now)

        self.assertEqual(result['summary']['non_sale_changes'], 2)
        self.assertEqual(result['summary']['eligible_changes'], 0)

    def test_negative_price_is_counted_as_a_price_anomaly(self):
        item = self.make_item()
        self.make_change(item, old_price='-1.00', new_price='20.00')

        result = build_price_response(days=30, now=self.now)

        self.assertEqual(result['summary']['invalid_price_changes'], 1)
        self.assertEqual(result['summary']['eligible_changes'], 0)

    def test_directions_and_the_zero_baseline_are_separate(self):
        drop = self.make_item(price='80.00')
        rise = self.make_item(price='20.00')
        zero = self.make_item(price='10.00')
        self.make_change(drop, old_price='80.00', new_price='70.00')
        self.make_change(rise, old_price='20.00', new_price='25.00')
        self.make_change(zero, old_price='0.00', new_price='10.00')

        result = build_price_response(days=30, now=self.now)
        rows = {row['key']: row for row in result['direction_rows']}

        self.assertEqual(rows['drop']['changes'], 1)
        self.assertEqual(rows['rise']['changes'], 1)
        self.assertEqual(rows['zero']['changes'], 1)
        self.assertEqual(result['overall']['changes'], 3)
        self.assertTrue(all(row['bucket'] == '—' for row in result['direction_rows']))

    def test_magnitude_buckets_reuse_the_price_quality_thresholds(self):
        cases = (
            ('100.00', '99.00', '0–5%'),
            ('100.00', '95.00', '5–10%'),
            ('100.00', '90.00', '10–20%'),
            ('100.00', '80.00', '20% 以上'),
        )
        for index, (old_price, new_price, _) in enumerate(cases):
            item = self.make_item(title=f'幅度边界商品 {index}', price=new_price)
            self.make_change(item, old_price=old_price, new_price=new_price)

        result = build_price_response(days=30, now=self.now)
        counts = {
            (row['direction'], row['bucket']): row['changes']
            for row in result['magnitude_rows']
        }

        self.assertEqual(
            [label for _, label in MAGNITUDE_BUCKETS],
            ['0–5%', '5–10%', '10–20%', '20% 以上'],
        )
        for direction, (old_price, new_price, bucket) in (
            *[('降价', case) for case in cases],
        ):
            self.assertEqual(counts[(direction, bucket)], 1)
        self.assertEqual(sum(counts.values()), 4)

    def test_the_sample_threshold_is_twelve_mature_changes(self):
        for index in range(MIN_GROUP_CHANGES):
            item = self.make_item(title=f'样本门槛商品 {index}')
            self.make_change(item)

        result = build_price_response(days=30, now=self.now)

        self.assertEqual(result['direction_rows'][0]['changes'], MIN_GROUP_CHANGES)
        self.assertTrue(result['direction_rows'][0]['has_sample'])
        self.assertTrue(result['overall']['has_sample'])

    def test_click_rate_has_no_fake_zero_when_there_are_no_impressions(self):
        item = self.make_item()
        change = self.make_change(item)
        self.create_event(SearchClick, item=item, occurred_at=change.created_at)

        result = build_price_response(days=30, now=self.now)
        row = result['direction_rows'][0]

        self.assertEqual(row['after_impressions'], 0)
        self.assertIsNone(row['before_click_rate'])
        self.assertIsNone(row['after_click_rate'])

    def test_building_reads_only_and_never_touches_browsing_history(self):
        item = self.make_item(title='只读价格回响商品')
        change = self.make_change(item)
        self.create_event(SearchImpression, item=item, occurred_at=change.created_at)

        with CaptureQueriesContext(connection) as captured:
            result = build_price_response(days=30, now=self.now)

        write_prefixes = ('INSERT ', 'UPDATE ', 'DELETE ', 'REPLACE ')
        self.assertGreaterEqual(len(captured.captured_queries), 4)
        for query in captured.captured_queries:
            self.assertFalse(query['sql'].upper().startswith(write_prefixes))
            self.assertNotIn('BrowsingHistory', query['sql'])
        self.assertEqual(result['direction_rows'][0]['after_impressions'], 1)

    def test_the_result_has_no_personally_identifying_fields(self):
        item = self.make_item(title='不应导出的价格回响标题')
        change = self.make_change(item)
        self.create_event(Favorite, item=item, occurred_at=change.created_at)

        result = build_price_response(days=30, now=self.now)
        rendered = repr(result)

        for secret in (
            self.seller.username, self.buyer.username, item.title, item.description,
        ):
            self.assertNotIn(secret, rendered)
        expected_fields = {
            'key', 'direction', 'bucket', 'label', 'changes',
            'before_impressions', 'after_impressions',
            'after_median_impressions', 'before_click_rate',
            'after_click_rate', 'before_favorites', 'after_favorites',
            'before_orders', 'after_orders', 'has_sample',
        }
        self.assertEqual(set(result['direction_rows'][0]), expected_fields)


class PriceResponseDashboardTests(TestCase):
    """Keep the page and export on one contract without exposing identities."""

    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='price-response-export-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='price-response-export-buyer', password='safe-password-123',
        )
        self.staff = User.objects.create_user(
            username='price-response-staff', password='safe-password-123', is_staff=True,
        )
        self.category = Category.objects.create(name='价格回响导出分类')
        self.client.force_login(self.staff)

    def test_dashboard_and_csv_show_the_same_price_response_contract(self):
        item = Item.objects.create(
            title='回响导出不应带出的调价标题',
            description='用于价格回响看板导出测试。',
            price='70.00', trade_mode='sale', category=self.category,
            condition='九成新', seller=self.seller,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=self.now - timedelta(days=8),
        )
        item.refresh_from_db()
        changed_at = self.now - timedelta(days=4)
        change = ItemPriceChange.objects.create(
            item=item, old_price='80.00', new_price='70.00',
        )
        ItemPriceChange.objects.filter(pk=change.pk).update(created_at=changed_at)
        impression = SearchImpression.objects.create(item=item, position=1)
        SearchImpression.objects.filter(pk=impression.pk).update(
            created_at=changed_at + timedelta(hours=2),
        )
        favorite = Favorite.objects.create(user=self.buyer, item=item)
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=changed_at + timedelta(hours=3),
        )

        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        html = page.content.decode('utf-8')
        panel = html.split('价格调整后的搜索回响', 1)[1].split('Price change quality', 1)[0]
        for label in ('降价', '上涨', '零元基线', '全部', '0–5%', '20% 以上'):
            self.assertIn(label, panel)
        self.assertIn('方向对照（前 72 小时 / 后 72 小时）', panel)
        self.assertIn('哪些调价没有进入对照', panel)

        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        titles = [row[0] if row else '' for row in rows]
        start = titles.index('价格调整后的搜索回响')
        end = titles.index('出售价格轨迹质量')
        section = rows[start:end]
        expected_header = [
            '方向', '成熟样本', '前窗曝光', '后窗曝光', '后窗中位曝光',
            '前窗点击率（%）', '后窗点击率（%）', '前窗收藏', '后窗收藏',
            '前窗预约', '后窗预约', '样本状态',
        ]
        self.assertIn(expected_header, section)
        self.assertIn(['状态', '数量', '处理方式'], section)
        rendered_section = '\n'.join(','.join(row) for row in section)
        for secret in (self.seller.username, self.buyer.username, item.title):
            self.assertNotIn(secret, rendered_section)
