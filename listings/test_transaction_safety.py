from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CampusVerification

from .models import Category, CampusLocation, DeliveryConfirmation, Item, MeetingAppointment, Order
from .transaction_safety import build_transaction_safety


class TransactionSafetyTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='safety-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='safety-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='安全测试分类')
        self.location = CampusLocation.objects.create(
            name='门卫室大厅', building='东校区', is_public=True,
            safety_note='全天有值班人员。',
        )
        self.item = Item.objects.create(
            title='安全测试商品', description='测试交易安全卡', price='20.00',
            category=self.category, location=self.location,
            condition='良好', seller=self.seller,
        )
        self.order = Order.objects.create(
            item=self.item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='20.00', status='confirmed',
        )

    def test_new_order_gets_concrete_pre_trade_guidance(self):
        summary = build_transaction_safety(self.order)
        titles = {signal.title for signal in summary.signals}

        self.assertIn('商品暂未上传图片', titles)
        self.assertIn('发布者暂无历史完成交易', titles)
        self.assertIn('尚未形成双方确认的预约', titles)
        self.assertEqual(summary.warning_count, 1)
        self.assertEqual(summary.tone, 'warning')

    def test_low_price_and_private_location_are_flagged(self):
        Item.objects.create(
            title='同类高价商品', description='用于计算同类均值', price='100.00',
            category=self.category, location=self.location,
            condition='全新', seller=self.seller,
        )
        private_location = CampusLocation.objects.create(name='宿舍房间', is_public=False)
        self.order.meeting_location = private_location
        self.order.save(update_fields=['meeting_location'])

        summary = build_transaction_safety(self.order)
        titles = {signal.title for signal in summary.signals}

        self.assertIn('商品价格明显低于同类近期均值', titles)
        self.assertIn('当前地点未标记为公共交付区域', titles)
        self.assertGreaterEqual(summary.warning_count, 3)

    def test_verified_confirmed_delivery_has_positive_signals(self):
        CampusVerification.objects.create(
            user=self.seller,
            campus_email='seller@example.edu',
            domain_name='测试校园',
            status='verified',
            verified_at=timezone.now(),
        )
        appointment_start = timezone.localtime(timezone.now()).replace(
            hour=10, minute=0, second=0, microsecond=0,
        ) + timedelta(days=1)
        appointment = MeetingAppointment.objects.create(
            order=self.order,
            proposed_by=self.seller,
            location=self.location,
            start_at=appointment_start,
            end_at=appointment_start + timedelta(hours=1),
            status='confirmed',
        )
        self.order.status = 'meeting'
        self.order.save(update_fields=['status'])
        DeliveryConfirmation.objects.create(order=self.order, handoff_code_hash='hashed-code')

        summary = build_transaction_safety(self.order)
        titles = {signal.title for signal in summary.signals}

        self.assertIn('发布者已完成校园身份认证', titles)
        self.assertIn('交付地点属于公共区域', titles)
        self.assertIn('双方已确认交付时间', titles)
        self.assertIn('已启用交付确认码', titles)
        self.assertEqual(summary.warning_count, 1)  # 商品仍未上传图片
        self.assertEqual(summary.positive_count, 4)

    def test_order_detail_renders_safety_card(self):
        self.client.login(username='safety-buyer', password='safe-password-123')

        response = self.client.get(reverse('order_detail', args=[self.order.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '交易安全卡')
        self.assertContains(response, '发布者暂无历史完成交易')
        self.assertContains(response, '选择公共交付地点并保留订单沟通记录')
