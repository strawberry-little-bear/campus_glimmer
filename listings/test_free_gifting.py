from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import ItemForm
from .models import Category, CampusLocation, CommunityContribution, GiftApplication, Item, Notification, Order


class FreeGiftingTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='gift-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='gift-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='宿舍用品')
        self.location = CampusLocation.objects.create(name='南门快递站')

    def test_free_item_form_normalizes_price_to_zero(self):
        form = ItemForm(data={
            'title': '闲置收纳盒',
            'description': '免费送给需要的同学',
            'trade_mode': 'free',
            'price': '88.00',
            'category': self.category.pk,
            'location': self.location.pk,
            'condition': '8成新',
            'expires_at': '',
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['price'], Decimal('0.00'))

    def test_free_item_creates_queue_application_without_reserving_item(self):
        item = Item.objects.create(
            title='免费台灯', description='带灯泡，毕业清理', trade_mode='free', price='0.00',
            category=self.category, location=self.location, condition='9成新', seller=self.seller,
        )
        self.assertContains(self.client.get(reverse('item_detail', args=[item.pk])), '免费赠送')

        self.client.login(username='gift-buyer', password='safe-password-123')
        response = self.client.post(
            reverse('create_order', args=[item.pk]),
            {'meeting_location': self.location.pk, 'buyer_note': '想在南门领取'},
        )

        application = GiftApplication.objects.get(item=item, applicant=self.buyer)
        self.assertRedirects(response, reverse('my_gift_applications'))
        self.assertEqual(application.status, 'pending')
        self.assertFalse(Order.objects.filter(item=item).exists())
        item.refresh_from_db()
        self.assertEqual(item.status, 'available')
        self.assertTrue(Notification.objects.filter(
            recipient=self.seller, kind='gift_application', title='收到新的领取申请',
        ).exists())
        self.assertContains(self.client.get(reverse('my_gift_applications')), '等待选择')

    def test_seller_selects_one_applicant_and_notifies_the_queue(self):
        item = Item.objects.create(
            title='免费台灯', description='带灯泡，毕业清理', trade_mode='free', price='0.00',
            category=self.category, location=self.location, condition='9成新', seller=self.seller,
        )
        second_buyer = User.objects.create_user(username='gift-buyer-2', password='safe-password-123')
        first = GiftApplication.objects.create(
            item=item, applicant=self.buyer, meeting_location=self.location, applicant_note='我住南区',
        )
        second = GiftApplication.objects.create(
            item=item, applicant=second_buyer, meeting_location=self.location, applicant_note='我可以及时领取',
        )

        self.client.login(username='gift-seller', password='safe-password-123')
        response = self.client.post(reverse('accept_gift_application', args=[first.pk]))

        order = Order.objects.get(item=item)
        first.refresh_from_db()
        second.refresh_from_db()
        item.refresh_from_db()
        self.assertRedirects(response, reverse('order_detail', args=[order.pk]))
        self.assertEqual(order.buyer, self.buyer)
        self.assertEqual(order.agreed_price, Decimal('0.00'))
        self.assertEqual(first.status, 'selected')
        self.assertEqual(first.order_id, order.id)
        self.assertEqual(second.status, 'rejected')
        self.assertEqual(item.status, 'reserved')
        self.assertTrue(Notification.objects.filter(
            recipient=self.buyer, kind='gift_application_status', title='你的领取申请已被选中', order=order,
        ).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=second_buyer, kind='gift_application_status', title='领取申请结果更新', item=item,
        ).exists())

        order.status = 'meeting'
        order.save(update_fields=['status', 'updated_at'])
        self.client.force_login(self.seller)
        self.client.post(reverse('confirm_delivery', args=[order.pk]))
        self.client.force_login(self.buyer)
        self.client.post(reverse('confirm_delivery', args=[order.pk]))

        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        self.assertEqual(CommunityContribution.objects.filter(kind='gift_completed').count(), 2)
        self.assertEqual(
            CommunityContribution.objects.filter(user=self.seller, kind='gift_completed').values_list('points', flat=True).get(),
            8,
        )
        self.assertEqual(
            CommunityContribution.objects.filter(user=self.buyer, kind='gift_completed').values_list('points', flat=True).get(),
            8,
        )

    def test_same_user_cannot_submit_two_pending_applications(self):
        item = Item.objects.create(
            title='免费收纳盒', description='整洁宿舍', trade_mode='free', price='0.00',
            category=self.category, location=self.location, condition='8成新', seller=self.seller,
        )
        GiftApplication.objects.create(item=item, applicant=self.buyer)
        self.client.login(username='gift-buyer', password='safe-password-123')

        response = self.client.post(
            reverse('create_order', args=[item.pk]),
            {'meeting_location': self.location.pk, 'buyer_note': '再次申请'},
        )

        self.assertRedirects(response, reverse('my_gift_applications'))
        self.assertEqual(GiftApplication.objects.filter(item=item, applicant=self.buyer, status='pending').count(), 1)
