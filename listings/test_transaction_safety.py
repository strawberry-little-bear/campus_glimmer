from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CampusVerification

from .models import Category, CampusLocation, DeliveryConfirmation, Item, MeetingAppointment, Notification, Order, OrderEvent, OrderSafetyAcknowledgement
from .transaction_safety import build_transaction_safety, transaction_safety_fingerprint


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

    def test_both_roles_can_acknowledge_the_current_safety_card(self):
        self.client.login(username='safety-buyer', password='safe-password-123')
        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))

        acknowledgement = OrderSafetyAcknowledgement.objects.get()
        self.assertEqual(acknowledgement.role, 'buyer')
        self.assertEqual(len(acknowledgement.fingerprint), 64)
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(OrderEvent.objects.count(), 0)

        self.client.login(username='safety-seller', password='safe-password-123')
        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))

        response = self.client.get(reverse('order_detail', args=[self.order.id]))
        self.assertContains(response, '买家已确认')
        self.assertContains(response, '卖家已确认')
        self.assertContains(response, '你已确认当前版本')
        self.assertNotContains(response, '买家未确认')
        self.assertNotContains(response, '卖家未确认')
        self.assertEqual(OrderSafetyAcknowledgement.objects.count(), 2)

    def test_repeated_acknowledgement_is_idempotent(self):
        self.client.login(username='safety-buyer', password='safe-password-123')

        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))
        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))

        self.assertEqual(OrderSafetyAcknowledgement.objects.filter(role='buyer').count(), 1)

    def test_changed_safety_actions_require_a_new_acknowledgement(self):
        private_location = CampusLocation.objects.create(name='宿舍楼层', is_public=False)
        self.client.login(username='safety-buyer', password='safe-password-123')
        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))
        first = OrderSafetyAcknowledgement.objects.get()

        self.order.meeting_location = private_location
        self.order.save(update_fields=['meeting_location'])

        response = self.client.get(reverse('order_detail', args=[self.order.id]))
        self.assertIsNone(response.context['my_safety_acknowledgement'])
        self.assertTrue(response.context['can_acknowledge_order_safety'])
        self.assertContains(response, '买家未确认')
        self.assertContains(response, '卖家未确认')

        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))
        acknowledgements = OrderSafetyAcknowledgement.objects.filter(role='buyer')
        self.assertEqual(acknowledgements.count(), 2)
        self.assertNotEqual(first.fingerprint, acknowledgements.latest('acknowledged_at').fingerprint)

    def test_fingerprint_ignores_explanatory_number_drift(self):
        reference = Item.objects.create(
            title='同类参照商品', description='用于改变均值', price='100.00',
            category=self.category, location=self.location,
            condition='全新', seller=self.seller,
        )
        before = transaction_safety_fingerprint(build_transaction_safety(self.order))
        reference.price = '110.00'
        reference.save(update_fields=['price', 'updated_at'])
        after = transaction_safety_fingerprint(build_transaction_safety(self.order))

        self.assertEqual(before, after)

    def test_acknowledgement_boundaries(self):
        outsider = User.objects.create_user(username='safety-outsider', password='safe-password-123')
        self.client.login(username='safety-outsider', password='safe-password-123')
        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))
        self.assertEqual(OrderSafetyAcknowledgement.objects.count(), 0)

        self.client.login(username='safety-buyer', password='safe-password-123')
        self.client.get(reverse('acknowledge_order_safety', args=[self.order.id]))
        self.assertEqual(OrderSafetyAcknowledgement.objects.count(), 0)

        self.order.status = 'completed'
        self.order.save(update_fields=['status', 'updated_at'])
        self.client.post(reverse('acknowledge_order_safety', args=[self.order.id]))
        self.assertEqual(OrderSafetyAcknowledgement.objects.count(), 0)
