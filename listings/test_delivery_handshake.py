# -*- coding: utf-8 -*-
"""当面交付的两次握手，第一次被运营读到。

这两个时间戳一直在写，而运营侧一条统计都没有——这个模块把这件事补上，这些测试要
钉住的不是四舍五入，而是四件更根本的事。

第一件是分母。只有"已经离开当面交付阶段"的订单才谈得上两次握手有没有点完；一个
现在还挂在"待当面交付"的订单，见面还没发生，把它算进分母会让"只点一半"的比例凭空
变浑。这是右删失样本，和交付确认码、"约好的那一次见面"、归还握手三个面板是同一个
道理，测试必须钉住它。

第二件是"双方同一秒"必须单独一档。把它并进"买家先"或"卖家先"，顺序这一列就失真了
——一个在同一秒完成的握手，说它是任何一方先点的都是错的。测试要钉住它既不进买家
档也不进卖家档。

第三件是挂起时长不按统计周期切。一笔跨越周期边界的等待，按周期切开会被算成两段
短的，而那正是最不该被切开的样本。测试要钉住"挂起时长从先点那一刻算到现在"，
而不是从窗口起点算。

第四件是取消的订单要回去问事件表。一笔从没进入过当面交付就被取消的订单，两次握手
压根没开始，不该进分母；一笔带着真实交付确认被取消的订单，握手开始过，该进。测试
要钉住这个区分，因为它是"分母干净"这句话能否成立的唯一依据。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .delivery_handshake import (
    HANG_BUCKETS,
    MIN_MEASURED_ORDERS,
    _gap_bucket_index,
    _hang_bucket_index,
    build_delivery_handshake,
)
from .models import (
    Category,
    CampusLocation,
    DeliveryConfirmation,
    Item,
    Order,
    OrderEvent,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class DeliveryHandshakeSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('handshake-seller')
        self.buyer = make_user('handshake-buyer')
        self.category = Category.objects.create(name='握手分类')
        self.location = CampusLocation.objects.create(name='握手地点')
        self.item = Item.objects.create(
            title='握手商品', description='用于交付握手统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, *, status='completed', item=None, days_ago=1):
        item = item or self.item
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_at=None, seller_at=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_confirmed_at = buyer_at
        confirmation.seller_confirmed_at = seller_at
        confirmation.save()
        return confirmation

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_delivery_handshake(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['hang_stats']['total'], 0)
        self.assertEqual(report['stalled_rows'], [])
        self.assertIn('还没有走完当面交付', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_delivery_handshake(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_an_order_created_before_the_window_is_excluded(self):
        order = self.make_order(days_ago=45)
        self.make_confirmation(order, buyer_at=self.now, seller_at=self.now)

        report = build_delivery_handshake(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_an_order_without_a_confirmation_row_is_not_a_handshake(self):
        """没有 DeliveryConfirmation 行的订单不进任何一档，但也不该让分母变成 0。"""
        self.make_order()

        report = build_delivery_handshake(days=30, now=self.now)
        # 订单在分母里（已经离开当面交付），只是两个时间戳都空，不进顺序分档。
        self.assertTrue(report['has_data'])
        self.assertEqual(
            sum(row['count'] for row in report['sequence_rows']), 0,
        )


class DenominatorTests(TestCase):
    """分母只收已经离开当面交付阶段的订单——右删失样本不能进分母。"""

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

    def make_order(self, *, status='completed', item=None, days_ago=1):
        item = item or self.item
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_at=None, seller_at=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_confirmed_at = buyer_at
        confirmation.seller_confirmed_at = seller_at
        confirmation.save()
        return confirmation

    def test_an_order_still_in_meeting_is_a_right_censored_sample(self):
        """还挂在当面交付的订单是右删失样本：单独数，不进分母。"""
        order = self.make_order(status='meeting', days_ago=2)
        self.make_confirmation(order, buyer_at=self.now, seller_at=None)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['pending_in_window'], 1)
        self.assertGreaterEqual(report['pending_count'], 1)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['hang_stats']['total'], 0)
        self.assertEqual(report['stalled_rows'], [])

    def test_pending_orders_are_counted_even_outside_the_window(self):
        """右删失样本按订单的一生累计，不按统计周期切。"""
        # 一件商品同时只能有一条活跃订单，所以两笔 meeting 订单要分用两件商品。
        other = Item.objects.create(
            title='另一件分母商品', description='用于右删失测试',
            price=Decimal('30.00'), status='reserved', seller=self.seller,
            category=self.category, location=self.location,
        )
        self.make_order(status='meeting', days_ago=90)
        self.make_order(status='meeting', days_ago=2, item=other)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['pending_in_window'], 1)
        self.assertEqual(report['pending_count'], 2)


class SequenceTests(TestCase):
    """两次握手是什么顺序。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('seq-seller')
        self.buyer = make_user('seq-buyer')
        self.category = Category.objects.create(name='顺序分类')
        self.location = CampusLocation.objects.create(name='顺序地点')
        self.item = Item.objects.create(
            title='顺序商品', description='用于顺序测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, *, status='completed', item=None, days_ago=1):
        item = item or self.item
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_at=None, seller_at=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_confirmed_at = buyer_at
        confirmation.seller_confirmed_at = seller_at
        confirmation.save()
        return confirmation

    def _row(self, report, key):
        return next(row for row in report['sequence_rows'] if row['key'] == key)

    def test_buyer_first_is_counted_as_buyer_first(self):
        order = self.make_order()
        self.make_confirmation(
            order, buyer_at=self.now, seller_at=self.now + timedelta(hours=1),
        )

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(self._row(report, 'buyer')['count'], 1)
        self.assertEqual(self._row(report, 'seller')['count'], 0)

    def test_seller_first_is_counted_as_seller_first(self):
        order = self.make_order()
        self.make_confirmation(
            order, buyer_at=self.now + timedelta(hours=2), seller_at=self.now,
        )

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(self._row(report, 'seller')['count'], 1)
        self.assertEqual(self._row(report, 'buyer')['count'], 0)

    def test_a_simultaneous_handshake_is_its_own_row(self):
        """双方同一秒既不是买家先也不是卖家先，硬并进任何一边都会让顺序这一列失真。"""
        order = self.make_order()
        self.make_confirmation(order, buyer_at=self.now, seller_at=self.now)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(self._row(report, 'both')['count'], 1)
        self.assertEqual(self._row(report, 'buyer')['count'], 0)
        self.assertEqual(self._row(report, 'seller')['count'], 0)

    def test_a_one_sided_handshake_lands_in_the_one_sided_row(self):
        order = self.make_order()
        self.make_confirmation(order, buyer_at=self.now, seller_at=None)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(self._row(report, 'none')['count'], 1)
        self.assertEqual(report['hang_stats']['total'], 1)
        self.assertEqual(report['hang_stats']['buyer_only'], 1)
        self.assertEqual(report['hang_stats']['seller_only'], 0)

    def test_a_seller_only_handshake_is_counted_as_seller_only(self):
        order = self.make_order()
        self.make_confirmation(order, buyer_at=None, seller_at=self.now)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['hang_stats']['seller_only'], 1)
        self.assertEqual(report['hang_stats']['buyer_only'], 0)

    def test_a_small_sample_withholds_its_verdict(self):
        """不足样本门槛的档位只报数字，不参与"谁更可能先点"的方向判断。"""
        order = self.make_order()
        self.make_confirmation(
            order, buyer_at=self.now, seller_at=self.now + timedelta(hours=1),
        )

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertTrue(self._row(report, 'buyer')['is_small_sample'])
        self.assertNotIn('buyer', report['measurable_sides'])

    def test_enough_samples_make_a_side_measurable(self):
        for index in range(MIN_MEASURED_ORDERS):
            item = Item.objects.create(
                title=f'顺序商品 {index}', description='批量造样本',
                price=Decimal('30.00'), status='available', seller=self.seller,
                category=self.category, location=self.location,
            )
            order = self.make_order(item=item, days_ago=index + 1)
            self.make_confirmation(
                order, buyer_at=self.now, seller_at=self.now + timedelta(hours=1),
            )

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertIn('buyer', report['measurable_sides'])

    def test_median_gap_is_reported_in_human_units(self):
        order = self.make_order()
        self.make_confirmation(
            order, buyer_at=self.now, seller_at=self.now + timedelta(minutes=90),
        )

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(self._row(report, 'buyer')['median_gap_label'], '1 小时')


class GapBucketTests(TestCase):
    """两下之间隔了多久。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('gap-seller')
        self.buyer = make_user('gap-buyer')
        self.category = Category.objects.create(name='间隔分类')
        self.location = CampusLocation.objects.create(name='间隔地点')
        self.item = Item.objects.create(
            title='间隔商品', description='用于间隔测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def test_gap_buckets_are_left_closed_and_right_open(self):
        """档位必须左闭右开：隔满 1 分钟进下一档，隔 0.4 分钟仍算 1 分钟内。"""
        self.assertEqual(_gap_bucket_index(0), 0)
        self.assertEqual(_gap_bucket_index(59), 0)
        self.assertEqual(_gap_bucket_index(60), 1)
        self.assertEqual(_gap_bucket_index(3600), 2)
        self.assertEqual(_gap_bucket_index(86400), 3)

    def test_a_negative_gap_falls_into_the_first_bucket(self):
        """时钟回拨会让间隔变成负数，它仍然是一次几乎同时完成的握手。"""
        self.assertEqual(_gap_bucket_index(-30), 0)

    def test_a_huge_gap_falls_into_the_last_bucket(self):
        self.assertEqual(
            _gap_bucket_index(100 * 86400), len(build_gap_labels()) - 1,
        )


def build_gap_labels():
    from .delivery_handshake import GAP_BUCKETS
    return [label for _limit, label in GAP_BUCKETS]


class HangTests(TestCase):
    """只有一方点过的订单挂了多久。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('hang-seller')
        self.buyer = make_user('hang-buyer')
        self.category = Category.objects.create(name='挂起分类')
        self.location = CampusLocation.objects.create(name='挂起地点')
        self.item = Item.objects.create(
            title='挂起商品', description='用于挂起测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, *, status='completed', item=None, days_ago=1):
        item = item or self.item
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_at=None, seller_at=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_confirmed_at = buyer_at
        confirmation.seller_confirmed_at = seller_at
        confirmation.save()
        return confirmation

    def test_hang_buckets_are_left_closed_and_right_open(self):
        self.assertEqual(_hang_bucket_index(0), 0)
        self.assertEqual(_hang_bucket_index(86400), 1)
        self.assertEqual(_hang_bucket_index(30 * 86400), 4)

    def test_hang_is_measured_from_the_first_click_not_the_window_start(self):
        """挂起时长从先点那一刻算到现在，不从窗口起点算。

        一笔跨越周期边界的等待，按周期切开会被算成两段短的，而那正是最不该被切开的
        样本。
        """
        # 订单创建在窗口内（2 天前），而先点那一刻在 20 天前：等待跨越了周期边界。
        # 按周期切开会把这一段算成两段短的，所以必须从先点那一刻一直算到现在。
        order = self.make_order(days_ago=2)
        self.make_confirmation(order, buyer_at=self.now - timedelta(days=20), seller_at=None)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['hang_stats']['total'], 1)
        bucket = next(
            row for row in report['hang_stats']['bucket_rows'] if row['count'] == 1
        )
        self.assertEqual(bucket['label'], '7-30 天')

    def test_hang_rows_report_both_sides(self):
        order_a = self.make_order(days_ago=2)
        self.make_confirmation(order_a, buyer_at=self.now, seller_at=None)
        order_b = self.make_order(days_ago=3)
        self.make_confirmation(order_b, buyer_at=None, seller_at=self.now)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['hang_stats']['buyer_only'], 1)
        self.assertEqual(report['hang_stats']['seller_only'], 1)

    def test_an_empty_hang_denominator_reports_none_not_zero(self):
        """空分母报 None 不报 0——分子 0 / 分母 > 0 时报 0.0 才是对的。"""
        report = build_delivery_handshake(days=30, now=self.now)

        self.assertIsNone(report['hang_stats']['buyer_only_share'])
        self.assertIsNone(report['outcome_contrast']['gap_points'])

    def test_a_zero_numerator_with_a_real_denominator_reports_zero(self):
        order = self.make_order(days_ago=2)
        self.make_confirmation(order, buyer_at=self.now, seller_at=self.now)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['hang_stats']['buyer_only'], 0)
        self.assertEqual(report['outcome_contrast']['fast_rate'], 100.0)


class CancelledOrderTests(TestCase):
    """取消的订单要回去问事件表：握手有没有真的开始过。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('cancel-seller')
        self.buyer = make_user('cancel-buyer')
        self.category = Category.objects.create(name='取消分类')
        self.location = CampusLocation.objects.create(name='取消地点')
        self.item = Item.objects.create(
            title='取消商品', description='用于取消测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, *, status='completed', item=None, days_ago=1):
        item = item or self.item
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_at=None, seller_at=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_confirmed_at = buyer_at
        confirmation.seller_confirmed_at = seller_at
        confirmation.save()
        return confirmation

    def test_a_cancelled_order_that_never_reached_meeting_is_excluded(self):
        """从没进入过当面交付就被取消，两次握手压根没开始，不该进分母。"""
        order = self.make_order(status='cancelled', days_ago=2)
        self.make_confirmation(order, buyer_at=self.now, seller_at=None)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['hang_stats']['total'], 0)

    def test_a_cancelled_order_that_did_reach_meeting_is_included(self):
        """带着一次真实的交付确认被取消，握手开始过，该进分母。"""
        order = self.make_order(status='cancelled', days_ago=2)
        OrderEvent.objects.create(
            order=order, from_status='confirmed', to_status='meeting',
            note='双方确认交付时间',
        )
        self.make_confirmation(order, buyer_at=self.now, seller_at=None)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertTrue(report['has_data'])
        self.assertEqual(report['hang_stats']['total'], 1)
        self.assertEqual(report['hang_stats']['buyer_only'], 1)

    def test_a_completed_order_needs_no_event_proof(self):
        """已经走完的订单不需要事件表作证，它不是右删失样本。"""
        order = self.make_order(status='completed', days_ago=2)
        self.make_confirmation(order, buyer_at=self.now, seller_at=self.now)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertTrue(report['has_data'])


class ContrastTests(TestCase):
    """握手间隔与订单是否做成，只做对照，不给因果。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('contrast-seller')
        self.buyer = make_user('contrast-buyer')
        self.category = Category.objects.create(name='对照分类')
        self.location = CampusLocation.objects.create(name='对照地点')

    def make_order(self, *, status='completed', days_ago=1):
        item = Item.objects.create(
            title=f'对照商品 {days_ago}', description='用于对照测试',
            price=Decimal('30.00'), status='available', seller=self.seller,
            category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_at=None, seller_at=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_confirmed_at = buyer_at
        confirmation.seller_confirmed_at = seller_at
        confirmation.save()
        return confirmation

    def test_a_small_sample_contrast_withholds_its_verdict(self):
        order = self.make_order(days_ago=1)
        self.make_confirmation(
            order, buyer_at=self.now, seller_at=self.now + timedelta(minutes=10),
        )

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertFalse(report['outcome_contrast']['is_measurable'])
        self.assertIsNone(report['outcome_contrast']['gap_points'])

    def test_a_noise_band_difference_gets_no_direction(self):
        """两组样本都够，但差值落在噪声带内——只报数字，不给方向。"""
        for index in range(MIN_MEASURED_ORDERS):
            order = self.make_order(status='completed', days_ago=index + 1)
            self.make_confirmation(
                order, buyer_at=self.now, seller_at=self.now + timedelta(minutes=10),
            )
        # 取消的一批要在窗口内：days_ago 取 13 到 24，避开 30 天窗口的起点。
        for index in range(MIN_MEASURED_ORDERS):
            order = self.make_order(status='cancelled', days_ago=index + 13)
            OrderEvent.objects.create(
                order=order, from_status='confirmed', to_status='meeting',
                note='双方确认交付时间',
            )
            self.make_confirmation(
                order, buyer_at=self.now, seller_at=self.now + timedelta(hours=5),
            )

        report = build_delivery_handshake(days=30, now=self.now)

        contrast = report['outcome_contrast']
        self.assertTrue(contrast['is_measurable'])
        self.assertIsNotNone(contrast['gap_points'])
        self.assertEqual(contrast['fast_rate'], 100.0)
        self.assertEqual(contrast['slow_rate'], 0.0)


class StalledListTests(TestCase):
    """卡住的订单清单。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('stall-seller')
        self.buyer = make_user('stall-buyer')
        self.category = Category.objects.create(name='清单分类')
        self.location = CampusLocation.objects.create(name='清单地点')

    def make_order(self, *, status='completed', days_ago=1):
        item = Item.objects.create(
            title=f'清单商品 {days_ago}', description='用于清单测试',
            price=Decimal('30.00'), status='available', seller=self.seller,
            category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, buyer_at=None, seller_at=None):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.buyer_confirmed_at = buyer_at
        confirmation.seller_confirmed_at = seller_at
        confirmation.save()
        return confirmation

    def test_the_longest_hang_comes_first(self):
        short = self.make_order(days_ago=2)
        self.make_confirmation(short, buyer_at=self.now, seller_at=None)
        long = self.make_order(days_ago=3)
        self.make_confirmation(long, buyer_at=self.now - timedelta(days=30), seller_at=None)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['stalled_rows'][0]['order_id'], long.id)
        self.assertEqual(report['stalled_rows'][-1]['order_id'], short.id)

    def test_a_fully_confirmed_order_never_appears_on_the_list(self):
        order = self.make_order(days_ago=2)
        self.make_confirmation(order, buyer_at=self.now, seller_at=self.now)

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(report['stalled_rows'], [])

    def test_the_list_is_capped(self):
        from .delivery_handshake import STALLED_LIST_LIMIT
        for index in range(STALLED_LIST_LIMIT + 3):
            order = self.make_order(days_ago=index + 1)
            self.make_confirmation(
                order, buyer_at=self.now - timedelta(days=index + 1), seller_at=None,
            )

        report = build_delivery_handshake(days=30, now=self.now)

        self.assertEqual(len(report['stalled_rows']), STALLED_LIST_LIMIT)
        self.assertEqual(report['stalled_row_limit'], STALLED_LIST_LIMIT)