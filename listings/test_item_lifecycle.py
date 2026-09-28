from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .item_lifecycle import expire_items
from .models import Category, CampusLocation, Item, Notification


class ItemLifecycleTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='buyer', password='safe-password-123')
        self.category = Category.objects.create(name='教材')
        self.location = CampusLocation.objects.create(name='图书馆东门')

    def make_item(self, **kwargs):
        defaults = {
            'title': '考研资料',
            'description': '保存良好',
            'price': '20.00',
            'category': self.category,
            'location': self.location,
            'condition': '9成新',
            'seller': self.seller,
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_expired_listing_is_not_publicly_available(self):
        expired = self.make_item(title='已过期资料', expires_at=timezone.now() - timedelta(minutes=1))
        current = self.make_item(title='仍在展示资料', expires_at=timezone.now() + timedelta(days=1))

        self.assertFalse(Item.objects.available().filter(pk=expired.pk).exists())
        self.assertTrue(Item.objects.available().filter(pk=current.pk).exists())
        response = self.client.get(reverse('item_list'))
        self.assertNotContains(response, expired.title)
        self.assertContains(response, current.title)

    def test_expired_listing_cannot_start_an_order_before_maintenance_runs(self):
        item = self.make_item(expires_at=timezone.now() - timedelta(minutes=1))
        self.client.login(username='buyer', password='safe-password-123')

        response = self.client.get(reverse('create_order', args=[item.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(item.__class__.objects.filter(orders__buyer=self.buyer).exists())

    def test_expire_items_archives_and_notifies_once(self):
        now = timezone.now()
        item = self.make_item(expires_at=now - timedelta(minutes=1))
        self.make_item(title='长期展示', expires_at=None)

        first = expire_items(now=now)
        item.refresh_from_db()
        second = expire_items(now=now)

        self.assertEqual(first, {'expired': 1})
        self.assertEqual(second, {'expired': 0})
        self.assertEqual(item.status, 'expired')
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.seller, item=item, kind='item_expired',
            ).count(),
            1,
        )

    def test_expired_item_can_be_relisted_with_a_future_deadline(self):
        item = self.make_item(status='expired', expires_at=timezone.now() - timedelta(minutes=1))
        self.client.login(username='seller', password='safe-password-123')

        response = self.client.post(
            reverse('edit_item', args=[item.pk]),
            {
                'title': item.title,
                'description': item.description,
                'price': item.price,
                'category': self.category.pk,
                'location': self.location.pk,
                'condition': item.condition,
                'expires_at': (timezone.now() + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M'),
                'images-TOTAL_FORMS': '3',
                'images-INITIAL_FORMS': '0',
                'images-MIN_NUM_FORMS': '0',
                'images-MAX_NUM_FORMS': '5',
            },
        )

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.status, 'available')
        self.assertGreater(item.expires_at, timezone.now())
