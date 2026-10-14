# -*- coding: utf-8 -*-
import csv
import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from listings.models import Category, Favorite, Item, Order, SearchClick, SearchImpression
from listings.refresh_response import MIN_GROUP_ITEMS, build_refresh_response


class RefreshResponseTests(TestCase):
    """Keep the refresh comparison observable and free of identities."""

    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='refresh-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='refresh-buyer', password='safe-password-123',
        )
        self.buyer_two = User.objects.create_user(
            username='refresh-buyer-two', password='safe-password-123',
        )
        self.category = Category.objects.create(name='刷新回响测试分类')

    def make_item(self, *, title='刷新回响商品', refresh_count=1, created_at=None, refresh_at=None):
        refresh_at = refresh_at or self.now - timedelta(days=4)
        created_at = created_at or self.now - timedelta(days=8)
        item = Item.objects.create(
            title=title,
            description='用于商品刷新后的搜索回响统计。',
            price='46.00',
            category=self.category,
            condition='九成新',
            seller=self.seller,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=created_at,
            last_refreshed_at=refresh_at,
            refresh_count=refresh_count,
        )
        item.refresh_from_db()
        return item

    def create_event(self, model, *, item, occurred_at, favorite_user=None):
        if model is SearchImpression:
            row = model.objects.create(item=item, position=1)
        elif model is SearchClick:
            row = model.objects.create(item=item, position=1)
        elif model is Favorite:
            row = model.objects.create(user=favorite_user or self.buyer, item=item)
        else:
            row = Order.objects.create(
                item=item,
                buyer=self.buyer,
                seller=self.seller,
                agreed_price=item.price,
            )
        model.objects.filter(pk=row.pk).update(created_at=occurred_at)
        return row

    def test_empty_period_keeps_all_refresh_ordinals(self):
        result = build_refresh_response(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(
            [row['label'] for row in result['rows']],
            ['第 1 次', '第 2 次', '第 3 次', '第 4 次'],
        )
        self.assertTrue(all(row['refreshes'] == 0 for row in result['rows']))
        self.assertFalse(result['overall']['has_sample'])
        self.assertEqual(result['summary']['observed_refreshes'], 0)

    def test_immature_refresh_is_counted_but_not_compared(self):
        self.make_item(refresh_at=self.now - timedelta(hours=71))

        result = build_refresh_response(days=30, now=self.now)

        self.assertEqual(result['summary']['immature_refreshes'], 1)
        self.assertEqual(result['summary']['eligible_refreshes'], 0)
        self.assertEqual(result['overall']['refreshes'], 0)

    def test_refresh_without_full_pre_window_is_counted_separately(self):
        self.make_item(
            created_at=self.now - timedelta(days=5),
            refresh_at=self.now - timedelta(days=4),
        )

        result = build_refresh_response(days=30, now=self.now)

        self.assertEqual(result['summary']['no_full_pre_window_refreshes'], 1)
        self.assertEqual(result['summary']['eligible_refreshes'], 0)

    def test_refresh_before_publication_is_not_eligible(self):
        self.make_item(
            created_at=self.now - timedelta(days=2),
            refresh_at=self.now - timedelta(days=4),
        )

        result = build_refresh_response(days=30, now=self.now)

        self.assertEqual(result['summary']['invalid_timestamp_refreshes'], 1)
        self.assertEqual(result['summary']['eligible_refreshes'], 0)

    def test_windows_are_adjacent_and_exclude_the_common_end(self):
        item = self.make_item()
        refresh_at = item.last_refreshed_at
        before_start = refresh_at - timedelta(hours=72)
        after_end = refresh_at + timedelta(hours=72)

        self.create_event(SearchImpression, item=item, occurred_at=before_start)
        self.create_event(SearchImpression, item=item, occurred_at=refresh_at)
        self.create_event(SearchImpression, item=item, occurred_at=after_end)

        result = build_refresh_response(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['before_impressions'], 1)
        self.assertEqual(row['after_impressions'], 1)
        self.assertEqual(row['after_median_impressions'], 1.0)

    def test_search_click_rate_uses_the_same_window_as_its_impressions(self):
        item = self.make_item()
        refresh_at = item.last_refreshed_at

        for offset in (timedelta(hours=72), timedelta(hours=48)):
            self.create_event(SearchImpression, item=item, occurred_at=refresh_at - offset)
        self.create_event(SearchClick, item=item, occurred_at=refresh_at - timedelta(hours=40))

        for offset in (timedelta(hours=1), timedelta(hours=30)):
            self.create_event(SearchImpression, item=item, occurred_at=refresh_at + offset)
        self.create_event(SearchClick, item=item, occurred_at=refresh_at + timedelta(hours=2))

        result = build_refresh_response(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['before_impressions'], 2)
        self.assertEqual(row['before_clicks'], 1)
        self.assertEqual(row['before_click_rate'], 50.0)
        self.assertEqual(row['after_impressions'], 2)
        self.assertEqual(row['after_click_rate'], 50.0)

    def test_favorites_and_orders_use_their_own_timestamps(self):
        item = self.make_item()
        refresh_at = item.last_refreshed_at

        self.create_event(Favorite, item=item, occurred_at=refresh_at - timedelta(hours=10))
        self.create_event(
            Favorite, item=item, occurred_at=refresh_at + timedelta(hours=10),
            favorite_user=self.buyer_two,
        )
        self.create_event(Order, item=item, occurred_at=refresh_at + timedelta(hours=20))

        result = build_refresh_response(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['before_favorites'], 1)
        self.assertEqual(row['after_favorites'], 1)
        self.assertEqual(row['before_orders'], 0)
        self.assertEqual(row['after_orders'], 1)

    def test_refresh_ordinal_groups_do_not_blend_each_other(self):
        self.make_item(title='第一次刷新', refresh_count=1)
        self.make_item(title='第三次刷新', refresh_count=3)

        result = build_refresh_response(days=30, now=self.now)
        counts = {row['label']: row['refreshes'] for row in result['rows']}

        self.assertEqual(
            counts,
            {'第 1 次': 1, '第 2 次': 0, '第 3 次': 1, '第 4 次': 0},
        )
        self.assertEqual(result['overall']['refreshes'], 2)

    def test_group_needs_twelve_mature_refreshes_for_a_usable_reading(self):
        for index in range(MIN_GROUP_ITEMS):
            self.make_item(title=f'门槛样本{index}', refresh_count=2)

        result = build_refresh_response(days=30, now=self.now)
        row = next(row for row in result['rows'] if row['key'] == 'ordinal-2')

        self.assertEqual(row['refreshes'], MIN_GROUP_ITEMS)
        self.assertTrue(row['has_sample'])

    def test_returned_payload_has_no_identity_or_free_text(self):
        item = self.make_item(title='不应出现的商品标题')
        self.create_event(SearchImpression, item=item, occurred_at=item.last_refreshed_at)
        self.create_event(Order, item=item, occurred_at=item.last_refreshed_at)

        result = build_refresh_response(days=30, now=self.now)
        rendered = repr(result)

        self.assertNotIn(self.seller.username, rendered)
        self.assertNotIn(self.buyer.username, rendered)
        self.assertNotIn('不应出现的商品标题', rendered)
        self.assertEqual(
            set(result['rows'][0]),
            {
                'key', 'label', 'refreshes', 'before_impressions',
                'after_impressions', 'after_median_impressions',
                'before_clicks', 'after_clicks', 'before_click_rate',
                'after_click_rate', 'before_favorites', 'after_favorites',
                'before_orders', 'after_orders', 'has_sample',
            },
        )



class RefreshResponseDashboardTests(TestCase):
    """Keep the screen, CSV export, and privacy boundary in the same shape."""

    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='refresh-response-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='refresh-response-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='看板回响测试分类')
        self.staff = User.objects.create_user(
            username='refresh-response-staff', password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def make_item(self, *, title='刷新回响商品'):
        created_at = self.now - timedelta(days=8)
        refresh_at = self.now - timedelta(days=4)
        item = Item.objects.create(
            title=title,
            description='用于商品刷新后的搜索回响看板测试。',
            price='52.00',
            category=self.category,
            condition='九成新',
            seller=self.seller,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=created_at,
            last_refreshed_at=refresh_at,
            refresh_count=1,
        )
        item.refresh_from_db()
        return item

    def test_dashboard_and_csv_show_the_same_refresh_response_contract(self):
        item = self.make_item(title='回响导出不应带出的商品标题')
        impression = SearchImpression.objects.create(item=item, position=1)
        SearchImpression.objects.filter(pk=impression.pk).update(
            created_at=item.last_refreshed_at + timedelta(hours=2),
        )
        order = Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=self.seller,
            agreed_price=item.price,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=item.last_refreshed_at + timedelta(hours=3),
        )

        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        html = page.content.decode('utf-8')
        panel = html.split('商品刷新后的搜索回响', 1)[1].split(
            '重新上架的那件商品，为什么一回来就沉底', 1,
        )[0]
        for label in ('第 1 次', '第 2 次', '第 3 次', '第 4 次', '全部次序'):
            self.assertIn(label, panel)
        self.assertIn('刷新次序对照（前 72 小时 / 后 72 小时）', panel)
        self.assertIn('哪些刷新没有进入对照', panel)

        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        titles = [row[0] if row else '' for row in rows]
        start = titles.index('商品刷新后的搜索回响')
        end = titles.index('重新上架的那件商品，为什么一回来就沉底')
        section = rows[start:end]

        self.assertIn(
            [
                '刷新次序', '成熟样本', '前窗曝光', '后窗曝光', '后窗中位曝光',
                '前窗点击率（%）', '后窗点击率（%）', '前窗收藏', '后窗收藏',
                '前窗预约', '后窗预约', '样本状态',
            ],
            section,
        )
        self.assertIn(['状态', '数量', '处理方式'], section)
        rendered_section = '\n'.join(','.join(row) for row in section)
        for secret in (self.seller.username, self.buyer.username, item.title):
            self.assertNotIn(secret, rendered_section)
        main_rows = [row for row in section if len(row) == 12 and row[0].startswith(('第 ', '全部'))]
        self.assertEqual(len(main_rows), 5)
        self.assertTrue(all(len(row) == 12 for row in main_rows))
        self.assertNotIn('订单号', main_rows[0])
