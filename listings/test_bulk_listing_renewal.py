from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, ItemRefreshEvent, Notification


class BulkListingRenewalTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='renewal-seller', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='renewal-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='批量续期分类')

    def _item(self, seller, title, *, status='available', expires_at=None):
        return Item.objects.create(
            title=title,
            description='批量续期测试商品',
            price='8.00',
            category=self.category,
            condition='良好',
            seller=seller,
            status=status,
            expires_at=expires_at,
        )

    def _datetime_value(self, value):
        return timezone.localtime(value).strftime('%Y-%m-%dT%H:%M')

    def test_login_is_required(self):
        response = self.client.post(reverse('bulk_renew_items'), {
            'item_ids': ['1'], 'expires_at': self._datetime_value(
                timezone.now() + timedelta(days=10)
            ),
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_get_returns_to_my_items(self):
        self.client.force_login(self.seller)

        response = self.client.get(reverse('bulk_renew_items'))

        self.assertRedirects(response, reverse('my_items'))

    def test_missing_selection_or_invalid_time_does_not_write(self):
        item = self._item(
            self.seller, '即将到期的教材', expires_at=timezone.now() + timedelta(days=2),
        )
        original_expiry = item.expires_at
        self.client.force_login(self.seller)

        missing_selection = self.client.post(reverse('bulk_renew_items'), {
            'expires_at': self._datetime_value(timezone.now() + timedelta(days=10)),
        })
        past_time = self.client.post(reverse('bulk_renew_items'), {
            'item_ids': [item.id],
            'expires_at': self._datetime_value(timezone.now() - timedelta(minutes=1)),
        })
        too_far = self.client.post(reverse('bulk_renew_items'), {
            'item_ids': [item.id],
            'expires_at': self._datetime_value(timezone.now() + timedelta(days=91)),
        })

        item.refresh_from_db()
        self.assertRedirects(missing_selection, reverse('my_items'))
        self.assertRedirects(past_time, reverse('my_items'))
        self.assertRedirects(too_far, reverse('my_items'))
        self.assertEqual(item.expires_at, original_expiry)

    def test_duplicate_selection_is_rejected(self):
        item = self._item(
            self.seller, '重复选择的教材', expires_at=timezone.now() + timedelta(days=2),
        )
        original_expiry = item.expires_at
        self.client.force_login(self.seller)

        response = self.client.post(reverse('bulk_renew_items'), {
            'item_ids': [item.id, item.id],
            'expires_at': self._datetime_value(timezone.now() + timedelta(days=10)),
        })

        item.refresh_from_db()
        self.assertRedirects(response, reverse('my_items'))
        self.assertEqual(item.expires_at, original_expiry)

    def test_renewal_rejects_another_sellers_item_without_partial_write(self):
        own_item = self._item(
            self.seller, '本人的教材', expires_at=timezone.now() + timedelta(days=2),
        )
        foreign_item = self._item(
            self.other_user, '别人的教材', expires_at=timezone.now() + timedelta(days=2),
        )
        own_expiry = own_item.expires_at
        foreign_expiry = foreign_item.expires_at
        self.client.force_login(self.seller)

        response = self.client.post(reverse('bulk_renew_items'), {
            'item_ids': [own_item.id, foreign_item.id],
            'expires_at': self._datetime_value(timezone.now() + timedelta(days=10)),
        })

        own_item.refresh_from_db()
        foreign_item.refresh_from_db()
        self.assertRedirects(response, reverse('my_items'))
        self.assertEqual(own_item.expires_at, own_expiry)
        self.assertEqual(foreign_item.expires_at, foreign_expiry)

    def test_reserved_sold_and_open_ended_items_block_the_whole_batch(self):
        expiring = self._item(
            self.seller, '可续期教材', expires_at=timezone.now() + timedelta(days=2),
        )
        blocked = [
            self._item(
                self.seller, '已预订教材', status='reserved',
                expires_at=timezone.now() + timedelta(days=2),
            ),
            self._item(
                self.seller, '已售出教材', status='sold',
                expires_at=timezone.now() + timedelta(days=2),
            ),
            self._item(self.seller, '长期展示教材'),
        ]
        original_expiries = [item.expires_at for item in [expiring, *blocked]]
        self.client.force_login(self.seller)

        response = self.client.post(reverse('bulk_renew_items'), {
            'item_ids': [expiring.id, *[item.id for item in blocked]],
            'expires_at': self._datetime_value(timezone.now() + timedelta(days=10)),
        })

        current_expiries = [
            Item.objects.get(pk=item.pk).expires_at
            for item in [expiring, *blocked]
        ]
        self.assertRedirects(response, reverse('my_items'))
        self.assertEqual(current_expiries, original_expiries)

    def test_owner_can_renew_available_and_expired_items_in_one_batch(self):
        available = self._item(
            self.seller, '即将到期教材', expires_at=timezone.now() + timedelta(days=2),
        )
        expired = self._item(
            self.seller, '已过期教材', status='expired',
            expires_at=timezone.now() - timedelta(days=1),
        )
        original_refreshed_at = timezone.now() - timedelta(days=2)
        available.last_refreshed_at = original_refreshed_at
        available.refresh_count = 3
        available.save(update_fields=['last_refreshed_at', 'refresh_count'])
        ItemRefreshEvent.objects.create(
            item=available,
            seller=self.seller,
            refreshed_at=available.last_refreshed_at,
            refresh_count_after=available.refresh_count,
        )
        new_expiry = (timezone.now() + timedelta(days=10)).replace(second=0, microsecond=0)
        self.client.force_login(self.seller)

        response = self.client.post(reverse('bulk_renew_items'), {
            'item_ids': [available.id, expired.id],
            'expires_at': self._datetime_value(new_expiry),
        })

        available.refresh_from_db()
        expired.refresh_from_db()
        self.assertRedirects(response, reverse('my_items'))
        self.assertEqual(available.status, 'available')
        self.assertEqual(expired.status, 'available')
        self.assertEqual(available.expires_at, new_expiry)
        self.assertEqual(expired.expires_at, new_expiry)
        self.assertEqual(available.last_refreshed_at, original_refreshed_at)
        self.assertEqual(available.refresh_count, 3)
        self.assertEqual(ItemRefreshEvent.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 0)

    def test_my_items_renders_shared_bulk_tools(self):
        self._item(
            self.seller, '批量工具教材', expires_at=timezone.now() + timedelta(days=2),
        )
        self.client.force_login(self.seller)

        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '批量管理勾选商品')
        self.assertContains(response, '应用专题')
        self.assertContains(response, '批量续期')
