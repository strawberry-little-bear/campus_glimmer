# -*- coding: utf-8 -*-
"""一次交付安排被改了几次才定下来，第一次被运营读到。

`MeetingAppointment` 是 `OneToOneField(order)`，`propose_meeting` 对同一笔订单
第二次提交时把时间、地点、提议人原地覆盖，`status` 打回 `pending`。前几版不在
任何表里，唯一的痕迹是 `OrderEvent` 里那句 `更新了交付时间安排，等待对方确认`。
这些测试要钉住的不是四舍五入，而是四件更根本的事。

第一件是证据的性质。改期次数是从一句自由文本备注反推的下界，不是精确值。
测试要钉住：`OrderEvent` 上线之前的改动没有痕迹，会被算成一次谈成；匹配必须
写全「更新了」三个字，否则第一次提议也会被算成一次改期。

第二件是分母。只有排过交付时间的订单才谈得上改了几次；连一次都没约上的订单
不进分母。测试要钉住它被单独数出来，而不是被悄悄并进「一次谈成」。

第三件是响应时长的尺度必须和 `meeting_attendance` 对得上。两边读的是同一类
字段，如果分档上界不一致，运营会以为其中一个看板是错的。测试要钉住两组桶的
上界逐字相同。

第四件是地点只能和「当前这一版」比。前几版的地点被原地覆盖了，库里没有，
所以「地点改过几次」这个问题平台答不了。测试要钉住报的是「最后一次改期时地点
和现在这一版不一样」，而不是一个编出来的次数。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import (
    CampusLocation,
    Category,
    Item,
    MeetingAppointment,
    Order,
    OrderEvent,
)
from .meeting_attendance import RESPONSE_BUCKETS as ATTENDANCE_RESPONSE_BUCKETS
from .meeting_reschedule import (
    MIN_MEASURED_ORDERS,
    NOISE_POINTS,
    RESCHEDULE_BUCKETS,
    RESPONSE_BUCKETS,
    RESCHEDULE_NOTE,
    _bucket_index,
    _format_duration,
    build_meeting_reschedule,
)


# 夹具里「这一格没有地点」的哨兵。直接写 None 会被 `or self.location` 吃掉，
# 那样「两边都没地点」这条就永远造不出来。
UNSET = object()


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class RescheduleSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('resched-seller')
        self.buyer = make_user('resched-buyer')
        self.category = Category.objects.create(name='改期分类')
        self.location = CampusLocation.objects.create(name='改期地点')
        self.counter = 0

    def make_item(self, title):
        self.counter += 1
        return Item.objects.create(
            title=title, description='用于改期统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_appointment(self, *, title='改期商品', reschedule_count=0, days_ago=1,
                         start_in_hours=24, responded_hours_after=None,
                         proposed_by=None, location=None):
        """造一条交付安排，并补上 `reschedule_count` 条改期痕迹。

        痕迹只能是事件行——`propose_meeting` 的 update 分支不建新行，它改的是
        手上这一条。所以这里用事件行模拟那个分支写下的备注。
        """
        item = self.make_item(title)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        created_at = self.now - timedelta(days=days_ago)
        appointment = MeetingAppointment.objects.create(
            order=order,
            proposed_by=proposed_by or self.seller,
            location=location or self.location,
            start_at=self.now + timedelta(hours=start_in_hours),
            end_at=self.now + timedelta(hours=start_in_hours + 1),
            status='confirmed' if responded_hours_after is not None else 'pending',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=created_at,
            responded_by=self.buyer if responded_hours_after is not None else None,
            responded_at=(
                created_at + timedelta(hours=responded_hours_after)
                if responded_hours_after is not None else None
            ),
        )
        appointment.refresh_from_db()
        for index in range(reschedule_count):
            OrderEvent.objects.create(
                order=order,
                actor=proposed_by or self.seller,
                from_status='confirmed',
                to_status='confirmed',
                note=RESCHEDULE_NOTE + '，等待对方确认',
            )
            OrderEvent.objects.filter(pk=OrderEvent.objects.filter(order=order).latest('id').pk).update(
                created_at=appointment.created_at + timedelta(hours=index + 1),
            )
        return appointment

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_meeting_reschedule(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['reschedule_stats']['total'], 0)
        self.assertEqual(report['reschedule_stats']['max_label'], '—')
        self.assertEqual(report['reschedule_rows'], [])
        self.assertIn('还没有已安排的交付时间', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_meeting_reschedule(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_an_appointment_created_before_the_window_is_excluded(self):
        self.make_appointment(title='窗口外', days_ago=45)

        report = build_meeting_reschedule(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['reschedule_stats']['total'], 0)


class EvidenceTests(TestCase):
    """改期次数是下界：证据是事件备注，不是一条记录。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('evi-seller')
        self.buyer = make_user('evi-buyer')
        self.category = Category.objects.create(name='证据分类')
        self.location = CampusLocation.objects.create(name='证据地点')
        self.counter = 0

    def make_item(self, title):
        self.counter += 1
        return Item.objects.create(
            title=title, description='用于证据测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_appointment(self, *, title='证据商品', reschedule_count=0, days_ago=1):
        item = self.make_item(title)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=self.now + timedelta(hours=24), end_at=self.now + timedelta(hours=25),
            status='pending',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        for _index in range(reschedule_count):
            OrderEvent.objects.create(
                order=order, actor=self.seller, from_status='confirmed',
                to_status='confirmed', note=RESCHEDULE_NOTE + '，等待对方确认',
            )
        return appointment

    def test_reschedule_events_are_counted_as_changes(self):
        self.make_appointment(reschedule_count=3)

        report = build_meeting_reschedule(days=30, now=self.now)

        counts = {
            row['label']: row['count'] for row in report['reschedule_stats']['bucket_rows']
        }
        self.assertEqual(counts['改过 2-3 次'], 1)
        self.assertEqual(report['reschedule_stats']['changed_thrice_or_more_count'], 1)

    def test_a_first_proposal_is_not_a_reschedule(self):
        """第一次提交写的是「提出了新的…」，只多一个字，不能算成一次改期。"""
        item = self.make_item('首次提议')
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=self.now + timedelta(hours=24), end_at=self.now + timedelta(hours=25),
            status='pending',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=self.now - timedelta(days=1),
        )
        OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='confirmed', to_status='confirmed',
            note='提出了新的交付时间安排',
        )

        report = build_meeting_reschedule(days=30, now=self.now)

        self.assertEqual(report['reschedule_stats']['settled_once_count'], 1)
        self.assertEqual(report['reschedule_stats']['changed_count'], 0)

    def test_an_order_without_any_event_is_settled_once(self):
        """没有痕迹的订单归「一次谈成」——这是下界，宁可偏大也不编。"""
        self.make_appointment(reschedule_count=0)

        report = build_meeting_reschedule(days=30, now=self.now)

        self.assertEqual(report['reschedule_stats']['settled_once_count'], 1)
        self.assertEqual(report['reschedule_stats']['changed_rate'], 0)


class DenominatorTests(TestCase):
    """分母只收排过交付时间的订单。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('deno-seller')
        self.buyer = make_user('deno-buyer')
        self.category = Category.objects.create(name='分母分类')
        self.location = CampusLocation.objects.create(name='分母地点')
        self.counter = 0

    def make_item(self, title):
        self.counter += 1
        return Item.objects.create(
            title=title, description='用于分母测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def test_an_order_that_never_scheduled_stays_out(self):
        """连一次都没约上的订单谈不到改期，不进分母。"""
        item = self.make_item('没约上')
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='cancelled',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))

        report = build_meeting_reschedule(days=30, now=self.now)

        self.assertEqual(report['reschedule_stats']['total'], 0)
        self.assertFalse(report['has_data'])

    def test_reschedule_buckets_are_left_closed_right_open(self):
        self.assertEqual(RESCHEDULE_BUCKETS[0][0], 1)
        self.assertEqual(_bucket_index(RESCHEDULE_BUCKETS, 0), 0)
        self.assertEqual(_bucket_index(RESCHEDULE_BUCKETS, 1), 1)
        self.assertEqual(_bucket_index(RESCHEDULE_BUCKETS, 2), 2)
        self.assertEqual(_bucket_index(RESCHEDULE_BUCKETS, 3), 2)
        self.assertEqual(_bucket_index(RESCHEDULE_BUCKETS, 4), 3)
        self.assertEqual(_bucket_index(RESCHEDULE_BUCKETS, 9), 3)


class ResponseContrastTests(TestCase):
    """改过的订单是不是更难谈：只报对照，不给因果。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('contr-seller')
        self.buyer = make_user('contr-buyer')
        self.category = Category.objects.create(name='对照分类')
        self.location = CampusLocation.objects.create(name='对照地点')
        self.counter = 0

    def make_item(self, title):
        self.counter += 1
        return Item.objects.create(
            title=title, description='用于对照测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_appointment(self, *, title='对照商品', reschedule_count=0,
                         responded_hours_after=1):
        self.counter += 1
        item = self.make_item(title)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        created_at = self.now - timedelta(days=1)
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=self.now + timedelta(hours=24), end_at=self.now + timedelta(hours=25),
            status='confirmed',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=created_at,
            responded_by=self.buyer,
            responded_at=created_at + timedelta(hours=responded_hours_after),
        )
        appointment.refresh_from_db()
        for index in range(reschedule_count):
            OrderEvent.objects.create(
                order=order, actor=self.seller, from_status='confirmed',
                to_status='confirmed', note=RESCHEDULE_NOTE + '，等待对方确认',
            )
            OrderEvent.objects.filter(
                pk=OrderEvent.objects.filter(order=order).latest('id').pk,
            ).update(created_at=appointment.created_at + timedelta(hours=index + 1))
        return appointment

    def test_the_response_buckets_match_the_attendance_module(self):
        """两边的响应时长必须是同一个尺度，运营才能把两个面板对上。"""
        self.assertEqual(list(RESPONSE_BUCKETS), list(ATTENDANCE_RESPONSE_BUCKETS))

    def test_an_unanswered_appointment_is_not_in_the_contrast(self):
        """没回应的安排答不了「多久回应」，两组都不收。"""
        self.counter += 1
        item = self.make_item('没回应')
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=self.now + timedelta(hours=24), end_at=self.now + timedelta(hours=25),
            status='pending',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=self.now - timedelta(days=1),
        )
        OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='confirmed', to_status='confirmed',
            note=RESCHEDULE_NOTE + '，等待对方确认',
        )

        report = build_meeting_reschedule(days=30, now=self.now)

        contrast = report['response_contrast']
        self.assertEqual(contrast['changed_total'], 0)
        self.assertEqual(contrast['settled_total'], 0)
        self.assertFalse(contrast['both_measured'])

    def test_small_samples_get_numbers_but_no_direction(self):
        self.make_appointment(title='改过的', reschedule_count=2, responded_hours_after=1)
        self.make_appointment(title='一次谈成', reschedule_count=0, responded_hours_after=2)

        report = build_meeting_reschedule(days=30, now=self.now)
        contrast = report['response_contrast']

        self.assertEqual(contrast['changed_total'], 1)
        self.assertEqual(contrast['settled_total'], 1)
        self.assertTrue(contrast['is_small_sample'])
        self.assertIsNone(contrast['gap_points'])
        self.assertFalse(contrast['gap_is_meaningful'])

    def test_a_meaningful_gap_is_only_reported_once_both_sides_are_measured(self):
        for index in range(MIN_MEASURED_ORDERS):
            self.make_appointment(
                title=f'改过{index}', reschedule_count=2, responded_hours_after=1,
            )
            self.make_appointment(
                title=f'谈成{index}', reschedule_count=0, responded_hours_after=4,
            )

        report = build_meeting_reschedule(days=30, now=self.now)
        contrast = report['response_contrast']

        self.assertTrue(contrast['both_measured'])
        self.assertEqual(contrast['changed_median_label'], '1 小时')
        self.assertEqual(contrast['settled_median_label'], '4 小时')
        self.assertEqual(contrast['gap_points'], -75.0)
        self.assertTrue(contrast['gap_is_meaningful'])


class LocationShiftTests(TestCase):
    """改期时地点换没换：只能和当前这一版比。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('loc-seller')
        self.buyer = make_user('loc-buyer')
        self.category = Category.objects.create(name='地点分类')
        self.location = CampusLocation.objects.create(name='原地点')
        self.other_location = CampusLocation.objects.create(name='新地点')
        self.counter = 0

    def make_item(self, title):
        self.counter += 1
        return Item.objects.create(
            title=title, description='用于地点测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_appointment(self, *, title='地点商品', reschedule_count=1,
                         order_location=UNSET, appointment_location=UNSET):
        """造一条安排，地点可以显式传 None——`or self.location` 会把 None 吃掉。"""
        item = self.make_item(title)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location if order_location is UNSET else order_location,
            agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller,
            location=self.location if appointment_location is UNSET else appointment_location,
            start_at=self.now + timedelta(hours=24), end_at=self.now + timedelta(hours=25),
            status='pending',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=self.now - timedelta(days=1),
        )
        for _index in range(reschedule_count):
            OrderEvent.objects.create(
                order=order, actor=self.seller, from_status='confirmed',
                to_status='confirmed', note=RESCHEDULE_NOTE + '，等待对方确认',
            )
        return appointment

    def test_a_changed_location_is_counted(self):
        self.make_appointment(
            title='换过地点', order_location=self.location,
            appointment_location=self.other_location,
        )

        report = build_meeting_reschedule(days=30, now=self.now)

        stats = report['location_stats']
        self.assertEqual(stats['measurable_count'], 1)
        self.assertEqual(stats['shifted_count'], 1)
        self.assertEqual(stats['shift_rate'], 100.0)

    def test_an_unchanged_location_is_not_counted_as_a_shift(self):
        self.make_appointment(title='没换地点')

        report = build_meeting_reschedule(days=30, now=self.now)

        stats = report['location_stats']
        self.assertEqual(stats['measurable_count'], 1)
        self.assertEqual(stats['shifted_count'], 0)
        self.assertEqual(stats['shift_rate'], 0)

    def test_an_order_without_a_location_is_not_measurable(self):
        """两边都没地点的订单答不了换没换，单独数，不当成没换。"""
        self.make_appointment(
            title='没地点', order_location=None, appointment_location=None,
        )
        order = Order.objects.filter(item__title='没地点').first()
        self.assertIsNone(order.meeting_location_id)

        report = build_meeting_reschedule(days=30, now=self.now)

        stats = report['location_stats']
        self.assertEqual(stats['measurable_count'], 0)
        self.assertEqual(stats['both_missing_count'], 1)
        self.assertIsNone(stats['shift_rate'])

    def test_an_order_that_was_never_rescheduled_is_not_measured(self):
        """一次谈成的订单不在这一节里——问的是「改期时换没换」。"""
        self.make_appointment(title='一次谈成', reschedule_count=0)

        report = build_meeting_reschedule(days=30, now=self.now)

        self.assertEqual(report['location_stats']['measurable_count'], 0)


class RoleTests(TestCase):
    """谁在改期：只按角色，不落到具体的人。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('role-seller')
        self.buyer = make_user('role-buyer')
        self.category = Category.objects.create(name='角色分类')
        self.location = CampusLocation.objects.create(name='角色地点')
        self.counter = 0

    def make_item(self, title):
        self.counter += 1
        return Item.objects.create(
            title=title, description='用于角色测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_appointment(self, *, title='角色商品', proposed_by=None):
        self.counter += 1
        item = self.make_item(title)
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=proposed_by, location=self.location,
            start_at=self.now + timedelta(hours=24), end_at=self.now + timedelta(hours=25),
            status='pending',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=self.now - timedelta(days=1),
        )
        return appointment

    def test_the_proposer_role_is_split_by_side_not_by_name(self):
        self.make_appointment(title='买家提的', proposed_by=self.buyer)
        self.make_appointment(title='卖家提的', proposed_by=self.seller)

        report = build_meeting_reschedule(days=30, now=self.now)
        role_stats = report['role_stats']

        self.assertEqual(role_stats['buyer_count'], 1)
        self.assertEqual(role_stats['seller_count'], 1)
        self.assertEqual(role_stats['unknown_count'], 0)
        self.assertEqual(role_stats['buyer_share'], 50.0)

    def test_a_missing_proposer_is_unknown_not_a_guess(self):
        self.make_appointment(title='没有提议人', proposed_by=None)

        report = build_meeting_reschedule(days=30, now=self.now)

        self.assertEqual(report['role_stats']['unknown_count'], 1)

    def test_rows_only_list_orders_rescheduled_twice_or_more(self):
        self.make_appointment(title='改过一次', proposed_by=self.buyer)
        self.make_appointment(title='改过三次', proposed_by=self.seller)
        # 给第二笔补三条改期痕迹。
        order = Order.objects.filter(
            item__title='改过三次',
        ).first()
        for _index in range(3):
            OrderEvent.objects.create(
                order=order, actor=self.seller, from_status='confirmed',
                to_status='confirmed', note=RESCHEDULE_NOTE + '，等待对方确认',
            )

        report = build_meeting_reschedule(days=30, now=self.now)

        rows = report['reschedule_rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['count'], 3)
        self.assertEqual(rows[0]['proposed_by_role'], '卖家')
        self.assertEqual(rows[0]['item_title'], '改过三次')


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
            title='只报商品', description='读前读后一致', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=1))
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=self.now + timedelta(hours=24), end_at=self.now + timedelta(hours=25),
            status='pending',
        )
        MeetingAppointment.objects.filter(pk=appointment.pk).update(
            created_at=self.now - timedelta(days=1),
        )
        OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='confirmed', to_status='confirmed',
            note=RESCHEDULE_NOTE + '，等待对方确认',
        )
        before = MeetingAppointment.objects.get(pk=appointment.pk)
        events_before = OrderEvent.objects.count()

        build_meeting_reschedule(days=30, now=self.now)

        after = MeetingAppointment.objects.get(pk=appointment.pk)
        self.assertEqual(after.start_at, before.start_at)
        self.assertEqual(after.end_at, before.end_at)
        self.assertEqual(after.proposed_by_id, before.proposed_by_id)
        self.assertEqual(after.status, before.status)
        self.assertEqual(OrderEvent.objects.count(), events_before)


class ExportShapeTests(TestCase):
    """看板导出用到的键都在，空态不炸。"""

    def setUp(self):
        self.now = local_noon()

    def test_an_empty_report_still_exposes_every_export_key(self):
        report = build_meeting_reschedule(days=30, now=self.now)

        for key in (
            'summary', 'reschedule_stats', 'response_contrast', 'location_stats',
            'role_stats', 'reschedule_rows', 'reschedule_row_limit',
            'min_measured_orders', 'noise_points',
        ):
            self.assertIn(key, report)
        self.assertIsNone(report['reschedule_stats']['bucket_rows'][0]['share'])
        self.assertEqual(report['min_measured_orders'], MIN_MEASURED_ORDERS)
        self.assertEqual(report['noise_points'], NOISE_POINTS)
        self.assertEqual(_format_duration(None), '—')
        self.assertEqual(_format_duration(7200), '2 小时')