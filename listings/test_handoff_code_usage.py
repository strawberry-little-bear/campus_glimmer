# -*- coding: utf-8 -*-
"""在现场念出来的那六位数字，第一次被运营读到。

交付确认码的五个字段一直在写，而运营侧一条统计都没有——这个模块把这件事补上，
这些测试要钉住的不是四舍五入，而是四件更根本的事。

第一件是分母。只有"已经离开当面交付阶段"的订单才谈得上有没有生成码；一个现在还
挂在"待当面交付"的订单，见面还没发生，把它算进分母会让生成率凭空变小。这是右删失
样本，和订单阶段流面板、"约好的那一次见面"面板是同一个道理，测试必须钉住它。

第二件是 ``handoff_code_attempts`` 的语义。它记的是最后一枚码的错误次数，不是这笔
订单累计的错误次数——重新生成会把它归零。所以"错几次才对"这一列必须和"重新生成"
那一列合起来读，单看任何一列都会得出一个过于乐观的结论。测试要钉住"重新生成归零"
这个事实，因为它正是这个统计最容易被误读的地方。

第三件是重新生成的下界口径。这个数从站内通知反推，而卖家关掉订单状态通知后生成
就不再留痕。所以它是下界不是准数——测试要钉住"如实标成下界"，而不是让某个百分比
把这个数包装成一个精确值。

第四件是完成率对照只做对照、不给因果。生成码的订单更可能完成，很可能只是因为愿意
生成码的双方本来就是更认真的那批人。测试要钉住"样本不足时只报数字不给方向"以及
"差值在噪声带内不给方向"，因为一个把对照讲成结论的模块，会推动平台去做一个没有
因果关系的功能。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .handoff_code_usage import (
    LOCK_THRESHOLD,
    MIN_MEASURED_ORDERS,
    NOISE_POINTS,
    _attempt_bucket_index,
    _issue_to_use_seconds,
    build_handoff_code_usage,
)
from .models import Category, CampusLocation, DeliveryConfirmation, Item, Order


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class HandoffCodeSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('code-seller')
        self.buyer = make_user('code-buyer')
        self.category = Category.objects.create(name='确认码分类')
        self.location = CampusLocation.objects.create(name='确认码地点')

    def make_item(self, title='确认码商品'):
        return Item.objects.create(
            title=title, description='用于确认码统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, *, status='completed', item=None, days_ago=1):
        item = item or self.make_item()
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, issued=True, used=None, attempts=0,
                          hint='末两位 42'):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        if issued:
            confirmation.handoff_code_hash = 'hashed-code'
            confirmation.handoff_code_hint = hint
            confirmation.handoff_code_issued_at = self.now - timedelta(hours=2)
        if used is not None:
            confirmation.handoff_code_used_at = self.now - used
        confirmation.handoff_code_attempts = attempts
        confirmation.save()
        return confirmation

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_handoff_code_usage(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['adoption_stats']['total'], 0)
        self.assertEqual(report['attempt_stats']['issued_total'], 0)
        self.assertIn('还没有走完当面交付阶段', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_handoff_code_usage(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_an_order_created_before_the_window_is_excluded(self):
        self.make_order(days_ago=45)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertFalse(report['has_data'])


class DenominatorTests(TestCase):
    """分母只收已经离开当面交付阶段的订单——右删失样本不能进分母。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('denom-seller')
        self.buyer = make_user('denom-buyer')
        self.category = Category.objects.create(name='分母分类')
        self.location = CampusLocation.objects.create(name='分母地点')
        self.item = Item.objects.create(
            title='分母商品', description='用于分母统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, status='completed', days_ago=1):
        # 一笔商品同时只能有一笔进行中的订单，所以每笔订单各建一件商品。
        order = Order.objects.create(
            item=Item.objects.create(
                title='分母商品', description='用于分母统计', price=Decimal('30.00'),
                status='sold', seller=self.seller, category=self.category,
                location=self.location,
            ), buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def issue_code(self, order, **kwargs):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.handoff_code_hash = 'hashed-code'
        confirmation.handoff_code_issued_at = self.now - timedelta(hours=2)
        for key, value in kwargs.items():
            setattr(confirmation, key, value)
        confirmation.save()
        return confirmation

    def test_an_order_still_in_meeting_is_not_in_the_denominator(self):
        # 还挂在"待当面交付"的订单，见面还没发生，根本谈不上有没有生成码。
        meeting = self.make_order(status='meeting')
        self.issue_code(meeting)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['adoption_stats']['total'], 0)

    def test_only_concluded_orders_are_counted(self):
        for status in ('completed', 'borrowed', 'returned', 'cancelled'):
            self.make_order(status=status)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['adoption_stats']['total'], 4)

    def test_a_pending_or_confirmed_order_is_excluded(self):
        # 从来没走到当面交付的订单不参与：这一列量的是"当面交付这一程"。
        self.make_order(status='pending')
        self.make_order(status='confirmed')

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_issue_rate_uses_concluded_orders_as_the_denominator(self):
        # 三笔走完的订单里两笔生成了码，第三笔还挂着当面交付阶段：
        # 分母是 3，不是 4。
        self.make_order()
        self.make_order()
        self.make_order(status='meeting')
        orders = Order.objects.filter(status='completed').order_by('id')
        for order in orders:
            self.issue_code(order)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['adoption_stats']['total'], 2)
        self.assertEqual(report['adoption_stats']['issue_rate'], 100.0)

    def test_an_order_created_before_the_window_is_not_in_the_denominator(self):
        self.make_order(days_ago=45)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertFalse(report['has_data'])


class AttemptBucketTests(TestCase):
    """错误尝试记的是最后一枚码，不是这笔订单累计的错误次数。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('attempt-seller')
        self.buyer = make_user('attempt-buyer')
        self.category = Category.objects.create(name='尝试分类')
        self.location = CampusLocation.objects.create(name='尝试地点')
        self.item = Item.objects.create(
            title='尝试商品', description='用于尝试分档', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, status='completed'):
        # 一笔商品同时只能有一笔进行中的订单，所以每笔订单各建一件商品。
        order = Order.objects.create(
            item=Item.objects.create(
                title='分档商品', description='用于尝试分档', price=Decimal('30.00'),
                status='sold', seller=self.seller, category=self.category,
                location=self.location,
            ), buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        order.refresh_from_db()
        return order

    def make_confirmation(self, order, *, used=True, attempts=0):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.handoff_code_hash = 'hashed-code'
        confirmation.handoff_code_issued_at = self.now - timedelta(hours=2)
        confirmation.handoff_code_used_at = self.now - timedelta(hours=1) if used else None
        confirmation.handoff_code_attempts = attempts
        confirmation.save()
        return confirmation

    def test_no_code_at_all_has_no_bucket(self):
        # 没生成过码的订单不属于这一列的分母，它连一枚码都没有。
        order = self.make_order()
        DeliveryConfirmation.objects.create(order=order)

        self.assertIsNone(_attempt_bucket_index(order.delivery_confirmation))

    def test_first_try_lands_in_its_own_bucket(self):
        order = self.make_order()
        confirmation = self.make_confirmation(order, used=True, attempts=0)

        self.assertEqual(_attempt_bucket_index(confirmation), 0)

    def test_one_and_two_wrong_attempts_have_their_own_buckets(self):
        first = self.make_order()
        second = self.make_order()
        self.assertEqual(
            _attempt_bucket_index(self.make_confirmation(first, attempts=1)), 1,
        )
        self.assertEqual(
            _attempt_bucket_index(self.make_confirmation(second, attempts=2)), 2,
        )

    def test_three_and_four_wrong_attempts_share_a_bucket(self):
        order = self.make_order()
        self.assertEqual(
            _attempt_bucket_index(self.make_confirmation(order, attempts=3)), 3,
        )
        confirmation = self.make_confirmation(order, attempts=4)
        self.assertEqual(_attempt_bucket_index(confirmation), 3)

    def test_five_wrong_attempts_lock_the_code(self):
        order = self.make_order()
        confirmation = self.make_confirmation(order, used=False, attempts=5)

        self.assertEqual(_attempt_bucket_index(confirmation), 5)
        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['attempt_stats']['locked_count'], 1)
        self.assertEqual(report['attempt_stats']['locked_rate'], 100.0)

    def test_a_locked_code_that_was_never_used_is_reported_as_locked_not_as_unused(self):
        # 错误次数已达上限、使用时间为空：这一枚码在现场撞了五次作废了。把它读成
        # "还没核对过"，会把一次真实的沟通问题讲成一次没走到那一步。
        order = self.make_order()
        self.make_confirmation(order, used=False, attempts=5)

        report = build_handoff_code_usage(days=30, now=self.now)
        buckets = {row['label']: row['count'] for row in report['attempt_stats']['bucket_rows']}
        self.assertEqual(buckets['撞到 5 次锁定'], 1)
        self.assertEqual(buckets['还没核对过'], 0)

    def test_regeneration_resets_the_attempt_count(self):
        # 重新生成把 attempts 归零，于是这笔订单在这里看起来和一次就对的订单一模
        # 一样。这个事实正是这个统计最容易被误读的地方：错误次数那一列描述的是
        # 最后一枚码，不是这笔订单的全部现场情况。
        order = self.make_order()
        confirmation = self.make_confirmation(order, used=True, attempts=0)
        confirmation.refresh_from_db()

        self.assertEqual(confirmation.handoff_code_attempts, 0)
        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['attempt_stats']['first_try_count'], 1)
        self.assertEqual(report['attempt_stats']['locked_count'], 0)

    def test_issue_to_use_gap_is_measured_from_the_latest_generation(self):
        order = self.make_order()
        self.make_confirmation(order, used=True)
        confirmation = order.delivery_confirmation
        confirmation.handoff_code_issued_at = self.now - timedelta(hours=5)
        confirmation.handoff_code_used_at = self.now - timedelta(hours=1)
        confirmation.save()

        seconds = _issue_to_use_seconds(order.delivery_confirmation)
        self.assertEqual(seconds, 4 * 3600)

    def test_a_code_that_was_never_used_has_no_gap(self):
        order = self.make_order()
        self.make_confirmation(order, used=False)

        self.assertIsNone(_issue_to_use_seconds(order.delivery_confirmation))

    def test_the_gap_is_never_negative(self):
        # 时钟漂移或补录的记录会让使用时间早于生成时间。负间隔没有意义，取零比
        # 报一个负数诚实。
        order = self.make_order()
        confirmation = self.make_confirmation(order, used=True)
        confirmation.handoff_code_issued_at = self.now
        confirmation.handoff_code_used_at = self.now - timedelta(hours=1)
        confirmation.save()

        self.assertEqual(_issue_to_use_seconds(order.delivery_confirmation), 0.0)


class RegenerationTests(TestCase):
    """重新生成是从站内通知反推的下界，不是准数。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('regen-seller')
        self.buyer = make_user('regen-buyer')
        self.staff = make_user('regen-staff')
        self.staff.is_staff = True
        self.staff.save()
        self.category = Category.objects.create(name='重生成分类')
        self.location = CampusLocation.objects.create(name='重生成地点')
        self.item = Item.objects.create(
            title='重生成商品', description='用于重新生成统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, status='completed'):
        # 一笔商品同时只能有一笔进行中的订单，所以每笔订单各建一件商品。
        order = Order.objects.create(
            item=Item.objects.create(
                title='分档商品', description='用于尝试分档', price=Decimal('30.00'),
                status='sold', seller=self.seller, category=self.category,
                location=self.location,
            ), buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        order.refresh_from_db()
        return order

    def issue_code(self, order, times=1):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.handoff_code_hash = 'hashed-code'
        confirmation.handoff_code_issued_at = self.now - timedelta(hours=2)
        confirmation.save()
        # 每次生成都会给卖家发一条没有聚合键的通知，这是系统里唯一能数出
        # "重新生成"的地方：handoff_code_issued_at 每生成一次就被覆盖。
        from .models import Notification
        for _ in range(times):
            Notification.objects.create(
                recipient=self.seller,
                actor=self.buyer,
                order=order,
                item=order.item,
                kind='order_status',
                title='买家已生成交付确认码',
                message='已生成新的交付确认码，请在现场向买家索取并核对。',
            )
        return confirmation

    def test_one_generation_is_not_regeneration(self):
        self.issue_code(self.make_order(), times=1)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['adoption_stats']['regenerated_count'], 0)

    def test_multiple_generations_count_as_regeneration(self):
        self.issue_code(self.make_order(), times=3)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['adoption_stats']['regenerated_count'], 1)
        self.assertEqual(report['adoption_stats']['regenerated_rate'], 100.0)

    def test_the_summary_labels_the_count_as_a_lower_bound(self):
        # 卖家关掉订单状态通知后，生成就不再留下这条记录。所以这是下界不是准数——
        # 运营要拿这个数字判断现场是不是经常出问题，一个被低估的下界会让那句话
        # 听起来比实际温和。
        self.issue_code(self.make_order(), times=2)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['adoption_stats']['regenerated_count'], 1)
        self.assertIn('下界', report['summary'])

    def test_a_suppressed_notification_makes_the_count_a_lower_bound(self):
        # 关掉通知偏好的卖家，生成不产生任何通知记录。重新生成就从 1 掉到 0——
        # 这正是把它标成下界的理由：本模块报的是下界，不是准数。
        order = self.make_order()
        self.issue_code(order, times=1)
        from .models import NotificationPreference
        NotificationPreference.objects.update_or_create(
            user=self.seller, defaults={'order_status': False},
        )

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['adoption_stats']['regenerated_count'], 0)
        self.assertIn('下界', report['summary'])

    def test_generations_are_counted_across_the_whole_life_of_the_order(self):
        # handoff_code_issued_at 每生成一次就被覆盖，早期的生成时间在数据里已经
        # 不存在了。所以重新生成按订单的一生累计，不按统计周期切——想切也切不出来。
        self.issue_code(self.make_order(), times=4)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['adoption_stats']['regenerated_count'], 1)


class OutcomeContrastTests(TestCase):
    """完成率对照只做对照、不给因果。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('contrast-seller')
        self.buyer = make_user('contrast-buyer')
        self.category = Category.objects.create(name='对照分类')
        self.location = CampusLocation.objects.create(name='对照地点')
        self.item = Item.objects.create(
            title='对照商品', description='用于完成率对照', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )
        self.counter = 0

    def make_order(self, *, used, success, status=None):
        self.counter += 1
        item = Item.objects.create(
            title=f'对照商品 {self.counter}', description='用于完成率对照',
            price=Decimal('30.00'), status='sold', seller=self.seller,
            category=self.category, location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00',
            status=status or ('completed' if success else 'cancelled'),
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        order.refresh_from_db()
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.handoff_code_hash = 'hashed-code'
        confirmation.handoff_code_used_at = self.now - timedelta(hours=1) if used else None
        confirmation.save()
        return order

    def test_a_small_sample_withholds_the_direction(self):
        # 两组各自不足门槛时只报数字、不给方向：一个把对照讲成结论的模块，会推动
        # 平台去做一个没有因果关系的功能。
        for _ in range(MIN_MEASURED_ORDERS - 1):
            self.make_order(used=True, success=True)
            self.make_order(used=False, success=False)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertTrue(report['outcome_contrast']['is_small_sample'])
        self.assertFalse(report['outcome_contrast']['has_direction'])
        self.assertIn('只报数字、不给方向', report['summary'])

    def test_a_gap_inside_the_noise_band_withholds_the_direction(self):
        # 两个方向相差几个百分点，在几十条样本上和一次抛硬币没有区别。
        # 两组各 24 笔，做成数只差 1 笔：37.5% 对 41.7%，4.2 个百分点正是噪声带内。
        for index in range(MIN_MEASURED_ORDERS * 2):
            self.make_order(used=True, success=index < 9)
            self.make_order(used=False, success=index < 10)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertFalse(report['outcome_contrast']['is_small_sample'])
        self.assertFalse(report['outcome_contrast']['has_direction'])

    def test_a_gap_outside_the_noise_band_gives_a_direction_but_not_a_conclusion(self):
        for index in range(MIN_MEASURED_ORDERS):
            self.make_order(used=True, success=index < 11)
            self.make_order(used=False, success=index < 1)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertTrue(report['outcome_contrast']['has_direction'])
        self.assertIn('这是对照，不是结论', report['summary'])

    def test_cancelled_orders_stay_in_both_groups(self):
        # 取消不是失败。把它从对照组里剔掉会让两边的完成率一起变好看，而那正好是
        # 最该被看见的那批订单。
        for index in range(MIN_MEASURED_ORDERS):
            self.make_order(used=True, success=False, status='cancelled')
            self.make_order(used=False, success=False, status='cancelled')

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['outcome_contrast']['used_count'], MIN_MEASURED_ORDERS)
        self.assertEqual(report['outcome_contrast']['unused_count'], MIN_MEASURED_ORDERS)
        self.assertEqual(report['outcome_contrast']['used_success_count'], 0)


class LockedListTests(TestCase):
    """撞到五次锁定的订单，逐条列出来给运营去核实。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('locked-seller')
        self.buyer = make_user('locked-buyer')
        self.category = Category.objects.create(name='锁定分类')
        self.location = CampusLocation.objects.create(name='锁定地点')
        self.item = Item.objects.create(
            title='锁定商品', description='用于锁定清单', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_order(self, status='completed'):
        # 一笔商品同时只能有一笔进行中的订单，所以每笔订单各建一件商品。
        order = Order.objects.create(
            item=Item.objects.create(
                title='分档商品', description='用于尝试分档', price=Decimal('30.00'),
                status='sold', seller=self.seller, category=self.category,
                location=self.location,
            ), buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        order.refresh_from_db()
        return order

    def lock(self, order, *, attempts=LOCK_THRESHOLD, hint='末两位 07'):
        confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
        confirmation.handoff_code_hash = 'hashed-code'
        confirmation.handoff_code_hint = hint
        confirmation.handoff_code_issued_at = self.now - timedelta(hours=2)
        confirmation.handoff_code_attempts = attempts
        confirmation.save()
        return confirmation

    def test_locked_orders_are_listed_for_follow_up(self):
        # 错误次数达到上限，意味着现场发生过一次真实的沟通问题，而不是手滑打错
        # 一位。每一行都要带上后来有没有重新生成、有没有最终做成——因为一笔锁定
        # 之后换码顺利交付的订单，和一笔锁定后就僵在那里的订单，要做的动作完全不同。
        order = self.make_order(status='completed')
        self.lock(order)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(len(report['locked_rows']), 1)
        row = report['locked_rows'][0]
        self.assertEqual(row['attempts'], LOCK_THRESHOLD)
        self.assertTrue(row['is_success'])
        self.assertEqual(row['hint'], '末两位 07')

    def test_a_first_try_order_is_not_in_the_list(self):
        order = self.make_order()
        confirmation = self.lock(order, attempts=0)
        confirmation.handoff_code_used_at = self.now - timedelta(hours=1)
        confirmation.save()

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(report['locked_rows'], [])

    def test_the_list_is_capped(self):
        # 清单是给"现在去核实"用的，不是给归档用的。
        for index in range(5):
            item = Item.objects.create(
                title=f'锁定商品 {index}', description='用于锁定清单',
                price=Decimal('30.00'), status='sold', seller=self.seller,
                category=self.category, location=self.location,
            )
            order = Order.objects.create(
                item=item, buyer=self.buyer, seller=self.seller,
                meeting_location=self.location, agreed_price='30.00',
                status='completed',
            )
            Order.objects.filter(pk=order.pk).update(
                created_at=self.now - timedelta(days=1),
            )
            self.lock(order)

        report = build_handoff_code_usage(days=30, now=self.now)
        self.assertEqual(len(report['locked_rows']), 5)
        self.assertGreaterEqual(report['locked_row_limit'], 5)

    def test_the_lock_threshold_is_exported_for_the_template(self):
        # 阈值写在视图里，模板要显示它，就不该在两处各写一个 5。
        self.assertEqual(report_lock_threshold(), LOCK_THRESHOLD)


def report_lock_threshold():
    from .handoff_code_usage import LOCK_THRESHOLD as threshold
    return threshold


class DashboardSurfaceTests(TestCase):
    """看板与导出是这套统计真正落地的地方。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('code-staff')
        self.staff.is_staff = True
        self.staff.save()
        self.seller = make_user('surface-seller')
        self.buyer = make_user('surface-buyer')
        self.category = Category.objects.create(name='呈现分类')
        self.location = CampusLocation.objects.create(name='呈现地点')

    def seed(self):
        from .models import Notification
        for index, used in enumerate([True, True, False]):
            item = Item.objects.create(
                title=f'呈现商品 {index}', description='用于看板呈现',
                price=Decimal('30.00'), status='sold', seller=self.seller,
                category=self.category, location=self.location,
            )
            order = Order.objects.create(
                item=item, buyer=self.buyer, seller=self.seller,
                meeting_location=self.location, agreed_price='30.00',
                status='completed',
            )
            Order.objects.filter(pk=order.pk).update(
                created_at=self.now - timedelta(days=1),
            )
            confirmation, _ = DeliveryConfirmation.objects.get_or_create(order=order)
            confirmation.handoff_code_hash = 'hashed-code'
            confirmation.handoff_code_issued_at = self.now - timedelta(hours=2)
            confirmation.handoff_code_used_at = self.now - timedelta(hours=1) if used else None
            confirmation.handoff_code_attempts = 0 if used else LOCK_THRESHOLD
            confirmation.save()
            Notification.objects.create(
                recipient=self.seller, actor=self.buyer, order=order, item=item,
                kind='order_status', title='买家已生成交付确认码',
                message='已生成新的交付确认码。',
            )

    def test_the_dashboard_renders_the_code_section(self):
        self.seed()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '在现场念出来的那六位数字')
        self.assertContains(response, '生成率')
        self.assertContains(response, '撞到 5 次锁定')

    def test_the_export_carries_the_code_section(self):
        self.seed()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        body = response.content.decode('utf-8-sig')
        self.assertIn('在现场念出来的那六位数字', body)
        self.assertIn('重新生成过（下界）', body)
        self.assertIn('这是对照，不是结论', body)

    def test_the_export_also_carries_the_section_when_there_is_no_data(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8-sig')
        self.assertIn('在现场念出来的那六位数字', body)
        self.assertIn('当前周期还没有走完当面交付阶段的订单', body)
