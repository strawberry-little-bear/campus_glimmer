# -*- coding: utf-8 -*-
"""为「确认了求购响应之后，交易真的发生了吗」钉住口径。

`accept` 写入 `accepted_at` 并把求购改成 `fulfilled`；发布者之后从响应入口创建
`Order` 或 `GiftApplication` 时，显式保留响应来源。这个模块把两段事实读出来，只
报告不改写。这些测试要钉住的是：

1. 一笔订单算不算「这条响应带来的」，新记录只认 `demand_response_id`；没有来源的
   迁移前旧订单才按商品相同、买家是求购发布者、下单不早于确认做兼容推断。
2. 分母是已确认的响应数，排除 `withdrawn`，不是求购数也不是响应总数。
3. 成交看 `completed` / `returned`，`cancelled` 单独一类。
4. 从确认到下单的间隔按 `Order.created_at` 减显式 `accepted_at` 算，分档按小时起步。
5. 没有订单的那些，先看领取申请走到了哪一步，再看商品、求购和尚未发起交易。
6. 领取申请只算阶段，不算订单转化；显式来源优先于历史兼容推断。
7. 响应说明只报「写了没写」和长度，一个字符都不读。
8. 不按用户聚合、不排名。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .demand_response_conversion import (
    CONVERSION_LATENCY_BUCKETS, MIN_MEASURED_RESPONSES, STUCK_LIST_LIMIT,
    build_demand_response_conversion, build_reopen_funnel_alerts,
)
from .models import (
    CampusLocation, Category, DeliveryConfirmation, DemandPost,
    DemandReopenEvent, DemandResponse, GiftApplication, Item,
    MeetingAppointment, Order,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class DemandReopenTrendAlertTests(SimpleTestCase):
    """Pin the two trend decisions without depending on database timing."""

    @staticmethod
    def period(event_count, reconfirmed_count, order_count):
        return {
            'event_count': event_count,
            'reconfirmed_count': reconfirmed_count,
            'order_count': order_count,
            'reconfirmation_rate_value': (
                reconfirmed_count / event_count * 100 if event_count else 0
            ),
            'order_conversion_rate_value': (
                order_count / reconfirmed_count * 100
                if reconfirmed_count else 0
            ),
        }

    def build(self, prior, previous, current):
        return build_reopen_funnel_alerts(current, previous, prior)

    def test_each_rate_has_its_own_twelve_sample_floor(self):
        # 再次确认率的分母是事件，订单转化率的分母是再次确认。后一项不足 12
        # 时，即使两次下降都超过 8 个百分点，也只能出现前一项告警。
        alerts = self.build(
            self.period(11, 11, 11),
            self.period(12, 10, 8),
            self.period(12, 9, 6),
        )
        self.assertEqual(alerts, [])

        alerts = self.build(
            self.period(12, 11, 8),
            self.period(12, 10, 7),
            self.period(12, 9, 6),
        )
        self.assertEqual(
            [alert['key'] for alert in alerts],
            ['reopen_reconfirmation_decline'],
        )

    def test_one_decline_or_a_current_recovery_does_not_alert(self):
        only_one_decline = self.build(
            self.period(20, 18, 16),
            self.period(20, 18, 16),
            self.period(20, 16, 14),
        )
        recovered = self.build(
            self.period(20, 18, 16),
            self.period(20, 16, 14),
            self.period(20, 17, 15),
        )

        self.assertEqual(only_one_decline, [])
        self.assertEqual(recovered, [])

    def test_exact_eight_point_drops_alert_but_seven_point_drops_do_not(self):
        exact = self.build(
            self.period(25, 20, 16),
            self.period(25, 18, 14),
            self.period(25, 16, 12),
        )
        near = self.build(
            self.period(13, 7, 7),
            self.period(13, 6, 6),
            self.period(13, 5, 5),
        )

        self.assertEqual(
            [alert['key'] for alert in exact],
            ['reopen_reconfirmation_decline'],
        )
        self.assertEqual(near, [])

    def test_both_rates_can_alert_with_their_own_exact_evidence(self):
        alerts = self.build(
            self.period(20, 18, 16),
            self.period(30, 24, 18),
            self.period(40, 28, 18),
        )

        self.assertEqual(
            [alert['key'] for alert in alerts],
            [
                'reopen_reconfirmation_decline',
                'reopen_order_conversion_decline',
            ],
        )
        self.assertTrue(all(
            alert['action_url_name'] == 'operations_dashboard'
            and alert['action_anchor'] == 'ops-reopen-funnel'
            and alert['severity'] == 'warning'
            for alert in alerts
        ))
        self.assertIn('20 → 30 → 40 次', alerts[0]['message'])
        self.assertIn('90.0%、80.0%、70.0%', alerts[0]['message'])
        self.assertEqual(alerts[0]['metric'], '70.0%')
        self.assertEqual(alerts[0]['metric_label'], '当前再次确认率')
        self.assertEqual(alerts[1]['metric_label'], '当前订单转化率')
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
                      confirmed_days_ago=5, accepted_at=None, message=''):
        """建一条响应，并显式回填确认时刻。"""
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=responder or item.seller,
            message=message, match_score=90, status='pending',
        )
        response.status = status
        response.save(update_fields=['status'])
        confirmed_at = accepted_at or (
            timezone.now() - timedelta(days=confirmed_days_ago)
        )
        DemandResponse.objects.filter(pk=response.pk).update(
            updated_at=confirmed_at,
            accepted_at=confirmed_at if status in {'accepted', 'superseded'} else None,
        )
        response.refresh_from_db()
        return response

    def make_order(self, item, buyer, *, status='completed', delay_hours=2,
                   demand_response=None):
        """按响应确认之后 `delay_hours` 小时建一笔订单。"""
        order = Order.objects.create(
            item=item, buyer=buyer, seller=item.seller,
            demand_response=demand_response,
            meeting_location=self.location, agreed_price='50.00', status=status,
        )
        # `created_at` 是 auto_now_add，建行时拿到的是真实墙上时间，回填成
        # 「五天前 + delay_hours」——测试再按确认时刻逐例改写。
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(days=5, hours=-delay_hours),
        )
        order.refresh_from_db()
        return order

    def make_appointment(
        self, order, *, status='confirmed', ended=True,
        buyer_arrived=False, seller_arrived=False,
    ):
        now = timezone.now()
        if ended:
            start_at = now - timedelta(hours=2)
            end_at = now - timedelta(hours=1)
        else:
            start_at = now + timedelta(hours=1)
            end_at = now + timedelta(hours=2)
        return MeetingAppointment.objects.create(
            order=order,
            proposed_by=self.requester,
            location=self.location,
            start_at=start_at,
            end_at=end_at,
            status=status,
            buyer_arrived_at=(now - timedelta(hours=2, minutes=5)
                              if buyer_arrived else None),
            seller_arrived_at=(now - timedelta(hours=2, minutes=3)
                               if seller_arrived else None),
        )

    def make_delivery_confirmation(
        self, order, *, buyer=False, seller=False,
    ):
        now = timezone.now()
        return DeliveryConfirmation.objects.create(
            order=order,
            buyer_confirmed_at=now if buyer else None,
            seller_confirmed_at=now if seller else None,
        )

    def make_gift_application(
        self, response, *, status='pending', demand_response='source',
        hours_after_confirmation=1,
    ):
        """建一份免费领取申请，并显式回填申请时刻。"""
        application = GiftApplication.objects.create(
            item=response.item,
            applicant=response.demand.requester,
            meeting_location=self.location,
            applicant_note='转化阶段测试',
            status=status,
            demand_response=response if demand_response == 'source' else demand_response,
        )
        GiftApplication.objects.filter(pk=application.pk).update(
            created_at=response.accepted_at + timedelta(
                hours=hours_after_confirmation,
            ),
            decided_at=(
                response.accepted_at + timedelta(hours=hours_after_confirmation)
                if status in {'selected', 'rejected', 'withdrawn'} else None
            ),
        )
        application.refresh_from_db()
        return application

    def make_reopen_event(
        self, demand, response, *, reopened_at=None, count=1, source='runtime',
    ):
        return DemandReopenEvent.objects.create(
            demand=demand,
            response=response,
            previous_item=response.item,
            previous_item_title=response.item.title,
            previous_trade_mode=response.item.trade_mode,
            previous_accepted_at=response.accepted_at,
            reopened_at=reopened_at or timezone.now(),
            reopen_count_after=count,
            source=source,
        )

    # --- 转化判定 -------------------------------------------------------

    def test_confirmed_response_with_a_later_order_is_converted(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.demand.requester)
        order.created_at = response.accepted_at + timedelta(hours=3)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 1)
        self.assertEqual(report['conversion']['converted_count'], 1)
        self.assertEqual(report['conversion']['conversion_rate'], 100.0)

    def test_superseded_confirmation_stays_in_the_historical_denominator(self):
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item, status='superseded')

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 1)
        self.assertEqual(report['conversion']['converted_count'], 0)

    def test_order_created_before_the_confirmation_is_not_a_conversion(self):
        # 下单早于确认，说明这笔订单和这次确认没有关系，是买家自己摸过来的。
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.demand.requester)
        order.created_at = response.accepted_at - timedelta(hours=1)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 0)
        self.assertEqual(report['blocked']['total'], 1)

    def test_order_by_someone_else_is_not_a_conversion(self):
        # 商品相同但买家不是求购发布者：卖家卖给了别人，不是这条响应走完了那段路。
        demand = self.make_demand()
        item = self.make_item()
        self.make_response(demand, item)
        stranger = make_user('conversion-stranger')
        self.make_order(item, stranger)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 0)

    def test_order_by_item_seller_is_not_a_conversion(self):
        # 响应人是商品卖家，不是买家。卖家给自己响应过的商品下单不能算转化。
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        self.make_order(item, response.responder)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 0)

    def test_explicit_response_link_counts_without_guessing(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        self.make_order(
            item, self.requester, demand_response=response,
        )

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 1)

    def test_explicit_link_disambiguates_same_item_and_buyer(self):
        # 同一件商品可以响应两条不同求购，且两条都由同一发布者确认。显式来源只把订单
        # 归给真正发起交易的那一条，不能让两条响应同时凭空变成成交。
        first_demand = self.make_demand()
        second_demand = self.make_demand()
        item = self.make_item()
        first_response = self.make_response(first_demand, item)
        self.make_response(second_demand, item)
        self.make_order(
            item, self.requester, demand_response=first_response,
        )

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['total'], 2)
        self.assertEqual(report['conversion']['converted_count'], 1)

    def test_order_for_a_different_item_is_not_a_conversion(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        other_item = self.make_item()
        self.make_order(other_item, response.demand.requester)

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
        order = self.make_order(item, response.demand.requester, status='returned')
        order.created_at = response.accepted_at + timedelta(hours=2)
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
        order = self.make_order(item, response.demand.requester, status='cancelled')
        order.created_at = response.accepted_at + timedelta(hours=2)
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
        order = self.make_order(item, response.demand.requester, status='pending')
        order.created_at = response.accepted_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['dealt']['still_open_count'], 1)
        self.assertEqual(report['dealt']['cancelled_count'], 0)

    # --- 时延 ----------------------------------------------------------

    def test_latency_is_measured_from_confirmation_to_order(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(item, response.demand.requester)
        order.created_at = response.accepted_at + timedelta(hours=2)
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
        order = self.make_order(item, response.demand.requester)
        order.created_at = response.accepted_at + timedelta(days=30)
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
            status = 'accepted' if index == 0 else 'superseded'
            self.make_response(demand, item, status=status)

        report = build_demand_response_conversion(days=30)
        stuck = report['stuck_rows']
        self.assertEqual(len(stuck['rows']), STUCK_LIST_LIMIT)
        self.assertEqual(stuck['row_total'], STUCK_LIST_LIMIT + 3)
        self.assertEqual(stuck['still_walking_row_total'], STUCK_LIST_LIMIT + 3)

    # --- 免费领取阶段 --------------------------------------------------

    def test_pending_gift_application_is_not_still_walking(self):
        # 已经提交领取申请、正在等卖家选择，是用户走过的下一步，不能再报成「两边都没动」。
        demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        response = self.make_response(demand, item)
        self.make_gift_application(response, status='pending')

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 0)
        self.assertEqual(report['gift_applications']['total'], 1)
        self.assertEqual(report['gift_applications']['pending_count'], 1)
        self.assertEqual(report['gift_applications']['awaiting_start_count'], 0)
        self.assertEqual(report['blocked']['still_walking_count'], 0)
        row = [row for row in report['blocked']['rows'] if row['key'] == 'gift_pending'][0]
        self.assertEqual(row['count'], 1)
        self.assertIn('等待卖家选择', report['summary'])

    def test_each_unselected_gift_outcome_keeps_its_own_stage(self):
        for status in ('rejected', 'withdrawn'):
            demand = self.make_demand()
            item = self.make_item(trade_mode='free')
            response = self.make_response(demand, item)
            self.make_gift_application(response, status=status)

        report = build_demand_response_conversion(days=30)
        stages = report['gift_applications']
        self.assertEqual(stages['total'], 2)
        self.assertEqual(stages['rejected_count'], 1)
        self.assertEqual(stages['withdrawn_count'], 1)
        self.assertEqual(stages['pending_count'], 0)
        self.assertEqual(report['blocked']['still_walking_count'], 0)

    def test_selected_gift_without_order_is_reported_as_a_missing_link(self):
        demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        response = self.make_response(demand, item)
        self.make_gift_application(response, status='selected')

        report = build_demand_response_conversion(days=30)
        self.assertEqual(
            report['gift_applications']['selected_without_order_count'], 1,
        )
        self.assertEqual(report['blocked']['still_walking_count'], 0)
        row = [
            row for row in report['blocked']['rows']
            if row['key'] == 'gift_selected_without_order'
        ][0]
        self.assertEqual(row['count'], 1)

    def test_explicit_gift_source_disambiguates_same_item_and_requester(self):
        first_demand = self.make_demand()
        second_demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        first_response = self.make_response(
            first_demand, item, confirmed_days_ago=6,
        )
        second_response = self.make_response(
            second_demand, item, confirmed_days_ago=5,
        )
        self.make_gift_application(first_response, status='pending')

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['gift_applications']['total'], 1)
        self.assertEqual(report['gift_applications']['pending_count'], 1)
        self.assertEqual(report['gift_applications']['awaiting_start_count'], 1)
        stages_by_response = {
            row.response.pk: row.blocked_reason
            for row in report['stuck_rows']['rows']
        }
        self.assertEqual(stages_by_response[first_response.pk], 'gift_pending')
        self.assertEqual(stages_by_response[second_response.pk], 'still_walking')

    def test_legacy_gift_application_uses_compatible_inference(self):
        demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        response = self.make_response(demand, item)
        self.make_gift_application(response, demand_response=None, status='pending')

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['gift_applications']['pending_count'], 1)
        self.assertEqual(report['blocked']['still_walking_count'], 0)

    def test_legacy_gift_application_before_confirmation_is_not_attributed(self):
        demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        response = self.make_response(demand, item)
        self.make_gift_application(
            response, demand_response=None, status='pending',
            hours_after_confirmation=-1,
        )

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['gift_applications']['total'], 0)
        self.assertEqual(report['blocked']['still_walking_count'], 1)

    def test_legacy_gift_application_goes_to_nearest_confirmation_only(self):
        first_demand = self.make_demand()
        second_demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        first_response = self.make_response(
            first_demand, item, confirmed_days_ago=6,
        )
        second_response = self.make_response(
            second_demand, item, confirmed_days_ago=5,
        )
        self.make_gift_application(
            second_response, demand_response=None, status='pending',
            hours_after_confirmation=1,
        )

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['gift_applications']['total'], 1)
        stages_by_response = {
            row.response.pk: row.blocked_reason
            for row in report['stuck_rows']['rows']
        }
        self.assertEqual(stages_by_response[first_response.pk], 'still_walking')
        self.assertEqual(stages_by_response[second_response.pk], 'gift_pending')

    def test_order_takes_priority_over_a_pending_gift_application(self):
        # 领取申请存在不等于订单转化；真正接出订单时，成交口径仍优先看订单。
        demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        response = self.make_response(demand, item)
        self.make_gift_application(response, status='pending')
        order = self.make_order(
            item, self.requester, demand_response=response,
        )
        order.created_at = response.accepted_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertEqual(report['conversion']['converted_count'], 1)
        self.assertEqual(report['blocked']['total'], 0)
        self.assertEqual(report['gift_applications']['total'], 0)

    def test_order_and_gift_stage_counts_conserve_the_denominator(self):
        pending_demand = self.make_demand()
        pending_item = self.make_item(trade_mode='free')
        pending_response = self.make_response(pending_demand, pending_item)
        self.make_gift_application(pending_response)

        order_demand = self.make_demand()
        order_item = self.make_item()
        order_response = self.make_response(order_demand, order_item)
        order = self.make_order(
            order_item, self.requester, demand_response=order_response,
        )
        order.created_at = order_response.accepted_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        self.make_demand()
        waiting_item = self.make_item()
        waiting_demand = self.make_demand()
        self.make_response(waiting_demand, waiting_item)

        report = build_demand_response_conversion(days=30)
        self.assertEqual(
            report['conversion']['converted_count'] + report['blocked']['total'],
            report['conversion']['total'],
        )
        self.assertEqual(
            report['gift_applications']['total']
            + report['gift_applications']['awaiting_start_count']
            + report['blocked']['item_gone_count']
            + report['blocked']['demand_closed_count'],
            report['blocked']['total'],
        )

    def test_delivery_funnel_deduplicates_legacy_orders_and_excludes_no_order_rows(self):
        demand = self.make_demand()
        item = self.make_item()
        first = self.make_response(demand, item, accepted_at=timezone.now() - timedelta(days=5))
        second = self.make_response(
            self.make_demand(), item, accepted_at=first.accepted_at,
        )
        # 迁移前订单没有显式来源。两条同商品、同发布者、确认后下单的响应都能兼容看见
        # 它；交付阶段必须只把这个真实订单数一次。
        self.make_order(item, self.requester, status='confirmed')
        no_order = self.make_response(
            self.make_demand(), self.make_item(), accepted_at=timezone.now() - timedelta(days=4),
        )

        report = build_demand_response_conversion(days=30)
        delivery = report['delivery_funnel']

        self.assertEqual(no_order.order if hasattr(no_order, 'order') else None, None)
        self.assertEqual(report['conversion']['converted_count'], 2)
        self.assertEqual(delivery['converted_response_count'], 2)
        self.assertEqual(delivery['order_count'], 1)
        self.assertEqual(delivery['appointment_count'], 0)
        stages = {row['key']: row['count'] for row in delivery['appointment_stage_rows']}
        self.assertEqual(stages['active_without_appointment'], 1)

    def test_orders_without_appointments_split_pending_active_and_ended_states(self):
        cases = (
            ('pending', 'pending_without_appointment'),
            ('confirmed', 'active_without_appointment'),
            ('cancelled', 'ended_without_appointment'),
            ('completed', 'ended_without_appointment'),
        )
        for status, _key in cases:
            demand = self.make_demand()
            item = self.make_item()
            response = self.make_response(demand, item)
            self.make_order(
                item, self.requester, status=status, demand_response=response,
            )

        delivery = build_demand_response_conversion(days=30)['delivery_funnel']
        stages = {row['key']: row['count'] for row in delivery['appointment_stage_rows']}
        self.assertEqual(stages['pending_without_appointment'], 1)
        self.assertEqual(stages['active_without_appointment'], 1)
        self.assertEqual(stages['ended_without_appointment'], 2)
        self.assertEqual(delivery['order_count'], 4)
        self.assertEqual(delivery['appointment_count'], 0)
        self.assertEqual(delivery['appointment_rate'], 0.0)

    def test_every_current_appointment_state_has_its_own_stage(self):
        cases = (
            ('pending', True, 'appointment_pending'),
            ('declined', True, 'appointment_declined'),
            ('cancelled', True, 'appointment_cancelled'),
            ('confirmed', False, 'appointment_confirmed_open'),
            ('confirmed', True, 'appointment_confirmed_ended'),
        )
        for status, ended, _key in cases:
            demand = self.make_demand()
            item = self.make_item()
            response = self.make_response(demand, item)
            order = self.make_order(
                item, self.requester, status='meeting', demand_response=response,
            )
            self.make_appointment(order, status=status, ended=ended)

        delivery = build_demand_response_conversion(days=30)['delivery_funnel']
        stages = {row['key']: row['count'] for row in delivery['appointment_stage_rows']}
        for _status, _ended, key in cases:
            self.assertEqual(stages[key], 1)
        self.assertEqual(delivery['appointment_count'], 5)
        self.assertEqual(delivery['appointment_rate'], 100.0)
        self.assertEqual(delivery['confirmed_open_count'], 1)
        self.assertEqual(delivery['confirmed_ended_count'], 1)
        self.assertEqual(delivery['both_arrived_count'], 0)

    def test_attendance_and_delivery_confirmation_split_do_not_guess_abandonment(self):
        arrival_cases = (
            (True, True),
            (True, False),
            (False, True),
            (False, False),
        )
        for index, (buyer, seller) in enumerate(arrival_cases):
            demand = self.make_demand()
            item = self.make_item()
            response = self.make_response(demand, item)
            order = self.make_order(
                item, self.requester, status='completed',
                demand_response=response,
            )
            self.make_appointment(
                order, status='confirmed', ended=True,
                buyer_arrived=buyer, seller_arrived=seller,
            )
            if index == 0:
                self.make_delivery_confirmation(order, buyer=True, seller=True)
            elif index == 1:
                self.make_delivery_confirmation(order, buyer=True)
            elif index == 2:
                self.make_delivery_confirmation(order, seller=True)

        # 尚未结束的确认预约是右删失样本，不能提前算成双方都没有到场。
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        open_order = self.make_order(
            item, self.requester, status='meeting', demand_response=response,
        )
        self.make_appointment(open_order, status='confirmed', ended=False)

        delivery = build_demand_response_conversion(days=30)['delivery_funnel']
        attendance = {
            row['key']: row['count'] for row in delivery['attendance_rows']
        }
        confirmations = {
            row['key']: row['count']
            for row in delivery['delivery_confirmation_rows']
        }

        self.assertEqual(delivery['confirmed_ended_count'], 4)
        self.assertEqual(delivery['confirmed_open_count'], 1)
        self.assertEqual(attendance, {
            'both_arrived': 1,
            'buyer_only': 1,
            'seller_only': 1,
            'neither_arrived': 1,
        })
        self.assertEqual(delivery['both_arrived_rate'], 25.0)
        self.assertEqual(confirmations, {
            'both_confirmed': 1,
            'buyer_only': 1,
            'seller_only': 1,
            'not_started': 2,
        })
        self.assertEqual(delivery['delivery_completion_rate'], 20.0)

    def test_order_reached_through_a_selected_gift_application_enters_delivery(self):
        demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        response = self.make_response(demand, item)
        application = self.make_gift_application(response, status='selected')
        order = self.make_order(
            item, self.requester, status='meeting', demand_response=None,
        )
        GiftApplication.objects.filter(pk=application.pk).update(order=order)
        self.make_appointment(order, status='confirmed', ended=True)

        delivery = build_demand_response_conversion(days=30)['delivery_funnel']
        self.assertEqual(delivery['order_count'], 1)
        self.assertEqual(delivery['appointment_count'], 1)
        self.assertEqual(delivery['confirmed_ended_count'], 1)

    def test_gift_stage_report_uses_a_fixed_number_of_queries(self):
        for _ in range(5):
            demand = self.make_demand()
            item = self.make_item(trade_mode='free')
            response = self.make_response(demand, item)
            order = self.make_order(
                item, self.requester, status='meeting',
                demand_response=response,
            )
            self.make_appointment(order, status='confirmed', ended=True)
            self.make_delivery_confirmation(order)
            self.make_gift_application(response)

        with self.assertNumQueries(5):
            report = build_demand_response_conversion(days=30)
        self.assertEqual(report['delivery_funnel']['order_count'], 5)

    def test_operations_fragment_renders_delivery_funnel_without_guessing(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(
            item, self.requester, status='completed', demand_response=response,
        )
        self.make_appointment(
            order, status='confirmed', ended=True,
            buyer_arrived=True, seller_arrived=True,
        )
        self.make_delivery_confirmation(order, buyer=True, seller=True)

        staff = make_user('conversion-delivery-staff')
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        self.client.force_login(staff)
        fragment = self.client.get(
            reverse(
                'operations_dashboard_fragment',
                kwargs={'anchor': 'ops-deal-outcomes'},
            ),
            {'days': 30},
        )

        self.assertEqual(fragment.status_code, 200)
        self.assertContains(fragment, 'id="ops-demand-delivery"')
        self.assertContains(fragment, '确认之后，预约和交付走完了吗')
        self.assertContains(fragment, '订单数作分母')
        self.assertContains(fragment, '预约已确认且已结束')
        self.assertContains(fragment, '双方到场')
        self.assertContains(fragment, '双方确认交付')
        self.assertContains(fragment, '作为右删失样本单列')

    # --- 重新选择后的响应闭环 ------------------------------------------

    def test_reopen_funnel_cuts_on_the_reopen_moment(self):
        now = timezone.now()
        old_demand = self.make_demand()
        old_item = self.make_item()
        old_response = self.make_response(
            old_demand, old_item, status='superseded',
            accepted_at=now - timedelta(days=20),
        )
        self.make_reopen_event(
            old_demand, old_response, reopened_at=now - timedelta(days=40), count=1,
        )
        self.make_response(
            old_demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )

        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=6),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=4), count=1,
        )
        self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )

        report = build_demand_response_conversion(days=30, now=now)
        funnel = report['reopen_funnel']
        self.assertEqual(funnel['event_count'], 1)
        self.assertEqual(funnel['reconfirmed_count'], 1)

    def test_backfill_events_are_disclosed_but_excluded_from_exact_funnel(self):
        now = timezone.now()
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=5),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=3),
            count=1, source='backfill',
        )
        self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )

        report = build_demand_response_conversion(days=30, now=now)
        funnel = report['reopen_funnel']
        self.assertTrue(report['has_data'])
        self.assertEqual(funnel['backfill_event_count'], 1)
        self.assertEqual(funnel['event_count'], 0)
        self.assertEqual(funnel['reconfirmed_count'], 0)
        current = funnel['trend']['periods'][-1]
        self.assertEqual(current['event_count'], 0)
        self.assertEqual(current['backfill_event_count'], 1)
        self.assertEqual(funnel['trend']['alerts'], [])

    def test_previous_period_reopen_keeps_a_later_confirmation(self):
        now = timezone.now()
        period_start = build_demand_response_conversion(
            days=7, now=now,
        )['period_start']
        previous_start = period_start - timedelta(days=7)
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=previous_start + timedelta(hours=1),
        )
        self.make_reopen_event(
            demand, replaced,
            reopened_at=previous_start + timedelta(hours=2), count=1,
        )
        self.make_response(
            demand, self.make_item(),
            accepted_at=period_start + timedelta(hours=1),
        )

        periods = {
            row['key']: row
            for row in build_demand_response_conversion(
                days=7, now=now,
            )['reopen_funnel']['trend']['periods']
        }
        self.assertEqual(periods['previous']['event_count'], 1)
        self.assertEqual(periods['previous']['reconfirmed_count'], 1)
        self.assertEqual(periods['current']['event_count'], 0)
        self.assertEqual(periods['current']['reconfirmed_count'], 0)

    def test_a_later_reopen_stops_an_earlier_event_claiming_the_newer_confirmation(self):
        now = timezone.now()
        period_start = build_demand_response_conversion(
            days=7, now=now,
        )['period_start']
        previous_start = period_start - timedelta(days=7)
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=previous_start + timedelta(hours=1),
        )
        self.make_reopen_event(
            demand, replaced,
            reopened_at=previous_start + timedelta(hours=2), count=1,
        )
        intermediate = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=period_start + timedelta(hours=1),
        )
        self.make_reopen_event(
            demand, intermediate,
            reopened_at=period_start + timedelta(hours=2), count=2,
        )
        self.make_response(
            demand, self.make_item(),
            accepted_at=period_start + timedelta(hours=3),
        )

        periods = {
            row['key']: row
            for row in build_demand_response_conversion(
                days=7, now=now,
            )['reopen_funnel']['trend']['periods']
        }
        self.assertEqual(periods['previous']['event_count'], 1)
        self.assertEqual(periods['previous']['reconfirmed_count'], 1)
        self.assertEqual(periods['current']['event_count'], 1)
        self.assertEqual(periods['current']['reconfirmed_count'], 1)

    def test_trend_windows_are_contiguous_and_labels_hide_open_ends(self):
        now = timezone.now()
        trend = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']['trend']
        prior, previous, current = trend['periods']

        self.assertEqual(prior['period_end'], previous['period_start'])
        self.assertEqual(previous['period_end'], current['period_start'])
        self.assertEqual(
            previous['period_end_label'],
            timezone.localtime(
                previous['period_end'] - timedelta(days=1),
            ).strftime('%m-%d'),
        )
        self.assertEqual(
            current['period_end_label'],
            timezone.localtime(now).strftime('%m-%d'),
        )

    def test_historical_runtime_event_keeps_the_panel_visible(self):
        now = timezone.now()
        period_start = build_demand_response_conversion(
            days=7, now=now,
        )['period_start']
        previous_start = period_start - timedelta(days=7)
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=previous_start + timedelta(hours=1),
        )
        self.make_reopen_event(
            demand, replaced,
            reopened_at=previous_start + timedelta(hours=2), count=1,
        )

        report = build_demand_response_conversion(days=7, now=now)
        self.assertTrue(report['has_data'])
        self.assertTrue(report['reopen_funnel']['trend']['has_data'])
        self.assertEqual(report['reopen_funnel']['event_count'], 0)
        self.assertEqual(
            report['reopen_funnel']['trend']['periods'][1]['event_count'],
            1,
        )

        staff = make_user('conversion-history-staff')
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        self.client.force_login(staff)
        fragment = self.client.get(
            reverse(
                'operations_dashboard_fragment',
                kwargs={'anchor': 'ops-deal-outcomes'},
            ),
            {'days': 7},
        )
        self.assertContains(fragment, 'id="ops-reopen-funnel"')
        self.assertContains(fragment, '三个周期里的重新选择闭环')

    def test_trend_alert_is_built_from_runtime_periods(self):
        now = timezone.now()
        period_start = build_demand_response_conversion(
            days=30, now=now,
        )['period_start']
        prior_start = period_start - timedelta(days=60)
        previous_start = period_start - timedelta(days=30)
        periods = (
            (prior_start, 12, 12),
            (previous_start, 10, 8),
            (period_start, 8, 6),
        )
        for window_start, reconfirmed_count, order_count in periods:
            event_at = window_start + timedelta(days=1)
            for index in range(12):
                demand = self.make_demand()
                replaced = self.make_response(
                    demand, self.make_item(status='unavailable'),
                    status='superseded',
                    accepted_at=event_at - timedelta(hours=1),
                )
                self.make_reopen_event(
                    demand, replaced, reopened_at=event_at, count=index + 1,
                )
                if index >= reconfirmed_count:
                    continue
                confirmation = self.make_response(
                    demand, self.make_item(),
                    accepted_at=event_at + timedelta(hours=2),
                )
                if index < order_count:
                    order = self.make_order(
                        confirmation.item, self.requester,
                        demand_response=confirmation,
                    )
                    order.created_at = event_at + timedelta(hours=3)
                    order.save(update_fields=['created_at'])

        trend = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']['trend']
        self.assertEqual(
            [row['reconfirmed_count'] for row in trend['periods']],
            [12, 10, 8],
        )
        self.assertEqual(
            [row['order_count'] for row in trend['periods']],
            [12, 8, 6],
        )
        self.assertEqual(
            [alert['key'] for alert in trend['alerts']],
            ['reopen_reconfirmation_decline'],
        )
    def test_reopen_event_count_is_not_the_same_as_affected_demand_count(self):
        now = timezone.now()
        demand = self.make_demand()
        first_replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=8),
        )
        self.make_reopen_event(
            demand, first_replaced, reopened_at=now - timedelta(days=6), count=1,
        )
        first_confirmation = self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=5),
        )
        DemandResponse.objects.filter(pk=first_confirmation.pk).update(
            status='superseded',
        )
        first_confirmation.refresh_from_db()
        self.make_reopen_event(
            demand, first_confirmation, reopened_at=now - timedelta(days=4), count=2,
        )
        self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        self.assertEqual(funnel['event_count'], 2)
        self.assertEqual(funnel['affected_demand_count'], 1)
        self.assertEqual(funnel['reconfirmed_count'], 2)

    def test_first_confirmation_after_reopen_wins(self):
        now = timezone.now()
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=7),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=5), count=1,
        )
        first = self.make_response(
            demand, self.make_item(trade_mode='free'),
            accepted_at=now - timedelta(days=4),
        )
        self.make_gift_application(first, status='pending', hours_after_confirmation=1)
        DemandResponse.objects.filter(pk=first.pk).update(status='superseded')
        first.refresh_from_db()
        later = self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )
        order = self.make_order(
            later.item, self.requester, demand_response=later,
        )
        order.created_at = later.accepted_at + timedelta(hours=1)
        order.save(update_fields=['created_at'])

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        self.assertEqual(funnel['reconfirmed_count'], 1)
        self.assertEqual(funnel['gift_application_count'], 1)
        self.assertEqual(funnel['order_count'], 0)

    def test_confirmation_before_reopen_is_ignored(self):
        now = timezone.now()
        demand = self.make_demand()
        item = self.make_item()
        replaced = self.make_response(
            demand, item, status='superseded',
            accepted_at=now - timedelta(days=5),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=3), count=1,
        )
        self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=4),
        )

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        self.assertEqual(funnel['event_count'], 1)
        self.assertEqual(funnel['reconfirmed_count'], 0)
        self.assertEqual(funnel['not_reconfirmed_count'], 1)

    def test_confirmation_after_the_next_reopen_belongs_to_the_later_event(self):
        now = timezone.now()
        demand = self.make_demand()
        first_replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=8),
        )
        self.make_reopen_event(
            demand, first_replaced, reopened_at=now - timedelta(days=6), count=1,
        )
        second_replaced = self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=9),
        )
        DemandResponse.objects.filter(pk=second_replaced.pk).update(
            status='superseded',
        )
        second_replaced.refresh_from_db()
        self.make_reopen_event(
            demand, second_replaced, reopened_at=now - timedelta(days=4), count=2,
        )
        self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        rows = {row['key']: row for row in funnel['reopen_count_rows']}
        self.assertEqual(rows['1']['reconfirmed_count'], 0)
        self.assertEqual(rows['2']['reconfirmed_count'], 1)

    def test_reconfirmed_order_contributes_once_even_with_a_gift_application(self):
        now = timezone.now()
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=6),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=4), count=1,
        )
        confirmation = self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=3),
        )
        order = self.make_order(
            confirmation.item, self.requester, demand_response=confirmation,
        )
        order.created_at = confirmation.accepted_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])
        self.make_gift_application(confirmation, status='pending')

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        self.assertEqual(funnel['order_count'], 1)
        self.assertEqual(funnel['gift_application_count'], 0)
        self.assertEqual(funnel['transaction_count'], 1)

    def test_reconfirmed_gift_application_is_not_an_order(self):
        now = timezone.now()
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=6),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=4), count=1,
        )
        confirmation = self.make_response(
            demand, self.make_item(trade_mode='free'),
            accepted_at=now - timedelta(days=3),
        )
        self.make_gift_application(confirmation, status='pending')

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        self.assertEqual(funnel['reconfirmed_count'], 1)
        self.assertEqual(funnel['order_count'], 0)
        self.assertEqual(funnel['gift_application_count'], 1)
        self.assertEqual(funnel['transaction_count'], 1)
        self.assertEqual(funnel['no_transaction_count'], 0)

    def test_reconfirmed_without_transaction_reports_why(self):
        now = timezone.now()
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=6),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=4), count=1,
        )
        confirmation_item = self.make_item()
        self.make_response(
            demand, confirmation_item, accepted_at=now - timedelta(days=3),
        )
        confirmation_item.status = 'sold'
        confirmation_item.save(update_fields=['status', 'updated_at'])

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        stages = {row['key']: row for row in funnel['no_order_stage_rows']}
        self.assertEqual(funnel['reconfirmed_count'], 1)
        self.assertEqual(funnel['transaction_count'], 0)
        self.assertEqual(funnel['no_transaction_count'], 1)
        self.assertEqual(stages['item_gone']['count'], 1)

    def test_reopen_latencies_are_measured_from_the_event(self):
        now = timezone.now()
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=6),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=3), count=1,
        )
        confirmation = self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )
        order = self.make_order(
            confirmation.item, self.requester, demand_response=confirmation,
        )
        order.created_at = confirmation.accepted_at + timedelta(hours=4)
        order.save(update_fields=['created_at'])

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        self.assertEqual(funnel['reopen_to_reconfirmation']['total'], 1)
        self.assertEqual(funnel['reopen_to_reconfirmation']['same_day_count'], 0)
        self.assertEqual(funnel['reconfirmation_to_transaction']['total'], 1)
        self.assertEqual(
            funnel['reconfirmation_to_transaction']['same_day_count'], 1,
        )

    def test_reopen_strata_and_conservation_cover_every_event(self):
        now = timezone.now()
        for index, trade_mode in enumerate(('sale', 'free', 'borrow'), start=1):
            demand = self.make_demand()
            replaced = self.make_response(
                demand, self.make_item(trade_mode=trade_mode), status='superseded',
                accepted_at=now - timedelta(days=8),
            )
            self.make_reopen_event(
                demand, replaced,
                reopened_at=now - timedelta(days=6 + index), count=index,
            )
            confirmation = self.make_response(
                demand, self.make_item(trade_mode=trade_mode),
                accepted_at=now - timedelta(days=3),
            )
            if trade_mode == 'sale':
                order = self.make_order(
                    confirmation.item, self.requester,
                    demand_response=confirmation,
                )
                order.created_at = confirmation.accepted_at + timedelta(hours=2)
                order.save(update_fields=['created_at'])
            elif trade_mode == 'free':
                self.make_gift_application(confirmation, status='pending')

        funnel = build_demand_response_conversion(
            days=30, now=now,
        )['reopen_funnel']
        self.assertEqual(funnel['event_count'], 3)
        self.assertEqual(
            sum(row['event_count'] for row in funnel['reopen_count_rows']),
            funnel['event_count'],
        )
        self.assertEqual(
            sum(row['event_count'] for row in funnel['trade_mode_rows']),
            funnel['event_count'],
        )
        self.assertEqual(
            funnel['order_count'] + funnel['no_transaction_count'],
            funnel['reconfirmed_count'],
        )
        self.assertEqual(
            sum(row['count'] for row in funnel['order_stage_rows'])
            + sum(row['count'] for row in funnel['no_order_stage_rows']),
            funnel['reconfirmed_count'],
        )

    # --- 交易方式 ------------------------------------------------------

    def test_conversion_is_split_by_trade_mode(self):
        # 出售要看钱，免费和借用都不涉及付款，走完那段路的动力完全不同。
        for trade_mode in ('sale', 'free', 'borrow'):
            demand = self.make_demand()
            item = self.make_item(trade_mode=trade_mode)
            response = self.make_response(demand, item)
            order = self.make_order(item, response.demand.requester)
            order.created_at = response.accepted_at + timedelta(hours=1)
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
        order = self.make_order(item, response.demand.requester)
        order.created_at = response.accepted_at + timedelta(hours=4)
        order.save(update_fields=['created_at'])

        report = build_demand_response_conversion(days=30)
        self.assertIn('真的变成了订单', report['summary'])
        self.assertIn('确认到下单', report['summary'])

    def test_operations_csv_exports_gift_stage_instead_of_still_walking(self):
        demand = self.make_demand()
        item = self.make_item(trade_mode='free')
        response = self.make_response(demand, item)
        self.make_gift_application(response, status='pending')

        admin = make_user('conversion-export-admin')
        admin.is_staff = True
        admin.save(update_fields=['is_staff'])
        self.client.force_login(admin)
        export = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )

        self.assertEqual(export.status_code, 200)
        content = export.content.decode('utf-8-sig')
        self.assertIn('确认后没有订单的领取申请阶段', content)
        self.assertIn('GiftApplication 的 demand_response_id', content)
        self.assertIn('等待卖家选择,1,100.0', content)
        self.assertIn('领取申请发起率,100.0%', content)
        self.assertIn('本身不算订单转化', content)
        self.assertIn(
            '订单转化 0 条 + 未生成订单 1 条 = 周期内确认响应 1 条',
            content,
        )
        self.assertIn('确认后未生成订单的响应阶段', content)
        self.assertNotIn('确认之后没有订单的，卡在哪', content)
        self.assertNotIn('两边都没动', content)

    def test_operations_csv_exports_delivery_funnel(self):
        demand = self.make_demand()
        item = self.make_item()
        response = self.make_response(demand, item)
        order = self.make_order(
            item, self.requester, status='completed', demand_response=response,
        )
        self.make_appointment(
            order, status='confirmed', ended=True,
            buyer_arrived=True, seller_arrived=True,
        )
        self.make_delivery_confirmation(order, buyer=True, seller=True)

        admin = make_user('conversion-delivery-export-admin')
        admin.is_staff = True
        admin.save(update_fields=['is_staff'])
        self.client.force_login(admin)
        export = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )

        self.assertEqual(export.status_code, 200)
        content = export.content.decode('utf-8-sig')
        self.assertIn('确认响应后的交付漏斗', content)
        self.assertIn('去重来源订单,1', content)
        self.assertIn('提出过预约,1,100.0', content)
        self.assertIn('已结束确认预约,1', content)
        self.assertIn('双方到场,1,100.0', content)
        self.assertIn('双方确认交付,1,100.0', content)
        self.assertIn('来源订单的预约阶段', content)
        self.assertIn('双方已确认交付,1,100.0', content)
        self.assertIn('MeetingAppointment 保存每笔订单的当前版本', content)
        self.assertIn('MeetingAppointmentEvent', content)

    def test_operations_csv_exports_runtime_reopen_funnel(self):
        now = timezone.now()
        demand = self.make_demand()
        replaced = self.make_response(
            demand, self.make_item(), status='superseded',
            accepted_at=now - timedelta(days=6),
        )
        self.make_reopen_event(
            demand, replaced, reopened_at=now - timedelta(days=4), count=1,
        )
        reconfirmed = self.make_response(
            demand, self.make_item(), accepted_at=now - timedelta(days=2),
        )
        order = self.make_order(
            reconfirmed.item, self.requester, demand_response=reconfirmed,
        )
        order.created_at = reconfirmed.accepted_at + timedelta(hours=2)
        order.save(update_fields=['created_at'])

        admin = make_user('conversion-reopen-export-admin')
        admin.is_staff = True
        admin.save(update_fields=['is_staff'])
        self.client.force_login(admin)
        export = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )

        self.assertEqual(export.status_code, 200)
        content = export.content.decode('utf-8-sig')
        self.assertIn('重新选择后的响应闭环漏斗', content)
        self.assertIn('周期内精确事件,1', content)
        self.assertIn('事件后再次确认,1', content)
        self.assertIn(
            '再次确认 1 次 = 订单 1 次 + 未发起交易入口 0 次',
            content,
        )
        self.assertIn('历史兼容回填事件,0', content)
        self.assertIn('由迁移从旧 superseded_at 重建', content)
