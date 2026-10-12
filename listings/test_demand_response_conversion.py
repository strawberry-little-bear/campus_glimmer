# -*- coding: utf-8 -*-
"""为「确认了求购响应之后，交易真的发生了吗」钉住口径。

这条链路走到 `accept` 就断成了两半：`DemandPost.status` 当场变成 `fulfilled`，
而 `Order` 那边什么都没发生。这个模块把它们读出来，只报告不改写。这些测试要
钉住的是：

1. 一笔订单算不算「这条响应带来的」，只认商品相同 + 买家是响应人 + 下单不早于确认。
2. 分母是已确认的响应数，排除 `withdrawn`，不是求购数也不是响应总数。
3. 成交看 `completed` / `returned`，`cancelled` 单独一类。
4. 从确认到下单的间隔按 `Order.created_at` 减响应的 `updated_at` 算，分档按小时起步。
5. 没有订单的那些，卡在哪三类，顺序即优先级。
6. 响应说明只报「写了没写」和长度，一个字符都不读。
7. 不按用户聚合、不排名。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .demand_response_conversion import (
    CONVERSION_LATENCY_BUCKETS, MIN_MEASURED_RESPONSES, STUCK_LIST_LIMIT,
    build_demand_response_conversion,
)
from .models import (
    CampusLocation, Category, DemandPost, DemandResponse, Item, Order,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class DemandResponseConversionTests(TestCase):
    def setUp(self):
        self.requester = make_user('conversion-requester')
        self.seller = make_user('conversion-seller')
        self.category = Category.objects.create(name='转化测试分类')
        self.location = CampusLocation.objects.create(name='转化测试地点')
        self._seq = 0

    def make_demand(self, *, status='active', created_days_ago=10):
        self._seq += 1
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'转化测试求购 {self._seq}',
            description='用于求购响应转化统计', category=self.category,
            location=self.location, min_price='10.00', max_price='100.00',
            status=status,
        )
        demand.created_at = timezone.now() - timedelta(days=created_days_ago)
        demand.save(update_fields=['created_at'])
        return demand

    def make_item(self, *, status='available', trade_mode='sale', seller=None):
        self._seq += 1
        return Item.objects.create(
            title=f'转化测试商品 {self._seq}', description='用于求购响应转化统计',
            price='50.00', category=self.category, condition='良好',
            location=self.location, seller=seller or self.seller,
            trade_mode=trade_mode, status=status,
        )

    def make_response(self, demand, item, *, responder=None, status='accepted',
                      confirmed_days_ago=5, message=''):
        """建一条响应。`updated_at` 是 auto_now，建完再回填成确认时刻。"""
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=responder or item.seller,
            message=message, match_score=90, status='pending',
        )
        response.status = status
        response.save(update_fields=['status'])
        # `updated_at` 是 auto_now，save() 会把它改回墙上时间，只能走 .update() 绕开。
        # 而它正是确认时刻——accept 分支 save(update_fields=[..., 'updated_at']) 带的
        # auto_now，所以回填之后它就是这次确认发生的时刻。
        DemandResponse.objects.filter(pk=response.pk).update(
            updated_at=timezone.now() - timedelta(days=confirmed_days_ago),
        )
        response.refresh_from_db()
        return response

    def make_order(self, item, buyer, *, status='completed', delay_hours=2):
        """按响应确认之后 `delay_hours` 小时建一笔订单。"""
        order = Order.objects.create(
            item=item, buyer=buyer, seller=item.seller,
            meeting_location=self.location, agreed_price='50.00', status=status,
        )
        # `created_at` 是 auto_now_add，建行时拿到的是真实墙上时间，回填成
        # 「五天前 + delay_hours」——测试再按确认时刻逐例改写。
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(days=5, hours=-delay_hours),
        )
        order.refresh_from_db()
        return order

    # --- 转化判定 -------------------------------------------------------

    def test_confirmed_response_with_a_later_order_is_converted(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder)
        order.created_at = response.updated_at + timedelta(hours=3)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 1)
        self.assertEqual(report['conversion']['converted_count'], 1)
        self.assertEqual(report['conversion']['conversion_rate'], 100.0)

    def test_order_created_before_the_confirmation_is_not_a_conversion(self):
        # 下单早于确认，说明这笔订单和这次确认没有关系，是买家自己摸过来的。
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder)
        order.created_at = response.updated_at - timedelta(hours=1)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 0)
        self.assertEqual(report['blocked']['total'], 1)

    def test_order_by_someone_else_is_not_a_conversion(self):
        # 商品相同但买家不是响应人：卖家卖给了别人，不是这条响应走完了那段路。
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        stranger = make_user('conversion-stranger')
        self.make_order(item, stranger)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 0)

    def test_order_for_a_different_item_is_not_a_conversion(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        other_item = self.make_item()
        self.make_order(other_item, response.responder)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 0)

    def test_withdrawn_responses_are_not_in_the_denominator(self):
        # 撤回是响应人自己撤的，它没有进入过发布者的决策，不是转化也不是流失。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item, status='withdrawn')

        report = build_demand_response_conversion(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['conversion']['total'], 0)

    def test_pending_responses_are_not_in_the_denominator(self):
        # 还没走到发布者决策那一步的，谈不上转化。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item, status='pending')

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 0)

    def test_demand_count_is_deduplicated(self):
        # 同一条求购只会确认一条，两个数正常情况下相等；不相等就是并发确认。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 1)
        self.assertEqual(report['conversion']['demand_count'], 1)

    # --- 成交拆分 -------------------------------------------------------

    def test_returned_orders_count_as_dealt(self):
        # 借用订单走到 returned 是一次完整的闭环，和 completed 等价。
        demand = self.make_demand()
        item = self.make_item(trade_mode='borrow')
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder, status='returned')
        order.created_at = response.updated_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['dealt']['dealt_count'], 1)
        self.assertEqual(report['dealt']['dealt_rate'], 100.0)
        self.assertEqual(report['dealt']['cancelled_count'], 0)

    def test_cancelled_orders_are_their_own_bucket(self):
        # 取消是一次真的失败，而且失败在确认之后，比「没有订单」更值得被看见。
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder, status='cancelled')
        order.created_at = response.updated_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 1)
        self.assertEqual(report['dealt']['dealt_count'], 0)
        self.assertEqual(report['dealt']['cancelled_count'], 1)
        self.assertEqual(report['dealt']['still_open_count'], 0)

    def test_still_open_orders_are_counted_separately(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder, status='pending')
        order.created_at = response.updated_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['dealt']['still_open_count'], 1)
        self.assertEqual(report['dealt']['cancelled_count'], 0)

    # --- 时延 ----------------------------------------------------------

    def test_latency_is_measured_from_confirmation_to_order(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder)
        order.created_at = response.updated_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['latency']['total'], 1)
        self.assertEqual(report['latency']['median_label'], '2 小时')
        self.assertEqual(report['latency']['same_day_count'], 1)

    def test_latency_buckets_start_at_hours_not_days(self):
        # 确认当天就下单和隔天下单是两件不同的事，按天起步会把「当天」撑得很大。
        self.assertEqual(CONVERSION_LATENCY_BUCKETS[0][0], 3600)
        self.assertEqual(CONVERSION_LATENCY_BUCKETS[1][0], 24 * 3600)
        self.assertEqual(CONVERSION_LATENCY_BUCKETS[2][0], 7 * 86400)
        self.assertEqual(CONVERSION_LATENCY_BUCKETS[-1][0], None)

    def test_responses_without_an_order_have_no_latency(self):
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['latency']['total'], 0)
        self.assertEqual(report['latency']['median_label'], '—')

    def test_latency_bucket_rows_cover_every_span(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder)
        order.created_at = response.updated_at + timedelta(days=30)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        buckets = report['latency']['bucket_rows']
        self.assertEqual(len(buckets), len(CONVERSION_LATENCY_BUCKETS))
        self.assertEqual(sum(row['count'] for row in buckets), 1)
        self.assertEqual(buckets[-1]['count'], 1)

    # --- 卡点 ----------------------------------------------------------

    def test_item_gone_is_the_first_blocked_reason(self):
        # 商品已经不可交易，卖家侧有动作，而且动作可查。这是最干净的一类。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)
        item.status = 'sold'
        item.save(update_fields=['status'])

        report = build_demand_response_conversion(days=30)
        blocked = report['blocked']
        self.assertEqual(blocked['total'], 1)
        self.assertEqual(blocked['item_gone_count'], 1)
        self.assertEqual(blocked['still_walking_count'], 0)
        self.assertEqual(
            [row for row in blocked['rows'] if row['key'] == 'item_gone'][0]['count'], 1,
        )

    def test_closed_demand_is_the_second_blocked_reason(self):
        # 商品还在、求购已经不重要了。这一档只报规模，不给方向。
        demand = self.make_demand(status='closed')
        item = self.make_item()
        self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        blocked = report['blocked']
        self.assertEqual(blocked['total'], 1)
        self.assertEqual(blocked['item_gone_count'], 0)
        self.assertEqual(blocked['still_walking_count'], 0)
        self.assertEqual(
            [row for row in blocked['rows'] if row['key'] == 'demand_closed'][0]['count'], 1,
        )

    def test_fulfilled_demand_is_not_a_blocked_reason(self):
        # `fulfilled` 是 accept 当场写的，人人如此，说明不了问题。
        demand = self.make_demand(status='fulfilled')
        item = self.make_item()
        self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['blocked']['still_walking_count'], 1)

    def test_everything_still_up_is_still_walking(self):
        # 商品在、求购在、两边都没动，那次确认就这样悬着。这是真正要运营看一眼的。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        blocked = report['blocked']
        self.assertEqual(blocked['total'], 1)
        self.assertEqual(blocked['still_walking_count'], 1)
        self.assertEqual(blocked['still_walking_rate'], 100.0)

    def test_blocked_rows_are_listed_without_any_username(self):
        # 清单里出现的是商品标题和求购标题，不是「谁答应了没做到」。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        rows = report['stuck_rows']['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].response.item.title, item.title)
        self.assertEqual(rows[0].response.demand.title, demand.title)
        # 用户名一次都不出现：清单里出现的字段只有商品、求购、响应和订单。
        listed = [
            rows[0].response.item.title, rows[0].response.demand.title,
            rows[0].response.responder.username,
        ]
        self.assertIn(item.title, listed)
        self.assertIn(demand.title, listed)
        # 响应人确实在数据里（这条响应是他提的），但卡点清单本身不带用户名之外的人。
        self.assertNotIn(self.requester.username, listed)

    def test_stuck_list_is_capped(self):
        demand = self.make_demand()
        for index in range(STUCK_LIST_LIMIT + 3):
            item = self.make_item()
            self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        stuck = report['stuck_rows']
        self.assertEqual(len(stuck['rows']), STUCK_LIST_LIMIT)
        self.assertEqual(stuck['row_total'], STUCK_LIST_LIMIT + 3)
        self.assertEqual(stuck['still_walking_row_total'], STUCK_LIST_LIMIT + 3)

    # --- 交易方式 ------------------------------------------------------

    def test_conversion_is_split_by_trade_mode(self):
        # 出售要看钱，免费和借用都不涉及付款，走完那段路的动力完全不同。
        for trade_mode in ('sale', 'free', 'borrow'):
            demand = self.make_demand()
            item = self.make_item(trade_mode=trade_mode)
            response = self.make_response(demand, item)
            order = self.make_order(item, response.responder)
            order.created_at = response.updated_at + timedelta(hours=1)
            order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        rows = {row['key']: row for row in report['trade_modes']['rows']}
        self.assertEqual(rows['sale']['conversion_rate'], 100.0)
        self.assertEqual(rows['free']['conversion_rate'], 100.0)
        self.assertEqual(rows['borrow']['conversion_rate'], 100.0)
        self.assertEqual(
            sum(row['total'] for row in report['trade_modes']['rows']), 3,
        )

    # --- 响应说明 ------------------------------------------------------

    def test_message_length_is_reported_without_reading_content(self):
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item, message='短')
        other = self.make_demand()
        other_item = self.make_item()
        self.make_response(
            other, other_item,
            message='我这台还有发票和原装盒子，可以当场验机，价格可以再商量。',
        )
        blank = self.make_demand()
        blank_item = self.make_item()
        self.make_response(blank, blank_item, message='   ')

        report = build_demand_response_conversion(days=30)
        message = report['message']
        self.assertEqual(message['total'], 3)
        self.assertEqual(message['filled_count'], 2)
        # 空白字符串不算「写了」，所以第三条不计入 filled，也不进任何一档。
        self.assertEqual(message['total'] - message['filled_count'], 1)
        self.assertGreater(message['average_length'], 0)
        self.assertEqual(sum(row['count'] for row in message['length_rows']), 2)

    def test_message_presence_is_only_a_comparison_not_a_verdict(self):
        # 写了说明和没写说明的转化差只做对照，两组都太小的时候不给方向。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item, message='有发票')
        other = self.make_demand()
        other_item = self.make_item()
        self.make_response(other, other_item)

        report = build_demand_response_conversion(days=30)
        message = report['message']
        self.assertEqual(message['with_message_count'], 1)
        self.assertEqual(message['without_message_count'], 1)
        self.assertFalse(message['is_measurable'])
        self.assertFalse(message['gap_is_meaningful'])

    # --- 窗口与空数据 ---------------------------------------------------

    def test_window_cuts_on_the_confirmation_moment(self):
        # 窗口前提交、窗口内才被确认的响应，它的转化发生在窗口内，不能整条丢掉。
        demand = self.make_demand(created_days_ago=40)
        item = self.make_item()
        self.make_response(demand, item, confirmed_days_ago=2)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 1)

    def test_confirmations_outside_the_window_are_excluded(self):
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item, confirmed_days_ago=45)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 0)
        self.assertFalse(report['has_data'])

    def test_empty_period_reports_no_data(self):
        report = build_demand_response_conversion(days=30)
        self.assertFalse(report['has_data'])
        self.assertIn('当前周期没有确认过的求购响应', report['summary'])
        self.assertEqual(report['conversion']['total'], 0)
        self.assertEqual(report['conversion']['conversion_rate'], 0)
        self.assertEqual(report['blocked']['total'], 0)
        self.assertEqual(report['latency']['total'], 0)

    def test_small_sample_is_flagged(self):
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)

        report = build_demand_response_conversion(days=30)
        self.assertTrue(report['conversion']['is_small_sample'])
        self.assertEqual(
            report['conversion']['min_measured_responses'], MIN_MEASURED_RESPONSES,
        )
        self.assertIn(f'不足 {MIN_MEASURED_RESPONSES} 条', report['summary'])

    def test_invalid_days_falls_back_to_thirty(self):
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)
        report = build_demand_response_conversion(days=14)
        self.assertEqual(report['period_days'], 30)
        self.assertEqual(report['conversion']['total'], 1)

    def test_summary_mentions_conversion_and_latency(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.responder)
        order.created_at = response.updated_at + timedelta(hours=4)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertIn('真的变成了订单', report['summary'])
        self.assertIn('确认到下单', report['summary'])
