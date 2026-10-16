# -*- coding: utf-8 -*-
"""交付开放时段风险审计的口径、页面与导出测试。

这个面板不是在给预约判违规：系统仍然允许双方把交付约到登记时段之外。
测试要守住的是三类信息不能混在一起——地点没登记、长期规则确实不覆盖、
单日例外改变了当天口径；以及运营看到的风险清单不能变成用户清单。
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .meeting_service_window_quality import (
    RISK_ROW_LIMIT,
    build_meeting_service_window_quality,
)
from .models import (
    CampusLocation,
    CampusLocationServiceException,
    CampusLocationServiceExceptionWindow,
    CampusLocationServiceWindow,
    Category,
    Item,
    MeetingAppointment,
    Notification,
    Order,
)


class MeetingServiceWindowQualityTestBase(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 10, 18, 12, 0))
        self.seller = User.objects.create_user(
            username='window-audit-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='window-audit-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='开放时段审计分类')
        self.location = CampusLocation.objects.create(name='审计图书馆')
        self.counter = 0

    def make_appointment(self, *, location='unset', hour=10, minute=0,
                         status='confirmed', title='开放时段审计商品'):
        if location == 'unset':
            location = self.location
        self.counter += 1
        item = Item.objects.create(
            title=f'{title}-{self.counter}',
            description='用于交付开放时段审计',
            price=Decimal('25.00'),
            status='available',
            seller=self.seller,
            category=self.category,
            location=self.location,
            condition='良好',
        )
        order = Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.location,
            agreed_price='25.00',
            status='confirmed',
        )
        start_at = timezone.make_aware(
            datetime.combine(date(2026, 10, 19), time(hour, minute)),
        )
        return MeetingAppointment.objects.create(
            order=order,
            proposed_by=self.seller,
            location=location,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            status=status,
        )


class MeetingServiceWindowQualityClassificationTests(MeetingServiceWindowQualityTestBase):
    def test_empty_state_has_no_false_risk(self):
        panel = build_meeting_service_window_quality(now=self.now)

        self.assertFalse(panel['has_data'])
        self.assertEqual(panel['risk_rows'], [])
        self.assertEqual(panel['metrics']['total_count'], 0)
        self.assertEqual(panel['metrics']['attention_count'], 0)
        self.assertIn('没有待核对', panel['summary'])

    def test_open_weekly_outside_unregistered_and_missing_location_are_separate(self):
        unregistered = CampusLocation.objects.create(name='未登记时段地点')
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        self.make_appointment(hour=10, title='时段内商品')
        self.make_appointment(hour=20, title='每周时段外商品')
        self.make_appointment(location=unregistered, hour=21, title='未登记商品')
        self.make_appointment(location=None, hour=22, title='无地点商品')

        panel = build_meeting_service_window_quality(now=self.now)
        metrics = panel['metrics']

        self.assertEqual(metrics['total_count'], 4)
        self.assertEqual(metrics['measurable_count'], 3)
        self.assertEqual(metrics['open_count'], 1)
        self.assertEqual(metrics['weekly_outside_count'], 1)
        self.assertEqual(metrics['unregistered_count'], 1)
        self.assertEqual(metrics['no_location_count'], 1)
        self.assertEqual(metrics['registered_outside_count'], 1)
        self.assertEqual(metrics['attention_count'], 2)
        self.assertEqual(
            [row['kind'] for row in panel['risk_rows']],
            ['weekly_outside', 'unregistered', 'no_location'],
        )

    def test_closed_and_replacement_exceptions_are_distinguished(self):
        replacement = CampusLocation.objects.create(name='考试周替换地点')
        unused = CampusLocation.objects.create(name='提前维护地点')
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        CampusLocationServiceWindow.objects.create(
            location=replacement, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        self.make_appointment(hour=10, title='临时关闭商品')
        self.make_appointment(location=replacement, hour=11, title='替换时段外商品')
        closed = CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 19),
            exception_type=CampusLocationServiceException.CLOSED,
            reason='假期闭馆',
        )
        replacement_exception = CampusLocationServiceException.objects.create(
            location=replacement,
            effective_date=date(2026, 10, 19),
            exception_type=CampusLocationServiceException.REPLACEMENT,
            reason='考试周缩短开放',
        )
        CampusLocationServiceExceptionWindow.objects.create(
            service_exception=replacement_exception,
            start_time=time(9), end_time=time(10),
        )
        CampusLocationServiceException.objects.create(
            location=unused,
            effective_date=date(2026, 10, 20),
            exception_type=CampusLocationServiceException.CLOSED,
            reason='设备检修',
        )

        panel = build_meeting_service_window_quality(now=self.now)
        metrics = panel['metrics']

        self.assertEqual(metrics['closed_exception_count'], 1)
        self.assertEqual(metrics['replacement_outside_count'], 1)
        self.assertEqual(metrics['weekly_outside_count'], 0)
        self.assertEqual(metrics['future_count'], 3)
        self.assertEqual(metrics['matched_count'], 2)
        self.assertEqual(metrics['unused_count'], 1)
        self.assertEqual(metrics['unused_location_count'], 1)
        self.assertIn('假期闭馆', panel['risk_rows'][0]['reason'])
        self.assertIn('考试周缩短开放', panel['risk_rows'][1]['reason'])
        self.assertEqual(closed.effective_date, date(2026, 10, 19))

    def test_inactive_exception_falls_back_to_weekly_rule(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        appointment = self.make_appointment(hour=20, title='停用例外商品')
        CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 19),
            exception_type=CampusLocationServiceException.CLOSED,
            reason='已停用例外',
            is_active=False,
        )

        panel = build_meeting_service_window_quality(now=self.now)

        self.assertEqual(panel['metrics']['closed_exception_count'], 0)
        self.assertEqual(panel['metrics']['weekly_outside_count'], 1)
        self.assertEqual(panel['metrics']['future_count'], 0)
        self.assertEqual(panel['risk_rows'][0]['order_id'], appointment.order_id)

    def test_page_limit_keeps_full_list_for_export(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        for _ in range(RISK_ROW_LIMIT + 1):
            self.make_appointment(hour=20, title='分页风险商品')

        panel = build_meeting_service_window_quality(now=self.now)

        self.assertEqual(len(panel['risk_rows']), RISK_ROW_LIMIT + 1)
        self.assertEqual(len(panel['risk_page_rows']), RISK_ROW_LIMIT)
        self.assertEqual(panel['risk_page_rows'][0]['order_id'], panel['risk_rows'][0]['order_id'])

    def test_location_rows_sort_by_attention_then_total_then_name(self):
        quiet = CampusLocation.objects.create(name='安静地点')
        busy = CampusLocation.objects.create(name='忙碌地点')
        for location, total in ((self.location, 3), (quiet, 1), (busy, 5)):
            CampusLocationServiceWindow.objects.create(
                location=location, weekday=1,
                start_time=time(9), end_time=time(18),
            )
            for index in range(total):
                self.make_appointment(
                    location=location,
                    hour=20 if index == 0 else 10,
                    title=f'{location.name}-商品',
                )

        panel = build_meeting_service_window_quality(now=self.now)

        self.assertEqual(
            [row['location_name'] for row in panel['location_rows']],
            ['忙碌地点', '审计图书馆', '安静地点'],
        )
        self.assertEqual(panel['location_rows'][0]['attention_count'], 1)


class MeetingServiceWindowQualityPageTests(MeetingServiceWindowQualityTestBase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            username='window-audit-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_page_and_csv_share_the_same_audit_language(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        appointment = self.make_appointment(hour=20, title='导出风险商品')

        page = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        page_text = page.content.decode('utf-8')
        export_text = export.content.decode('utf-8-sig')

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '交付开放时段风险')
        self.assertContains(page, f'#{appointment.order_id}')
        self.assertIn('交付开放时段风险', export_text)
        self.assertIn(f'订单 {appointment.order_id}', export_text)
        self.assertIn('安排不在登记每周开放时段内', export_text)
        self.assertIn('不自动改约', page_text)
        self.assertIn('不自动改约', export_text)

    def test_builder_is_read_only(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        appointment = self.make_appointment(hour=20, title='只读风险商品')
        exception = CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 19),
            exception_type=CampusLocationServiceException.CLOSED,
            reason='只读例外',
        )
        exception_window = CampusLocationServiceExceptionWindow.objects.create(
            service_exception=exception,
            start_time=time(9), end_time=time(10),
        )
        snapshots = {
            model: list(model.objects.values())
            for model in (
                CampusLocation, CampusLocationServiceWindow,
                CampusLocationServiceException, CampusLocationServiceExceptionWindow,
                MeetingAppointment, Order, Item, Notification,
            )
        }

        panel = build_meeting_service_window_quality(now=self.now)
        panel_again = build_meeting_service_window_quality(days=90, now=self.now)

        self.assertEqual(panel['metrics'], panel_again['metrics'])
        self.assertEqual(panel['risk_rows'], panel_again['risk_rows'])
        self.assertEqual(snapshots[MeetingAppointment][0]['id'], appointment.id)
        self.assertEqual(snapshots[CampusLocationServiceException][0]['id'], exception.id)
        self.assertEqual(snapshots[CampusLocationServiceExceptionWindow][0]['id'], exception_window.id)
        for model, before in snapshots.items():
            self.assertEqual(list(model.objects.values()), before)


