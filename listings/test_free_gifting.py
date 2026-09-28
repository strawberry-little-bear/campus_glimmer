from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import ItemForm
from .models import Category, CampusLocation, Item, Notification, Order


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

    def test_free_item_uses_gifting_language_and_zero_value_order(self):
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

        order = Order.objects.get(item=item)
        self.assertRedirects(response, reverse('order_detail', args=[order.pk]))
        self.assertEqual(order.agreed_price, Decimal('0.00'))
        self.assertTrue(Notification.objects.filter(
            recipient=self.seller, kind='order_created', title='收到新的领取申请',
        ).exists())
        self.assertContains(self.client.get(reverse('order_detail', args=[order.pk])), '免费领取')
