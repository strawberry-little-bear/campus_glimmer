# -*- coding: utf-8 -*-
"""归还登记的那两次握手，第一次被运营读到。

``buyer_returned_at`` 和 ``seller_returned_at`` 一直在写，而运营侧一条统计都没有。
这个模块把这件事补上，这些测试要钉住的不是四舍五入，而是五件更根本的事。

第一件是分母。只有「周期内创建、且已经离开 ``borrowed``」的借用订单才谈得上有没有
走完握手；一笔还挂在 ``borrowed`` 的订单，握手还没结束，把它算进分母会让完成率凭空
变小。这是右删失样本，和 ``handoff_code_usage``、``meeting_attendance`` 是同一个道理。

第二件是取消的两种含义。一笔取消的借用订单，可能是下单之后没约成（握手压根没开始），
也可能是东西已经拿走、最后走的是取消而不是归还确认（握手发生过但没走完）。前者不能
进分母，后者必须进——不区分这两者，取消的订单会被当成同一类处理，而它们的运营含义
正相反。

第三件是等待时长必须按天分档。一次归还等待的时间尺度是天，报「平均等了 26 小时」
对一个按周借用、按周归还的场景没有意义。

第四件是「逾期未归还」和「已登记未确认」必须分开报。这两类都表现为「订单还没结束」，
但运营动作完全相反：前者是催收阶梯该管的事，后者是「去提醒另一方点一下」的事。把它
们合成一个数字，会让人以为问题只有一种。

第五件是报「登记之后订单再也没有动过」而不是报「卡住」。前者是 ``OrderEvent`` 里可以
直接数出来的事实，后者是一个判断——平台没有能力区分一笔订单是忘了、在争议中、还是
双方已经在线下解决了，也就不该替运营下这个结论。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .return_handshake import (
    MIN_MEASURED_ORDERS,
    WAIT_BUCKETS,
    build_return_handshake,
)
from .models import (
    Category, CampusLocation, DeliveryConfirmation, Item, Order, OrderEvent,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class ReturnHandshakeSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('handshake-seller')
        self.buyer = make_user('handshake-buyer')
        self.category = Category.objects.create(name='握手分类')
        self.location = CampusLocation.objects.create(name='握手地点')

    def make_item(self, title='握手商品'):
        return Item.objects.create(
            title=title, description='用于归还握手统计', price=Decimal('30.00'),
            status='available', trade_mode='borrow', seller=self.seller,
            category=self.category, location=self.location,
        )

    def make_order(self, *, status='returned', item=None, days_ago=1,
                   due_days=7, returned=False):
        """一笔借用订单。每笔各建一件商品：一件商品同时只能有一笔进行中的订单。"""
        item = item or self.make_item()
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
            return_due_at=self.now + timedelta(days=due_days) if due_days is not None else None,
            returned_at=self.now - timedelta(days=1) if returned else None,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_days_ago=None, seller_days_ago=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        if buyer_days_ago is not None:
            confirmation.buyer_returned_at = self.now - timedelta(days=buyer_days_ago)
        if seller_days_ago is not None:
            confirmation.seller_returned_at = self.now - timedelta(days=seller_days_ago)
        confirmation.save()
        return confirmation

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_return_handshake(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['completion_stats']['concluded_total'], 0)
        self.assertIn('还没有走完借用的订单', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_return_handshake(days=999, now=self.now)

        self.assertEqual(report['period_days'], 30)

    def test_an_order_created_before_the_window_is_excluded(self):
        self.make_order(days_ago=45)

        report = build_return_handshake(days=30, now=self.now)

        self.assertFalse(report['has_data'])

    def test_a_sale_order_is_not_a_borrow(self):
        """出售订单根本不经过 borrowed，两个归还时间戳永远为空。"""
        item = self.make_item(title='出售商品')
        Item.objects.filter(pk=item.pk).update(trade_mode='sale')
        item.refresh_from_db()
        self.make_order(item=item)

        report = build_return_handshake(days=30, now=self.now)

        self.assertFalse(report['has_data'])


class DenominatorTests(TestCase):
    """分母只收已经离开 borrowed 的订单——右删失样本不能进分母。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('denom-seller')
        self.buyer = make_user('denom-buyer')
        self.category = Category.objects.create(name='分母分类')
        self.location = CampusLocation.objects.create(name='分母地点')
        self.item = Item.objects.create(
            title='分母商品', description='用于分母统计', price=Decimal('30.00'),
            status='available', trade_mode='borrow', seller=self.seller,
            category=self.category, location=self.location,
        )

    def make_order(self, status='returned', days_ago=1):
        # 一笔商品同时只能有一笔进行中的订单，所以每笔订单各建一件商品。
        item = Item.objects.create(
            title=f'分母商品 {days_ago} {status}', description='x', price=Decimal('1.00'),
            status='available', trade_mode='borrow', seller=self.seller,
            category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def test_an_order_still_in_borrowed_never_enters_the_denominator(self):
        """还挂在 borrowed 的订单，握手还没结束，谈不上有没有完成。"""
        self.make_order(status='borrowed')

        report = build_return_handshake(days=30, now=self.now)

        stats = report['completion_stats']
        self.assertEqual(stats['concluded_total'], 0)
        self.assertEqual(report['still_borrowed_count'], 1)
        self.assertEqual(stats['completion_rate'], 0)
        # 它不能被丢掉：完成率旁边必须能看见它，否则没人知道分母为什么这么小。
        self.assertIn('还挂在借用中', report['summary'])

    def test_a_returned_order_counts_in_the_denominator(self):
        order = self.make_order(status='returned')
        OrderEvent.objects.create(
            order=order, from_status='meeting', to_status='borrowed',
            note='双方确认借用已开始',
        )

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['completion_stats']['concluded_total'], 1)
        self.assertEqual(report['no_borrow_count'], 0)

    def test_a_cancelled_order_that_never_entered_borrowed_stays_out(self):
        """下单之后没约成就取消的订单，两次握手压根没开始。"""
        self.make_order(status='cancelled')

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['completion_stats']['concluded_total'], 0)
        self.assertEqual(report['cancelled_before_count'], 1)
        self.assertIn('握手没开始', report['summary'])

    def test_a_cancelled_order_that_did_enter_borrowed_counts_in_the_denominator(self):
        """东西已经拿走、最后走的是取消而不是归还确认——握手发生过但没走完。"""
        order = self.make_order(status='cancelled')
        OrderEvent.objects.create(
            order=order, from_status='meeting', to_status='borrowed',
            note='双方确认借用已开始',
        )

        report = build_return_handshake(days=30, now=self.now)

        stats = report['completion_stats']
        self.assertEqual(stats['concluded_total'], 1)
        self.assertEqual(stats['cancelled_count'], 1)
        self.assertEqual(stats['returned_count'], 0)
        self.assertEqual(stats['completion_rate'], 0)

    def test_an_order_that_never_entered_borrow_is_reported_as_a_contradiction(self):
        """状态是已归还却查不到进入借用中的事件——这不该发生，但不许静默忽略。"""
        self.make_order(status='returned')

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['no_borrow_count'], 1)
        self.assertEqual(report['completion_stats']['concluded_total'], 0)
        self.assertIn('矛盾', report['summary'])

    def test_self_referential_events_do_not_count_as_entering_borrowed(self):
        """签到、约定交付时间这类 from_status == to_status 的事件不是阶段切换。"""
        order = self.make_order(status='cancelled')
        OrderEvent.objects.create(
            order=order, from_status='borrowed', to_status='borrowed',
            note='约定了交付地点',
        )

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['cancelled_before_count'], 1)


class CompletionTests(TestCase):
    """握手完成率，以及「双方都登记」和「走到了 returned」之间的一致性。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('completion-seller')
        self.buyer = make_user('completion-buyer')
        self.category = Category.objects.create(name='完成率分类')
        self.location = CampusLocation.objects.create(name='完成率地点')

    def make_borrow(self, *, status='returned', buyer_days_ago=None, seller_days_ago=None):
        item = Item.objects.create(
            title=f'完成率商品 {status} {buyer_days_ago} {seller_days_ago}',
            description='x', price=Decimal('1.00'), status='available',
            trade_mode='borrow',
            seller=self.seller, category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=2),
        )
        order.refresh_from_db()
        OrderEvent.objects.create(
            order=order, from_status='meeting', to_status='borrowed',
            note='双方确认借用已开始',
        )
        if buyer_days_ago is not None or seller_days_ago is not None:
            confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
            if buyer_days_ago is not None:
                confirmation.buyer_returned_at = self.now - timedelta(days=buyer_days_ago)
            if seller_days_ago is not None:
                confirmation.seller_returned_at = self.now - timedelta(days=seller_days_ago)
            confirmation.save()
        return order

    def test_both_sides_registered_counts_as_completed(self):
        self.make_borrow(buyer_days_ago=3, seller_days_ago=2)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['completion_stats']
        self.assertEqual(stats['concluded_total'], 1)
        self.assertEqual(stats['returned_count'], 1)
        self.assertEqual(stats['completion_rate'], 100.0)

    def test_only_one_side_registered_is_not_completed(self):
        """一方登记、另一方没有，订单不会走到 returned——这不是一次完成的握手。"""
        self.make_borrow(status='borrowed', buyer_days_ago=3)

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['completion_stats']['concluded_total'], 0)
        self.assertEqual(report['still_borrowed_count'], 1)
        self.assertEqual(report['stalled_stats']['registered_count'], 1)
        self.assertEqual(report['stalled_stats']['overdue_count'], 0)

    def test_a_both_registered_order_stuck_in_borrowed_is_flagged(self):
        """两次握手都点了却没有走到 returned，是数据里的一处矛盾。"""
        self.make_borrow(status='borrowed', buyer_days_ago=3, seller_days_ago=2)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['completion_stats']
        self.assertEqual(stats['concluded_total'], 0)
        self.assertEqual(stats['both_registered_count'], 0)
        # 这矛盾不在分母里，但必须出现在卡在中间态的清单上，否则没人会看见它。
        self.assertEqual(len(report['stalled_rows']), 1)

    def test_small_samples_are_labelled_rather_than_judged(self):
        self.make_borrow(buyer_days_ago=3, seller_days_ago=2)

        report = build_return_handshake(days=30, now=self.now)

        self.assertTrue(report['completion_stats']['is_small_sample'])
        self.assertEqual(report['completion_stats']['min_measured_orders'], MIN_MEASURED_ORDERS)
        self.assertIn('占比只作参考', report['summary'])


class FirstRegisteredTests(TestCase):
    """谁先登记：借用方先登记是正常路径，发布者先登记是反常的。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('first-seller')
        self.buyer = make_user('first-buyer')
        self.category = Category.objects.create(name='先后分类')
        self.location = CampusLocation.objects.create(name='先后地点')

    def make_borrow(self, *, buyer_days_ago=None, seller_days_ago=None, status='returned'):
        item = Item.objects.create(
            title=f'先后商品 {buyer_days_ago} {seller_days_ago}',
            description='x', price=Decimal('1.00'), status='available',
            trade_mode='borrow',
            seller=self.seller, category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=5))
        order.refresh_from_db()
        OrderEvent.objects.create(
            order=order, from_status='meeting', to_status='borrowed', note='借用开始',
        )
        if buyer_days_ago is not None or seller_days_ago is not None:
            confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
            if buyer_days_ago is not None:
                confirmation.buyer_returned_at = self.now - timedelta(days=buyer_days_ago)
            if seller_days_ago is not None:
                confirmation.seller_returned_at = self.now - timedelta(days=seller_days_ago)
            confirmation.save()
        return order

    def test_the_buyer_registering_first_is_the_normal_path(self):
        self.make_borrow(buyer_days_ago=5, seller_days_ago=1)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['first_registered_stats']
        self.assertEqual(stats['buyer_first_count'], 1)
        self.assertEqual(stats['seller_first_count'], 0)

    def test_the_seller_registering_first_is_flagged_as_the_abnormal_path(self):
        """发布者先登记意味着他先收到了东西、而借用方还没点。"""
        self.make_borrow(buyer_days_ago=1, seller_days_ago=5)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['first_registered_stats']
        self.assertEqual(stats['seller_first_count'], 1)
        self.assertEqual(stats['buyer_first_count'], 0)
        self.assertIn('发布者先登记', report['summary'])

    def test_both_registered_in_the_same_second_is_its_own_bucket(self):
        """两个人各自点一下，两个请求可以落在同一秒内。"""
        self.make_borrow(buyer_days_ago=2, seller_days_ago=2)

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['first_registered_stats']['both_count'], 1)

    def test_orders_with_no_registration_at_all_are_counted_separately(self):
        self.make_borrow(buyer_days_ago=None, seller_days_ago=None)
        self.make_borrow(buyer_days_ago=3, seller_days_ago=1)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['first_registered_stats']
        self.assertEqual(stats['unregistered_count'], 1)
        self.assertEqual(stats['measured_total'], 1)

    def test_the_direction_is_withheld_below_the_sample_threshold(self):
        self.make_borrow(buyer_days_ago=5, seller_days_ago=1)

        report = build_return_handshake(days=30, now=self.now)

        self.assertFalse(report['first_registered_stats']['has_direction'])
        self.assertIn('只作参考', report['summary'])


class WaitStatsTests(TestCase):
    """等待时长按天分档，不按小时。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('wait-seller')
        self.buyer = make_user('wait-buyer')
        self.category = Category.objects.create(name='等待分类')
        self.location = CampusLocation.objects.create(name='等待地点')

    def make_borrow(self, *, buyer_days_ago, seller_days_ago):
        item = Item.objects.create(
            title=f'等待商品 {buyer_days_ago} {seller_days_ago}',
            description='x', price=Decimal('1.00'), status='available',
            trade_mode='borrow',
            seller=self.seller, category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='returned',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=20))
        order.refresh_from_db()
        OrderEvent.objects.create(
            order=order, from_status='meeting', to_status='borrowed', note='借用开始',
        )
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_returned_at = self.now - timedelta(days=buyer_days_ago)
        confirmation.seller_returned_at = self.now - timedelta(days=seller_days_ago)
        confirmation.save()
        return order

    def test_the_wait_is_measured_from_the_first_registration_to_the_second(self):
        self.make_borrow(buyer_days_ago=5, seller_days_ago=2)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['wait_stats']
        self.assertEqual(stats['measured_count'], 1)
        self.assertEqual(stats['median_label'], '3 天')
        # 3 天整落在「3-7 天」档：第一档的上界是当天，第二档的上界是 3 天，
        # 所以正好三天的等待不属于「1-3 天」而属于「3-7 天」。分档是左闭右开的，
        # 这一点必须钉住，否则边界上的样本会被两个口径各算一次。
        self.assertEqual(stats['bucket_rows'][2]['label'], '3-7 天')
        self.assertEqual(stats['bucket_rows'][2]['count'], 1)
        self.assertEqual(stats['bucket_rows'][1]['count'], 0)

    def test_a_one_sided_registration_never_enters_the_wait_table(self):
        """一方登记、另一方没有：它没有「等待多久」可言，只有「等了多久还没等到」。"""
        item = Item.objects.create(
            title='单边商品', description='x', price=Decimal('1.00'), status='available',
            trade_mode='borrow',
            seller=self.seller, category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='borrowed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=20))
        order.refresh_from_db()
        OrderEvent.objects.create(
            order=order, from_status='meeting', to_status='borrowed', note='借用开始',
        )
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_returned_at = self.now - timedelta(days=5)
        confirmation.save()

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['wait_stats']['measured_count'], 0)
        self.assertEqual(report['stalled_stats']['registered_count'], 1)

    def test_the_buckets_are_day_scaled_and_mutually_exclusive(self):
        labels = [label for _limit, label, _hint in WAIT_BUCKETS]
        self.assertEqual(
            labels, ['当天', '1-3 天', '3-7 天', '7-14 天', '超过两周'],
        )
        limits = [limit for limit, _label, _hint in WAIT_BUCKETS]
        self.assertEqual(limits, [86400, 3 * 86400, 7 * 86400, 14 * 86400, None])

    def test_a_half_day_wait_is_reported_as_same_day_not_in_hours(self):
        item = Item.objects.create(
            title='半天商品', description='x', price=Decimal('1.00'), status='available',
            trade_mode='borrow',
            seller=self.seller, category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='returned',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=20))
        order.refresh_from_db()
        OrderEvent.objects.create(
            order=order, from_status='meeting', to_status='borrowed', note='借用开始',
        )
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_returned_at = self.now - timedelta(hours=12)
        confirmation.seller_returned_at = self.now - timedelta(hours=6)
        confirmation.save()

        report = build_return_handshake(days=30, now=self.now)

        stats = report['wait_stats']
        self.assertEqual(stats['median_label'], '当天')
        self.assertEqual(stats['bucket_rows'][0]['count'], 1)
        self.assertEqual(stats['same_day_count'], 1)


class StalledTests(TestCase):
    """逾期未归还要和「已登记未确认」分开报——它们的运营动作完全相反。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('stalled-seller')
        self.buyer = make_user('stalled-buyer')
        self.category = Category.objects.create(name='中间态分类')
        self.location = CampusLocation.objects.create(name='中间态地点')

    def make_borrow(self, *, status='borrowed', due_days=-3, buyer_days_ago=None,
                    seller_days_ago=None, title=None, with_event=True):
        item = Item.objects.create(
            title=title or f'中间态商品 {due_days} {buyer_days_ago} {seller_days_ago}',
            description='x', price=Decimal('1.00'), status='available',
            trade_mode='borrow',
            seller=self.seller, category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status=status,
            return_due_at=self.now + timedelta(days=due_days) if due_days is not None else None,
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=20))
        order.refresh_from_db()
        if with_event:
            OrderEvent.objects.create(
                order=order, from_status='meeting', to_status='borrowed', note='借用开始',
            )
        if buyer_days_ago is not None or seller_days_ago is not None:
            confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
            if buyer_days_ago is not None:
                confirmation.buyer_returned_at = self.now - timedelta(days=buyer_days_ago)
            if seller_days_ago is not None:
                confirmation.seller_returned_at = self.now - timedelta(days=seller_days_ago)
            confirmation.save()
        return order

    def test_an_overdue_borrow_with_no_registration_is_reported_apart(self):
        self.make_borrow(due_days=-3, buyer_days_ago=None, seller_days_ago=None)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['stalled_stats']
        self.assertEqual(stats['overdue_count'], 1)
        self.assertEqual(stats['registered_count'], 0)
        self.assertIn('催收阶梯该管', report['summary'])

    def test_a_registered_borrow_is_not_an_overdue_one(self):
        """一方已登记、另一方没有，和「东西真的没回来」是两件事。"""
        self.make_borrow(due_days=-3, buyer_days_ago=2)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['stalled_stats']
        self.assertEqual(stats['registered_count'], 1)
        self.assertEqual(stats['overdue_count'], 0)
        self.assertIn('提醒另一方点一下', report['summary'])

    def test_the_pending_wait_runs_from_the_registration_to_now(self):
        self.make_borrow(due_days=3, buyer_days_ago=9)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['stalled_stats']
        self.assertEqual(stats['median_label'], '9 天')
        self.assertEqual(stats['over_week_count'], 1)
        self.assertEqual(stats['over_two_weeks_count'], 0)
        self.assertEqual(stats['bucket_rows'][3]['label'], '7-14 天')

    def test_a_two_week_wait_lands_in_the_long_tail_bucket(self):
        self.make_borrow(due_days=30, buyer_days_ago=20)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['stalled_stats']
        self.assertEqual(stats['over_two_weeks_count'], 1)
        self.assertEqual(stats['bucket_rows'][4]['label'], '超过两周')

    def test_a_borrow_still_inside_its_due_date_is_neither_overdue_nor_registered(self):
        self.make_borrow(due_days=7, buyer_days_ago=None)

        report = build_return_handshake(days=30, now=self.now)

        stats = report['stalled_stats']
        self.assertEqual(stats['overdue_count'], 0)
        self.assertEqual(stats['registered_count'], 0)
        self.assertEqual(report['still_borrowed_count'], 1)

    def test_a_row_says_whether_the_order_moved_after_the_registration(self):
        order = self.make_borrow(due_days=3, buyer_days_ago=2)
        OrderEvent.objects.create(
            order=order, from_status='borrowed', to_status='borrowed',
            note='买家补充了一条归还说明', created_at=self.now - timedelta(days=1),
        )

        report = build_return_handshake(days=30, now=self.now)

        row = report['stalled_rows'][0]
        self.assertTrue(row['moved_after_register'])
        self.assertEqual(row['waiting_for'], '卖家确认')

    def test_a_row_with_no_event_at_all_says_so_instead_of_saying_stuck(self):
        """报「无事件」而不是报「卡住」：前者是事实，后者是判断。"""
        self.make_borrow(due_days=3, buyer_days_ago=2, with_event=False)

        report = build_return_handshake(days=30, now=self.now)

        row = report['stalled_rows'][0]
        self.assertFalse(row['moved_after_register'])
        self.assertEqual(row['last_event_label'], '无事件')

    def test_the_longest_waiting_orders_come_first(self):
        self.make_borrow(due_days=3, buyer_days_ago=2, title='等了两天的')
        self.make_borrow(due_days=3, buyer_days_ago=25, title='等了二十五天的')

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(report['stalled_rows'][0]['item_title'], '等了二十五天的')

    def test_the_list_is_capped(self):
        for index in range(25):
            self.make_borrow(
                due_days=3, buyer_days_ago=index + 1, title=f'第 {index} 笔',
            )

        report = build_return_handshake(days=30, now=self.now)

        self.assertEqual(len(report['stalled_rows']), 25)
        self.assertEqual(report['stalled_row_limit'], 20)


class SummaryTests(TestCase):
    """一段话要把三个分母、两类中间态和矛盾都说清楚。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('summary-seller')
        self.buyer = make_user('summary-buyer')
        self.category = Category.objects.create(name='小结分类')
        self.location = CampusLocation.objects.create(name='小结地点')

    def make_borrow(self, *, status='returned', buyer_days_ago=None, seller_days_ago=None,
                    due_days=7, title=None, entered_borrow=True):
        item = Item.objects.create(
            title=title or f'小结商品 {status} {buyer_days_ago} {seller_days_ago}',
            description='x', price=Decimal('1.00'), status='available',
            trade_mode='borrow',
            seller=self.seller, category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status=status,
            return_due_at=self.now + timedelta(days=due_days) if due_days is not None else None,
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=10))
        order.refresh_from_db()
        if entered_borrow:
            OrderEvent.objects.create(
                order=order, from_status='meeting', to_status='borrowed', note='借用开始',
            )
        if buyer_days_ago is not None or seller_days_ago is not None:
            confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
            if buyer_days_ago is not None:
                confirmation.buyer_returned_at = self.now - timedelta(days=buyer_days_ago)
            if seller_days_ago is not None:
                confirmation.seller_returned_at = self.now - timedelta(days=seller_days_ago)
            confirmation.save()
        return order

    def test_the_summary_separates_the_two_kinds_of_open_borrow(self):
        self.make_borrow(status='returned', buyer_days_ago=6, seller_days_ago=4)
        self.make_borrow(
            status='borrowed', buyer_days_ago=None, seller_days_ago=None, due_days=-2,
            title='真的没回来',
        )
        self.make_borrow(
            status='borrowed', buyer_days_ago=5, seller_days_ago=None, due_days=5,
            title='登记了没人确认',
        )

        report = build_return_handshake(days=30, now=self.now)

        summary = report['summary']
        self.assertIn('催收阶梯该管的那一类', summary)
        self.assertIn('提醒另一方点一下', summary)

    def test_the_summary_states_that_the_pending_wait_is_not_cut_by_the_period(self):
        self.make_borrow(status='borrowed', buyer_days_ago=40, due_days=10)

        report = build_return_handshake(days=30, now=self.now)

        # 这笔订单创建于 40 天前，仍在窗口内被统计；等待时长按它的一生累计，
        # 不会被统计周期切成两段。
        self.assertEqual(report['stalled_stats']['registered_count'], 1)
        self.assertEqual(report['stalled_stats']['median_label'], '40 天')

    def test_the_summary_names_the_sample_threshold_when_it_is_not_met(self):
        self.make_borrow(buyer_days_ago=3, seller_days_ago=1)

        report = build_return_handshake(days=30, now=self.now)

        self.assertIn(
            f"不足 {MIN_MEASURED_ORDERS} 笔", report['summary'],
        )
