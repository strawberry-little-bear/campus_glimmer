# -*- coding: utf-8 -*-
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, ItemPriceChange
from .price_history import (
    build_item_price_history, build_item_price_history_maintenance,
)


class ItemPriceHistoryTests(TestCase):
    """商品详情价格轨迹的展示门槛、隐私粒度与只读边界。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='history-seller', password='safe-password-123',
        )
        self.operator = User.objects.create_user(
            username='history-operator', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='history-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='详情价格轨迹分类')
        self.item = Item.objects.create(
            title='价格轨迹详情商品',
            description='用于测试商品详情价格轨迹',
            trade_mode='sale',
            price=Decimal('70.00'),
            category=self.category,
            condition='9成新',
            seller=self.seller,
            status='available',
        )

    def make_history(self, prices):
        changes = []
        for old_price, new_price in prices:
            changes.append(ItemPriceChange.objects.create(
                item=self.item,
                old_price=Decimal(old_price),
                new_price=Decimal(new_price),
                changed_by=self.operator,
            ))
        self.item.price = Decimal(prices[-1][1])
        self.item.save(update_fields=['price', 'updated_at'])
        return changes

    def test_fewer_than_three_changes_stay_hidden(self):
        self.make_history([('100.00', '120.00'), ('120.00', '95.00')])

        result = build_item_price_history(self.item)

        self.assertFalse(result['available'])

    def test_three_continuous_changes_build_start_and_current_points(self):
        changes = self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        created_times = [
            timezone.make_aware(datetime(2025, 3, 1, 10, 12)),
            timezone.make_aware(datetime(2025, 3, 8, 11, 13)),
            timezone.make_aware(datetime(2025, 4, 3, 9, 11)),
        ]
        for change, created_at in zip(changes, created_times):
            ItemPriceChange.objects.filter(pk=change.pk).update(created_at=created_at)
        result = build_item_price_history(self.item)

        self.assertTrue(result['available'])
        self.assertEqual(result['total_change_count'], 3)
        self.assertEqual(result['displayed_change_count'], 3)
        self.assertFalse(result['window_is_partial'])
        self.assertEqual(len(result['points']), 4)
        self.assertEqual(result['points'][0]['price'], Decimal('100.00'))
        self.assertTrue(result['points'][0]['is_start'])
        self.assertTrue(result['points'][-1]['is_current'])
        self.assertEqual(result['points'][-1]['price'], Decimal('70.00'))
        self.assertIn('降价', result['points'][-1]['change_label'])
        self.assertIn('2025年04月03日', result['points'][-1]['date_label'])
        self.assertEqual(len(result['chart']['markers']), 4)
        self.assertTrue(result['chart']['markers'][0]['is_start'])
        self.assertTrue(result['chart']['markers'][-1]['is_current'])

    def test_broken_trajectory_fails_closed(self):
        self.make_history([
            ('100.00', '120.00'), ('95.00', '80.00'), ('80.00', '70.00'),
        ])

        result = build_item_price_history(self.item)

        self.assertFalse(result['available'])

    def test_current_price_mismatch_fails_closed(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        self.item.price = Decimal('75.00')
        self.item.save(update_fields=['price', 'updated_at'])

        result = build_item_price_history(self.item)

        self.assertFalse(result['available'])

    def test_more_than_six_changes_show_only_the_latest_window(self):
        self.make_history([
            ('100.00', '110.00'),
            ('110.00', '120.00'),
            ('120.00', '130.00'),
            ('130.00', '140.00'),
            ('140.00', '150.00'),
            ('150.00', '160.00'),
            ('160.00', '170.00'),
        ])

        result = build_item_price_history(self.item)

        self.assertTrue(result['available'])
        self.assertEqual(result['total_change_count'], 7)
        self.assertEqual(result['displayed_change_count'], 6)
        self.assertTrue(result['window_is_partial'])
        self.assertIn('最近 6 次（共 7 次调整）', result['scope_label'])
        self.assertEqual(result['points'][0]['price'], Decimal('110.00'))
        self.assertEqual(result['points'][-1]['price'], Decimal('170.00'))

    def test_free_and_borrow_modes_do_not_show_sale_history(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        self.item.trade_mode = 'free'
        self.item.save(update_fields=['trade_mode', 'updated_at'])
        self.assertFalse(build_item_price_history(self.item)['available'])

        self.item.trade_mode = 'borrow'
        self.item.save(update_fields=['trade_mode', 'updated_at'])
        self.assertFalse(build_item_price_history(self.item)['available'])

    def test_zero_base_is_labeled_without_percentage(self):
        self.make_history([('0.00', '30.00'), ('30.00', '20.00'), ('20.00', '18.00')])

        result = build_item_price_history(self.item)

        self.assertTrue(result['available'])
        self.assertIn('零元基线', result['points'][1]['change_label'])
        self.assertNotIn('%', result['points'][1]['change_label'])
        self.assertIn('%', result['points'][2]['change_label'])

    def test_sold_item_keeps_historical_trajectory(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        self.item.status = 'sold'
        self.item.save(update_fields=['status', 'updated_at'])

        self.assertTrue(build_item_price_history(self.item)['available'])

    def test_builder_is_read_only_and_does_not_expose_operator(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        connection.queries_log.clear()

        with self.assertNumQueries(2):
            result = build_item_price_history(self.item)

        statements = [query['sql'].lstrip().upper() for query in connection.queries]
        self.assertEqual(len(statements), 2)
        self.assertTrue(all(statement.startswith('SELECT') for statement in statements))
        self.assertNotIn(self.operator.username, str(result))

    def test_item_detail_renders_accessible_history_without_time_or_operator(self):
        changes = self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        created_times = [
            timezone.make_aware(datetime(2025, 3, 1, 10, 12)),
            timezone.make_aware(datetime(2025, 3, 8, 11, 13)),
            timezone.make_aware(datetime(2025, 4, 3, 9, 11)),
        ]
        for change, created_at in zip(changes, created_times):
            ItemPriceChange.objects.filter(pk=change.pk).update(created_at=created_at)
        response = self.client.get(reverse('item_detail', args=[self.item.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '最近调价记录')
        self.assertContains(response, '价格走势图')
        self.assertContains(response, 'role="img"')
        self.assertContains(response, '2025年04月03日')
        self.assertNotContains(response, '09:11')
        self.assertNotContains(response, self.operator.username)
        self.assertContains(response, '不展示操作人')

    def test_item_detail_hides_incomplete_history(self):
        self.make_history([('100.00', '120.00'), ('120.00', '95.00')])

        response = self.client.get(reverse('item_detail', args=[self.item.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '最近调价记录')

    def test_maintenance_is_visible_only_to_item_seller(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])

        self.assertTrue(
            build_item_price_history_maintenance(self.item, self.seller)['visible']
        )
        self.assertFalse(
            build_item_price_history_maintenance(self.item, self.buyer)['visible']
        )
        self.assertFalse(
            build_item_price_history_maintenance(self.item, AnonymousUser())['visible']
        )

    def test_maintenance_only_applies_to_sale_items(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        self.item.trade_mode = 'free'
        self.item.save(update_fields=['trade_mode', 'updated_at'])
        self.assertFalse(
            build_item_price_history_maintenance(self.item, self.seller)['visible']
        )

        self.item.trade_mode = 'borrow'
        self.item.save(update_fields=['trade_mode', 'updated_at'])
        self.assertFalse(
            build_item_price_history_maintenance(self.item, self.seller)['visible']
        )

    def test_maintenance_distinguishes_empty_waiting_and_public_states(self):
        empty = build_item_price_history_maintenance(self.item, self.seller)
        self.assertEqual(empty['state'], 'empty')
        self.assertEqual(empty['tone'], 'info')
        self.assertEqual(empty['title'], '暂无轨迹')
        self.assertEqual(empty['change_count'], 0)

        self.make_history([('100.00', '120.00'), ('120.00', '95.00')])
        waiting = build_item_price_history_maintenance(self.item, self.seller)
        self.assertEqual(waiting['state'], 'waiting')
        self.assertEqual(waiting['title'], '等待积累')
        self.assertIn('已记录 2 次价格调整', waiting['summary'])

        self.make_history([('95.00', '70.00')])
        public = build_item_price_history_maintenance(self.item, self.seller)
        self.assertEqual(public['state'], 'public')
        self.assertEqual(public['tone'], 'positive')
        self.assertEqual(public['title'], '正常展示')
        self.assertIn('最近 3 次价格调整', public['summary'])

    def test_maintenance_reports_broken_window(self):
        self.make_history([
            ('100.00', '120.00'), ('95.00', '80.00'), ('80.00', '70.00'),
        ])

        result = build_item_price_history_maintenance(self.item, self.seller)

        self.assertEqual(result['state'], 'broken')
        self.assertEqual(result['tone'], 'warning')
        self.assertEqual(result['title'], '轨迹断链')
        self.assertIn('买家侧暂不展示', result['summary'])

    def test_maintenance_reports_current_price_mismatch(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        self.item.price = Decimal('75.00')
        self.item.save(update_fields=['price', 'updated_at'])

        result = build_item_price_history_maintenance(self.item, self.seller)

        self.assertEqual(result['state'], 'current_mismatch')
        self.assertEqual(result['title'], '价格不一致')
        self.assertIn('¥70.00', result['summary'])
        self.assertIn('¥75.00', result['summary'])

    def test_maintenance_checks_only_latest_six_changes(self):
        changes = self.make_history([
            ('100.00', '110.00'),
            ('110.00', '120.00'),
            ('120.00', '130.00'),
            ('130.00', '140.00'),
            ('140.00', '150.00'),
            ('150.00', '160.00'),
            ('160.00', '170.00'),
        ])
        ItemPriceChange.objects.filter(pk=changes[0].pk).update(
            new_price=Decimal('90.00')
        )

        result = build_item_price_history_maintenance(self.item, self.seller)

        self.assertEqual(result['state'], 'public')
        self.assertEqual(result['change_count'], 7)
        self.assertIn('最近 6 次价格调整（共 7 次）', result['summary'])

    def test_maintenance_builder_is_read_only_and_does_not_expose_operator(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        connection.queries_log.clear()

        with self.assertNumQueries(2):
            result = build_item_price_history_maintenance(self.item, self.seller)

        statements = [query['sql'].lstrip().upper() for query in connection.queries]
        self.assertEqual(len(statements), 2)
        self.assertTrue(all(statement.startswith('SELECT') for statement in statements))
        self.assertNotIn(self.operator.username, str(result))
        self.assertNotIn('changed_by', str(result))

    def test_item_detail_renders_maintenance_for_seller_only(self):
        self.make_history([
            ('100.00', '120.00'), ('120.00', '95.00'), ('95.00', '70.00'),
        ])
        url = reverse('item_detail', args=[self.item.pk])

        self.client.force_login(self.seller)
        seller_response = self.client.get(url)
        self.assertContains(seller_response, '正常展示')
        self.assertContains(seller_response, '买家侧正在展示最近 3 次价格调整')
        self.assertNotContains(seller_response, self.operator.username)

        self.client.force_login(self.buyer)
        buyer_response = self.client.get(url)
        self.assertNotContains(buyer_response, '价格轨迹维护提示')
        self.assertNotContains(buyer_response, '买家侧正在展示最近 3 次价格调整')

        self.client.logout()
        anonymous_response = self.client.get(url)
        self.assertNotContains(anonymous_response, '价格轨迹维护提示')
