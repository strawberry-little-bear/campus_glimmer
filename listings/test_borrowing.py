from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ItemForm
from .models import CampusLocation, Category, DeliveryConfirmation, Item, Notification, Order
from .order_maintenance import process_borrow_due_notifications


class BorrowingFlowTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='borrow-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='borrow-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='借用测试', description='测试分类')
        self.location = CampusLocation.objects.create(name='借用测试点', building='东校区')
        self.item = Item.objects.create(
            title='可借用投影仪',
            description='适合社团活动短期使用',
            trade_mode='borrow',
            price='88.00',
            deposit_amount='120.00',
            borrow_days=14,
            category=self.category,
            location=self.location,
            condition='9成新',
            seller=self.seller,
        )

    def _create_order(self):
        self.client.login(username='borrow-buyer', password='safe-password-123')
        response = self.client.post(
            reverse('create_order', args=[self.item.id]),
            {'meeting_location': self.location.id, 'buyer_note': '社团活动使用后按时归还'},
        )
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(item=self.item)
        self.assertEqual(order.agreed_price, Decimal('0.00'))
        self.assertEqual(order.deposit_amount, Decimal('120.00'))
        self.assertIsNotNone(order.return_due_at)
        self.assertAlmostEqual(
            (order.return_due_at - timezone.now()).total_seconds(),
            timedelta(days=14).total_seconds(),
            delta=10,
        )
        self.client.logout()
        return order

    def _start_borrowing(self):
        order = self._create_order()
        self.client.login(username='borrow-seller', password='safe-password-123')
        status_url = reverse('update_order_status', args=[order.id])
        self.client.post(status_url, {'status': 'confirmed'})
        self.client.post(status_url, {'status': 'meeting'})
        self.client.logout()

        self.client.login(username='borrow-buyer', password='safe-password-123')
        self.client.post(reverse('confirm_delivery', args=[order.id]))
        self.client.logout()
        self.client.login(username='borrow-seller', password='safe-password-123')
        self.client.post(reverse('confirm_delivery', args=[order.id]))
        order.refresh_from_db()
        return order

    def test_borrow_form_zeroes_price_and_keeps_borrow_terms(self):
        form = ItemForm(data={
            'title': '借用相机',
            'description': '测试',
            'trade_mode': 'borrow',
            'price': '999',
            'deposit_amount': '50',
            'borrow_days': '21',
            'category': self.category.id,
            'location': self.location.id,
            'condition': '全新',
            'expires_at': '',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['price'], Decimal('0.00'))
        self.assertEqual(form.cleaned_data['deposit_amount'], Decimal('50.00'))
        self.assertEqual(form.cleaned_data['borrow_days'], 21)

    def test_delivery_starts_borrowing_instead_of_completing_sale(self):
        order = self._start_borrowing()
        self.assertEqual(order.status, 'borrowed')
        self.assertEqual(order.item.status, 'reserved')
        self.assertIsNotNone(order.delivery_confirmation.buyer_confirmed_at)
        self.assertIsNotNone(order.delivery_confirmation.seller_confirmed_at)
        self.assertTrue(Notification.objects.filter(
            recipient=self.borrower,
            order=order,
            title='借用已开始',
        ).exists())

    def test_both_parties_confirm_return_and_item_is_available_again(self):
        order = self._start_borrowing()

        self.client.login(username='borrow-buyer', password='safe-password-123')
        detail_response = self.client.get(reverse('order_detail', args=[order.id]))
        self.assertContains(detail_response, '借用进行中')
        self.assertContains(detail_response, '登记我已归还')
        response = self.client.post(reverse('confirm_return', args=[order.id]))
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        confirmation = DeliveryConfirmation.objects.get(order=order)
        self.assertIsNotNone(confirmation.buyer_returned_at)
        order.refresh_from_db()
        self.assertEqual(order.status, 'borrowed')
        self.assertEqual(order.item.status, 'reserved')
        self.client.logout()

        self.client.login(username='borrow-seller', password='safe-password-123')
        response = self.client.post(reverse('confirm_return', args=[order.id]))
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        order.refresh_from_db()
        confirmation.refresh_from_db()
        self.assertEqual(order.status, 'returned')
        self.assertEqual(order.item.status, 'available')
        self.assertIsNotNone(order.returned_at)
        self.assertTrue(confirmation.return_is_complete)
        self.assertTrue(Order.objects.filter(pk=order.pk, status='returned').exists())


    def test_borrow_maintenance_reminds_before_due_and_notifies_overdue_once(self):
        order = self._start_borrowing()
        base_time = timezone.now()
        order.return_due_at = base_time + timedelta(hours=3)
        order.save(update_fields=['return_due_at', 'updated_at'])

        result = process_borrow_due_notifications(now=base_time, reminder_hours=6)
        self.assertEqual(result, {'reminded': 1, 'overdue': 0})
        order.refresh_from_db()
        self.assertIsNotNone(order.return_reminder_sent_at)
        self.assertEqual(Notification.objects.filter(order=order, kind='order_expiring').count(), 2)

        second_result = process_borrow_due_notifications(now=base_time + timedelta(hours=1), reminder_hours=6)
        self.assertEqual(second_result, {'reminded': 0, 'overdue': 0})
        self.assertEqual(Notification.objects.filter(order=order, kind='order_expiring').count(), 2)

        order.return_due_at = base_time - timedelta(minutes=1)
        order.save(update_fields=['return_due_at', 'updated_at'])
        overdue_result = process_borrow_due_notifications(now=base_time, reminder_hours=6)
        self.assertEqual(overdue_result, {'reminded': 0, 'overdue': 1})
        order.refresh_from_db()
        self.assertIsNotNone(order.return_overdue_notice_sent_at)
        self.assertEqual(Notification.objects.filter(order=order, kind='order_expired').count(), 2)
        self.assertEqual(
            process_borrow_due_notifications(now=base_time + timedelta(hours=1)),
            {'reminded': 0, 'overdue': 0},
        )

    def test_non_borrow_order_cannot_use_return_endpoint(self):
        sale_item = Item.objects.create(
            title='普通出售商品', description='测试', price='10.00',
            category=self.category, location=self.location, condition='全新', seller=self.seller,
        )
        order = Order.objects.create(
            item=sale_item, buyer=self.borrower, seller=self.seller,
            agreed_price='10.00', status='completed',
        )
        self.client.login(username='borrow-buyer', password='safe-password-123')
        response = self.client.post(reverse('confirm_return', args=[order.id]))
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        self.assertFalse(DeliveryConfirmation.objects.filter(order=order).exists())