# -*- coding: utf-8 -*-
"""订单承诺的截止时间与提醒效果，第一次被运营读到。

这四个时间字段一直在写——截止时间写在两个视图里、提醒时间戳写在维护命令里，全都
完整、幂等、有行锁。而读取路径只有后台和订单详情页。这个模块把「平台承诺了什么」
和「催过之后到底有没有用」补上看板，这些测试要钉住的不是四舍五入，而是五件更根本
的事。

第一件是分母。只有「已经离开待卖家确认」的订单才谈得上确认花了多久；一个现在还
挂在本阶段的订单，确认还没发生，把它算进分母会让确认时长凭空变长。这是右删失样本，
和交付确认码、当面交付握手、归还握手三个面板是同一个道理，测试必须钉住它。

第二件是确认时刻只认 `OrderEvent`。`Order.updated_at` 是 `auto_now`，任何一次保存
都会改写它，拿它证明「卖家此刻确认」是不成立的。测试要钉住一笔只有 `updated_at`
被摸过、没有事件行的订单不进时长样本。

第三件是自动释放和用户主动取消必须分开。两者都表现为 `cancelled`，但运营动作完全
相反：一边是「卖家 24 小时没上线」，一边是「买家改了主意」。测试要钉住这个区分只
认事件备注，不按状态猜。

第四件是「催过之后」的四种结局不能合成一个数。提醒之后确认的、提醒之后仍被自动
释放的、提醒之后用户取消的、确认时刻取不到的，是四件不同的事。合成一个「已提醒
N 笔」会把最有效的提醒和最无效的提醒混成一个平均值。

第五件是等待不按统计周期切。一笔跨越周期边界的超时等待，按周期切开会被算成两段
短的，而那正是最不该被切开的样本。测试要钉住等待从截止时刻算到现在，而不是从窗口
起点算。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import (
    Category,
    CampusLocation,
    Item,
    Order,
    OrderEvent,
)
from .order_deadline_reminders import (
    CONFIRM_BUCKETS,
    MARGIN_BUCKETS,
    MIN_MEASURED_ORDERS,
    RETURN_MARGIN_BUCKETS,
    _bucket_index,
    _format_duration,
    build_order_deadline_reminders,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class OrderDeadlineSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('deadline-seller')
        self.buyer = make_user('deadline-buyer')
        self.category = Category.objects.create(name='截止分类')
        self.location = CampusLocation.objects.create(name='截止地点')

    def make_item(self, *, trade_mode='sell', title='截止商品'):
        return Item.objects.create(
            title=title, description='用于截止与提醒统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location, trade_mode=trade_mode,
        )

    def make_order(self, *, status='confirmed', item=None, days_ago=1,
                   confirmation_deadline_hours=24, reminded=False):
        item = item or self.make_item()
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
            confirmation_deadline=self.now - timedelta(days=days_ago) + timedelta(
                hours=confirmation_deadline_hours,
            ),
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        if reminded:
            Order.objects.filter(pk=order.pk).update(
                confirmation_reminder_sent_at=self.now - timedelta(days=days_ago)
                + timedelta(hours=confirmation_deadline_hours - 6),
            )
            order.refresh_from_db()
        return order

    def make_confirmed_event(self, order, *, at):
        OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='pending',
            to_status='confirmed', note='卖家确认了交易预约',
        )
        OrderEvent.objects.filter(pk=OrderEvent.objects.filter(order=order).latest('id').pk).update(
            created_at=at,
        )

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_order_deadline_reminders(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['confirm_stats']['total'], 0)
        self.assertEqual(report['confirm_stats']['median_label'], '—')
        self.assertEqual(report['pending_rows'], [])
        self.assertIn('还没有订单离开', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_order_deadline_reminders(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_an_order_created_before_the_window_is_excluded(self):
        order = self.make_order(days_ago=45)
        self.make_confirmed_event(order, at=self.now - timedelta(days=44))

        report = build_order_deadline_reminders(days=30, now=self.now)
        self.assertFalse(report['has_data'])


class DenominatorTests(TestCase):
    """分母只收已经离开「待卖家确认」的订单——右删失样本不能进分母。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('denom-seller')
        self.buyer = make_user('denom-buyer')
        self.category = Category.objects.create(name='分母分类')
        self.location = CampusLocation.objects.create(name='分母地点')
        self.item = Item.objects.create(
            title='分母商品', description='用于分母测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, *, status='confirmed', item=None, days_ago=1, deadline_hours=24):
        item = item or self.item
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
            confirmation_deadline=self.now - timedelta(days=days_ago) + timedelta(
                hours=deadline_hours,
            ),
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return order

    def test_a_pending_order_stays_out_of_the_denominator(self):
        """还挂在本阶段的订单，确认还没发生，根本谈不上花了多久。"""
        order = self.make_order(status='pending', days_ago=2)
        self.assertIsNotNone(order.confirmation_deadline)

        report = build_order_deadline_reminders(days=30, now=self.now)

        self.assertEqual(report['confirm_stats']['total'], 0)
        self.assertEqual(report['pending_in_window'], 1)
        self.assertEqual(len(report['pending_rows']), 1)
        self.assertEqual(report['pending_rows'][0]['order_id'], order.id)

    def test_pending_waiting_is_not_cut_by_the_period_boundary(self):
        """超时等待从截止时刻算到现在，不按统计周期切。"""
        order = self.make_order(status='pending', days_ago=1, deadline_hours=0)
        # 截止时间就在窗口内，等待时长是 1 天，不是「从窗口起点到现在」。
        report = build_order_deadline_reminders(days=30, now=self.now)

        row = report['pending_rows'][0]
        self.assertEqual(row['order_id'], order.id)
        self.assertEqual(row['overdue_label'], '1 天')

    def test_pending_without_a_deadline_is_not_counted_as_overdue(self):
        """没有截止时间的订单谈不上超时，单独放着，不当成超时。"""
        order = self.make_order(status='pending', days_ago=1)
        Order.objects.filter(pk=order.pk).update(confirmation_deadline=None)

        report = build_order_deadline_reminders(days=30, now=self.now)
        self.assertEqual(report['pending_rows'], [])


class ConfirmDurationTests(TestCase):
    """从下单到卖家确认花了多久。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('dur-seller')
        self.buyer = make_user('dur-buyer')
        self.category = Category.objects.create(name='时长分类')
        self.location = CampusLocation.objects.create(name='时长地点')

    def make_item(self, title='时长商品'):
        return Item.objects.create(
            title=title, description='用于确认时长统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_confirmed_order(self, *, hours_after=3, days_ago=1, item=None):
        item = item or self.make_item()
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
            confirmation_deadline=self.now - timedelta(days=days_ago) + timedelta(hours=24),
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='pending',
            to_status='confirmed', note='卖家确认了交易预约',
        )
        OrderEvent.objects.filter(order=order).update(
            created_at=self.now - timedelta(days=days_ago) + timedelta(hours=hours_after),
        )
        return order

    def test_confirmation_duration_uses_the_event_timestamp_only(self):
        """`updated_at` 是 auto_now，摸它不算确认——只有事件行算。"""
        order = self.make_confirmed_order(hours_after=3)
        # 把 updated_at 摸到很晚，确认时长不该跟着变。
        Order.objects.filter(pk=order.pk).update(updated_at=self.now)

        report = build_order_deadline_reminders(days=30, now=self.now)

        self.assertEqual(report['confirm_stats']['total'], 1)
        self.assertEqual(report['confirm_stats']['median_label'], '3 小时')

    def test_an_order_without_a_confirmation_event_is_not_in_the_duration(self):
        """没有事件行的订单，平台答不了它花了多久，单独放着不猜。"""
        item = self.make_item('无事件商品')
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
            confirmation_deadline=self.now + timedelta(hours=24),
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=1),
        )

        report = build_order_deadline_reminders(days=30, now=self.now)

        self.assertTrue(report['has_data'])
        self.assertEqual(report['confirm_stats']['total'], 0)

    def test_duration_buckets_are_left_closed_right_open(self):
        """分档左闭右开：刚好卡在上界的值归下一档，不留在本档。"""
        self.assertEqual(_bucket_index(CONFIRM_BUCKETS, 0), 0)
        self.assertEqual(_bucket_index(CONFIRM_BUCKETS, 3599), 0)
        self.assertEqual(_bucket_index(CONFIRM_BUCKETS, 3600), 1)
        # 6 小时整是「1-6 小时」的上界，按左闭右开落进「当天」。
        self.assertEqual(_bucket_index(CONFIRM_BUCKETS, 6 * 3600), 2)
        self.assertEqual(_bucket_index(CONFIRM_BUCKETS, 6 * 3600 - 1), 1)

    def test_orders_are_split_across_the_hour_and_day_buckets(self):
        self.make_confirmed_order(hours_after=1, item=self.make_item('早确认'))
        self.make_confirmed_order(hours_after=20, item=self.make_item('晚确认'))

        report = build_order_deadline_reminders(days=30, now=self.now)

        counts = {row['label']: row['count'] for row in report['confirm_stats']['bucket_rows']}
        # 1 小时整是「1 小时内」的上界，按左闭右开归下一档。
        self.assertEqual(counts['1 小时内'], 0)
        self.assertEqual(counts['1-6 小时'], 1)
        self.assertEqual(counts['当天'], 1)


class ReminderEffectTests(TestCase):
    """催过之后到底有没有用。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('remind-seller')
        self.buyer = make_user('remind-buyer')
        self.category = Category.objects.create(name='提醒分类')
        self.location = CampusLocation.objects.create(name='提醒地点')
        self.counter = 0

    def make_item(self, title):
        self.counter += 1
        return Item.objects.create(
            title=title, description='用于提醒效果统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, *, status='confirmed', days_ago=1, deadline_hours=24,
                   reminded_hours_before=None, title='提醒商品'):
        item = self.make_item(title)
        created_at = self.now - timedelta(days=days_ago)
        deadline = created_at + timedelta(hours=deadline_hours)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
            confirmation_deadline=deadline,
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        if reminded_hours_before is not None:
            Order.objects.filter(pk=order.pk).update(
                confirmation_reminder_sent_at=deadline - timedelta(
                    hours=reminded_hours_before,
                ),
            )
            order.refresh_from_db()
        return order

    def add_confirmed_event(self, order, *, at):
        OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='pending',
            to_status='confirmed', note='卖家确认了交易预约',
        )
        OrderEvent.objects.filter(order=order).update(created_at=at)

    def add_auto_release_event(self, order, *, at=None):
        OrderEvent.objects.create(
            order=order, to_status='cancelled', note='卖家确认超时，系统自动释放预约',
        )
        if at is not None:
            OrderEvent.objects.filter(order=order).update(created_at=at)

    def test_reminder_effect_splits_four_outcomes(self):
        """四种结局分开报，不合成一个「已提醒 N 笔」。"""
        # 提醒之后踩线确认：只剩不到一小时。
        order_a = self.make_order(
            deadline_hours=24, reminded_hours_before=6, title='踩线确认',
        )
        self.add_confirmed_event(
            order_a,
            at=order_a.confirmation_deadline - timedelta(minutes=30),
        )
        # 提醒之后很早就确认了：提醒在截止前 6 小时，确认在截止前 4 小时，
        # 落在「1-6 小时」这一档，和上面那笔踩线的分开。
        order_b = self.make_order(
            deadline_hours=24, reminded_hours_before=6, title='提前确认',
        )
        self.add_confirmed_event(
            order_b,
            at=order_b.confirmation_deadline - timedelta(hours=4),
        )
        # 提醒之后仍被自动释放。
        order_c = self.make_order(
            status='cancelled', deadline_hours=0, reminded_hours_before=6,
            title='催了仍超时',
        )
        self.add_auto_release_event(order_c)
        # 提醒之后用户主动取消。
        order_d = self.make_order(
            status='cancelled', deadline_hours=24, reminded_hours_before=6,
            title='用户取消',
        )
        OrderEvent.objects.create(
            order=order_d, to_status='cancelled', note='订单被取消，商品恢复为在售',
        )

        report = build_order_deadline_reminders(days=30, now=self.now)
        stats = report['reminder_stats']

        self.assertEqual(stats['total'], 4)
        self.assertEqual(stats['confirmed_after'], 2)
        self.assertEqual(stats['expired_after'], 1)
        self.assertEqual(stats['cancelled_by_user'], 1)
        self.assertEqual(stats['unmeasurable'], 0)
        # 提醒之后确认的两笔各归各的档：一笔踩线，一笔提前四小时。
        counts = {row['label']: row['count'] for row in stats['margin_rows']}
        self.assertEqual(counts['1 小时内'], 1)
        self.assertEqual(counts['1-6 小时'], 1)
        self.assertEqual(counts['6-24 小时'], 0)

    def test_auto_release_and_user_cancellation_are_not_the_same_thing(self):
        """都表现为 cancelled，但只有自动释放才进「催了仍超时」。"""
        order_a = self.make_order(
            status='cancelled', deadline_hours=0, reminded_hours_before=6,
            title='自动释放',
        )
        self.add_auto_release_event(order_a)
        order_b = self.make_order(
            status='cancelled', deadline_hours=24, reminded_hours_before=6,
            title='主动取消',
        )
        OrderEvent.objects.create(
            order=order_b, to_status='cancelled', note='订单被取消，商品恢复为在售',
        )

        report = build_order_deadline_reminders(days=30, now=self.now)
        stats = report['reminder_stats']

        self.assertEqual(stats['expired_after'], 1)
        self.assertEqual(stats['cancelled_by_user'], 1)

    def test_auto_released_orders_are_listed_with_whether_they_were_nudged(self):
        # 两笔都给出 24 小时的承诺，只是都没兑现——一笔催过，一笔没有。
        order_a = self.make_order(
            status='cancelled', deadline_hours=24, reminded_hours_before=6,
            title='催过仍超时',
        )
        self.add_auto_release_event(order_a)
        order_b = self.make_order(
            status='cancelled', deadline_hours=24, title='没催过就超时',
        )
        self.add_auto_release_event(order_b)

        report = build_order_deadline_reminders(days=30, now=self.now)
        rows = report['release_rows']

        self.assertEqual(len(rows), 2)
        self.assertEqual([row['was_reminded'] for row in rows], [True, False])
        self.assertEqual(rows[0]['order_id'], order_a.id)
        # 报的是承诺本身有多长（截止 - 下单），不是它超时了多久——后者在
        # 「超时还没确认」那一列按现在算。24 小时按「天」这一档显示。
        self.assertEqual(rows[0]['waited_label'], '1 天')

    def test_an_order_reminded_outside_the_window_is_not_in_the_effect(self):
        """提醒发生在窗口之前，本周期答不了它催了之后发生了什么。"""
        order = self.make_order(
            deadline_hours=24, reminded_hours_before=6, title='窗口外提醒',
        )
        Order.objects.filter(pk=order.pk).update(
            confirmation_reminder_sent_at=self.now - timedelta(days=40),
        )

        report = build_order_deadline_reminders(days=30, now=self.now)
        self.assertEqual(report['reminder_stats']['total'], 0)

    def test_margin_buckets_cover_the_past_deadline_tail(self):
        """已过截止才确认的单独一档：释放判定是 deadline <= now，这种订单存在。

        第一档的上界写 0 而不是 ``None``——写 ``None`` 会让它变成兜底档，把所有
        正数都吸进去，剩下四档永远为空。
        """
        self.assertEqual(MARGIN_BUCKETS[0][0], 0)
        self.assertEqual(_bucket_index(MARGIN_BUCKETS, -60), 0)
        self.assertEqual(_bucket_index(MARGIN_BUCKETS, 0), 1)
        self.assertEqual(_bucket_index(MARGIN_BUCKETS, 3599), 1)
        self.assertEqual(_bucket_index(MARGIN_BUCKETS, 3 * 3600), 2)
        self.assertEqual(_bucket_index(MARGIN_BUCKETS, 12 * 3600), 3)
        self.assertEqual(_bucket_index(MARGIN_BUCKETS, 48 * 3600), 4)

    def test_the_last_bucket_is_the_only_one_without_an_upper_bound(self):
        self.assertIsNone(MARGIN_BUCKETS[-1][0])
        self.assertIsNone(RETURN_MARGIN_BUCKETS[-1][0])


class BorrowReminderTests(TestCase):
    """借用侧催过之后，归还节奏变了没有。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('borrow-seller')
        self.buyer = make_user('borrow-buyer')
        self.category = Category.objects.create(name='借用分类')
        self.location = CampusLocation.objects.create(name='借用地点')
        self.counter = 0

    def make_borrow_order(self, *, status='returned', days_ago=10, due_in_days=7,
                          returned_days_ago=None, reminded=True):
        """造一笔借用订单。

        默认 10 天前下单、7 天后到期——这样「预计归还」和那条提前 6 小时的归还
        提醒都落在统计窗口内。按 1 天前下单造会把提醒推到未来，窗口里一条都不收。
        """
        self.counter += 1
        item = Item.objects.create(
            title=f'借用商品{self.counter}', description='用于归还提醒统计',
            price=Decimal('0.00'), status='available', seller=self.seller,
            category=self.category, location=self.location, trade_mode='borrow',
            borrow_days=7,
        )
        created_at = self.now - timedelta(days=days_ago)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='0.00', status=status,
            return_due_at=created_at + timedelta(days=due_in_days),
            confirmation_deadline=created_at + timedelta(hours=24),
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        if reminded:
            Order.objects.filter(pk=order.pk).update(
                return_reminder_sent_at=created_at + timedelta(days=due_in_days) - timedelta(hours=6),
            )
            order.refresh_from_db()
        if returned_days_ago is not None:
            Order.objects.filter(pk=order.pk).update(
                returned_at=self.now - timedelta(days=returned_days_ago),
            )
            order.refresh_from_db()
        return order

    def test_borrow_reminders_report_how_early_the_item_came_back(self):
        """提前 3 天归还的落在「提前 1 天以上」，提前半天的落在「提前 1 小时内」。"""
        # 两笔都让「预计归还」落在过去：第一笔提前 3 天归还（落「提前 1 天以上」）；
        # 第二笔提前 12 小时归还——12 小时小于 1 天的上界、大于 1 小时的上界，
        # 正好落在「提前 1 天内」这一档。
        early = self.make_borrow_order(due_in_days=7)
        Order.objects.filter(pk=early.pk).update(
            returned_at=self.now - timedelta(days=6),
        )
        early.refresh_from_db()
        # 「预计归还」放在 4 小时前、12 小时前就归还了 = 提前 8 小时，落在
        # 「提前 1 天内」。归还必须早于到期，否则就变成逾期那一档了。
        late = self.make_borrow_order(days_ago=2, due_in_days=1)
        Order.objects.filter(pk=late.pk).update(
            return_due_at=self.now - timedelta(hours=4),
            returned_at=self.now - timedelta(hours=12),
        )
        late.refresh_from_db()

        report = build_order_deadline_reminders(days=30, now=self.now)
        stats = report['borrow_stats']

        self.assertEqual(stats['total'], 2)
        self.assertEqual(stats['returned'], 2)
        counts = {row['label']: row['count'] for row in stats['margin_rows']}
        self.assertEqual(counts['提前 1 天以上'], 1)
        self.assertEqual(counts['提前 1 天内'], 1)
        self.assertEqual(counts['提前 1 小时内'], 0)
        self.assertEqual(counts['逾期才归还'], 0)

    def test_an_overdue_return_lands_in_the_tail_bucket(self):
        # 10 天前下单、1 天后到期 → 9 天前就到期，现在才归还，落在尾巴档。
        self.make_borrow_order(due_in_days=1, returned_days_ago=0)

        report = build_order_deadline_reminders(days=30, now=self.now)
        counts = {
            row['label']: row['count']
            for row in report['borrow_stats']['margin_rows']
        }
        self.assertEqual(counts['逾期才归还'], 1)

    def test_a_cancelled_borrow_order_is_not_a_late_return(self):
        """取消的借用订单不是「没还」，是「交易没有继续」，单独数。"""
        self.make_borrow_order(status='cancelled', due_in_days=7, reminded=True)

        report = build_order_deadline_reminders(days=30, now=self.now)
        stats = report['borrow_stats']

        self.assertEqual(stats['total'], 1)
        self.assertEqual(stats['returned'], 0)
        self.assertEqual(stats['cancelled'], 1)

    def test_sale_orders_are_not_read_as_borrow_reminders(self):
        """借用侧只认 `Item.trade_mode == 'borrow'`，不按订单状态猜。"""
        item = Item.objects.create(
            title='出售商品', description='不是借用', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='returned',
            confirmation_deadline=self.now - timedelta(hours=20),
            return_due_at=self.now - timedelta(days=1),
            returned_at=self.now,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=8),
            return_reminder_sent_at=self.now - timedelta(days=1),
        )

        report = build_order_deadline_reminders(days=30, now=self.now)

        self.assertEqual(report['borrow_stats']['total'], 0)

    def test_return_margin_buckets_cover_the_overdue_tail(self):
        self.assertEqual(RETURN_MARGIN_BUCKETS[0][0], 0)
        self.assertEqual(_bucket_index(RETURN_MARGIN_BUCKETS, -3600), 0)
        self.assertEqual(_bucket_index(RETURN_MARGIN_BUCKETS, 0), 1)
        self.assertEqual(_bucket_index(RETURN_MARGIN_BUCKETS, 3599), 1)
        self.assertEqual(_bucket_index(RETURN_MARGIN_BUCKETS, 3600), 2)
        self.assertEqual(_bucket_index(RETURN_MARGIN_BUCKETS, 12 * 3600), 2)
        self.assertEqual(_bucket_index(RETURN_MARGIN_BUCKETS, 86400), 3)


class LedgerSplitTests(TestCase):
    """台账把两件事加成的一个数，在看板上拆开。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('ledger-seller')
        self.buyer = make_user('ledger-buyer')
        self.category = Category.objects.create(name='台账分类')
        self.location = CampusLocation.objects.create(name='台账地点')

    def test_confirmation_and_borrow_reminders_are_counted_separately(self):
        sale_item = Item.objects.create(
            title='台账出售', description='确认提醒', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )
        created_at = self.now - timedelta(days=1)
        # 借用那笔必须造得更早：它的归还提醒落在「预计归还」前 6 小时，
        # 1 天前下单会把提醒推到窗口外面去。
        borrow_created_at = self.now - timedelta(days=10)
        sale_order = Order.objects.create(
            item=sale_item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
            confirmation_deadline=created_at + timedelta(hours=24),
        )
        Order.objects.filter(pk=sale_order.pk).update(
            created_at=created_at,
            confirmation_reminder_sent_at=created_at + timedelta(hours=18),
        )

        borrow_item = Item.objects.create(
            title='台账借用', description='归还提醒', price=Decimal('0.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location, trade_mode='borrow', borrow_days=7,
        )
        borrow_order = Order.objects.create(
            item=borrow_item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='0.00', status='returned',
            return_due_at=borrow_created_at + timedelta(days=7),
            returned_at=borrow_created_at + timedelta(days=5),
            confirmation_deadline=borrow_created_at + timedelta(hours=24),
        )
        Order.objects.filter(pk=borrow_order.pk).update(
            created_at=borrow_created_at,
            return_reminder_sent_at=borrow_created_at + timedelta(days=6, hours=18),
        )
        borrow_order.refresh_from_db()
        self.assertIsNotNone(borrow_order.return_reminder_sent_at)
        self.assertLessEqual(borrow_order.return_reminder_sent_at, self.now)

        report = build_order_deadline_reminders(days=30, now=self.now)

        self.assertEqual(report['confirmation_reminded_in_window'], 1)
        self.assertEqual(report['borrow_reminded_in_window'], 1)

    def test_the_reported_defaults_match_the_writer(self):
        """看板报出的常量必须和写入方一致，否则运营读到的承诺是另一个数。"""
        report = build_order_deadline_reminders(days=30, now=self.now)

        self.assertEqual(report['confirmation_timeout_hours'], 24)
        self.assertEqual(report['reminder_hours_default'], 6)
        self.assertEqual(report['min_measured_orders'], MIN_MEASURED_ORDERS)


class ReportOnlyTests(TestCase):
    """本模块只报告，不改写任何字段。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('ro-seller')
        self.buyer = make_user('ro-buyer')
        self.category = Category.objects.create(name='只报分类')
        self.location = CampusLocation.objects.create(name='只报地点')

    def test_building_the_report_changes_nothing(self):
        item = Item.objects.create(
            title='只报商品', description='确认前后一致', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )
        created_at = self.now - timedelta(days=1)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='pending',
            confirmation_deadline=created_at + timedelta(hours=24),
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        before = Order.objects.get(pk=order.pk)

        build_order_deadline_reminders(days=30, now=self.now)

        after = Order.objects.get(pk=order.pk)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.confirmation_deadline, before.confirmation_deadline)
        self.assertIsNone(after.confirmation_reminder_sent_at)
        self.assertEqual(OrderEvent.objects.count(), 0)


class ExportShapeTests(TestCase):
    """看板导出用到的键都在，空态不炸。"""

    def setUp(self):
        self.now = local_noon()

    def test_an_empty_report_still_exposes_every_export_key(self):
        report = build_order_deadline_reminders(days=30, now=self.now)

        for key in (
            'summary', 'confirm_stats', 'reminder_stats', 'release_rows',
            'borrow_stats', 'pending_rows', 'pending_in_window', 'pending_count',
            'confirmation_reminded_in_window', 'borrow_reminded_in_window',
            'confirmation_timeout_hours', 'reminder_hours_default',
        ):
            self.assertIn(key, report)
        self.assertIsNone(report['confirm_stats']['bucket_rows'][0]['share'])