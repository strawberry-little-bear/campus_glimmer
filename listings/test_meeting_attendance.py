# -*- coding: utf-8 -*-
"""约好的那一次见面，第一次被运营读到。

交付预约从上线起就在记两段时间，而运营侧一条统计都没有。这些测试要钉住的不是
四舍五入，而是四件更根本的事。

第一件是到场统计的分母。只有"约定结束时间已经过去"的确认预约才谈得上"有没有人到"；
把还没到结束时间的约会算进分母，会让到场率凭空变小，而这份数据会被用来判断交付
环节健康不健康。这是右删失样本，和订单阶段流模块同一个道理，测试必须钉住它。

第二件是迟到分档的口径。提前到和准点必须分开：一个提前二十分钟到的人不该被记成
"准点"，那会让"大家都守时"这句话变成一句被统计口径制造出来的好话。

第三件是异常与到场记录的交叉。提交人说的是一句主观陈述，签到记录是系统里另一方
亲手留下的动作。这个模块不做判定，只把对不上的那一条摊开给运营看；测试要钉住
"只对照未到场这一类异常"，因为安全问题和"其他"跟到没到无关，硬拉进来只会得到
一堆没有意义的"无依据"。

第四件是未接受原因。这个字段的写入路径是视图里的一句硬编码，用户从来没机会填它，
所以它的取值分布只可能有一个值。测试要钉住"如实报告它是常量"，而不是让某个
分布图把这件事讲成一次选择。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from decimal import Decimal

from .meeting_attendance import (
    CONTRADICTION_LIMIT,
    MIN_MEASURED_APPOINTMENTS,
    _arrival_offset_seconds,
    _format_offset,
    _has_ended,
    _lateness_bucket_index,
    build_meeting_attendance,
)
from .models import (
    CampusLocation, Category, Item, MeetingAppointment, MeetingIncident, Order,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username, is_staff=False):
    return User.objects.create_user(
        username=username, password='safe-password-123', is_staff=is_staff,
    )


class MeetingAttendanceFixtureTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('attendance-seller')
        self.buyer = make_user('attendance-buyer')
        self.staff = make_user('attendance-staff', is_staff=True)
        self.category = Category.objects.create(name='到场分类')
        self.location = CampusLocation.objects.create(name='到场地点')

    def make_item(self, title='到场商品'):
        return Item.objects.create(
            title=title, description='用于到场统计', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )

    def make_appointment(self, *, item=None, status='confirmed',
                         start_at=None, end_at=None, created_at=None,
                         responded_at=None, buyer_arrived_at=None,
                         seller_arrived_at=None):
        item = item or self.make_item()
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='meeting',
        )
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=start_at or (self.now - timedelta(hours=3)),
            end_at=end_at or (self.now - timedelta(hours=2)),
            status=status,
        )
        updates = {}
        if created_at is not None:
            updates['created_at'] = created_at
        if responded_at is not None:
            updates['responded_at'] = responded_at
        if buyer_arrived_at is not None:
            updates['buyer_arrived_at'] = buyer_arrived_at
        if seller_arrived_at is not None:
            updates['seller_arrived_at'] = seller_arrived_at
        if updates:
            MeetingAppointment.objects.filter(pk=appointment.pk).update(**updates)
            appointment.refresh_from_db()
        return appointment

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_meeting_attendance(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['response_stats']['total'], 0)
        self.assertEqual(report['attendance_stats']['confirmed_ended_count'], 0)
        self.assertEqual(report['incident_crosscheck']['total'], 0)
        self.assertIn('还没有交付时间安排', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_meeting_attendance(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_an_appointment_created_before_the_window_is_excluded(self):
        self.make_appointment(created_at=self.now - timedelta(days=45))

        report = build_meeting_attendance(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_an_appointment_created_in_the_future_is_excluded(self):
        self.make_appointment(created_at=self.now + timedelta(days=1))

        report = build_meeting_attendance(days=30, now=self.now)
        self.assertFalse(report['has_data'])
