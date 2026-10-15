from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import NotificationPreferenceForm
from .models import (
    CampusLocation, Category, Favorite, Item, ItemPriceChange, Notification,
    NotificationPreference,
)


class ItemPriceChangeTests(TestCase):
    """收藏降价提醒的写入、通知和边界。"""

    def setUp(self):
        self.seller = User.objects.create_user(username='price-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='price-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='价格轨迹分类')
        self.location = CampusLocation.objects.create(name='价格轨迹地点', building='主校区')
        self.item = Item.objects.create(
            title='降噪耳机',
            description='配件齐全',
            trade_mode='sale',
            price=Decimal('120.00'),
            category=self.category,
            location=self.location,
            condition='9成新',
            seller=self.seller,
            status='available',
        )

    def _edit_item(self, *, price='99.00', trade_mode='sale', **overrides):
        data = {
            'title': overrides.get('title', self.item.title),
            'description': overrides.get('description', self.item.description),
            'trade_mode': trade_mode,
            'price': price,
            'deposit_amount': overrides.get('deposit_amount', '0'),
            'borrow_days': overrides.get('borrow_days', '7'),
            'category': overrides.get('category', self.category.pk),
            'location': overrides.get('location', self.location.pk),
            'condition': overrides.get('condition', self.item.condition),
            'expires_at': overrides.get(
                'expires_at',
                (timezone.now() + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M'),
            ),
            'images-TOTAL_FORMS': '3',
            'images-INITIAL_FORMS': '0',
            'images-MIN_NUM_FORMS': '0',
            'images-MAX_NUM_FORMS': '5',
        }
        self.client.login(username=self.seller.username, password='safe-password-123')
        return self.client.post(reverse('edit_item', args=[self.item.pk]), data)

    def test_price_drop_records_trajectory_and_notifies_current_favoriter(self):
        Favorite.objects.create(user=self.buyer, item=self.item)

        response = self._edit_item(price='99.00')

        self.assertRedirects(response, reverse('item_detail', args=[self.item.pk]))
        self.item.refresh_from_db()
        change = ItemPriceChange.objects.get(item=self.item)
        self.assertEqual(change.old_price, Decimal('120.00'))
        self.assertEqual(change.new_price, Decimal('99.00'))
        self.assertEqual(change.changed_by, self.seller)
        notification = Notification.objects.get(
            recipient=self.buyer, kind='item_price_drop', item=self.item,
        )
        self.assertEqual(notification.actor, self.seller)
        self.assertEqual(
            notification.target_url, reverse('item_detail', args=[self.item.pk]),
        )
        self.assertIn('¥120.00', notification.message)
        self.assertIn('¥99.00', notification.message)
        self.assertIn('¥21.00', notification.message)
        self.assertIn(str(change.pk), notification.dedupe_key)

    def test_price_increase_keeps_trajectory_but_does_not_notify(self):
        Favorite.objects.create(user=self.buyer, item=self.item)

        self._edit_item(price='139.00')

        change = ItemPriceChange.objects.get(item=self.item)
        self.assertEqual(change.old_price, Decimal('120.00'))
        self.assertEqual(change.new_price, Decimal('139.00'))
        self.assertFalse(Notification.objects.filter(kind='item_price_drop').exists())

    def test_unchanged_price_does_not_write_empty_history(self):
        Favorite.objects.create(user=self.buyer, item=self.item)

        self._edit_item(price='120.00', description='只补充成色说明')

        self.assertFalse(ItemPriceChange.objects.exists())
        self.assertFalse(Notification.objects.filter(kind='item_price_drop').exists())

    def test_free_and_borrow_modes_do_not_enter_sale_price_history(self):
        Favorite.objects.create(user=self.buyer, item=self.item)
        self._edit_item(price='0.00', trade_mode='free')
        self.assertFalse(ItemPriceChange.objects.exists())

        self.item.refresh_from_db()
        self.item.trade_mode = 'sale'
        self.item.price = Decimal('120.00')
        self.item.save(update_fields=['trade_mode', 'price', 'updated_at'])
        self._edit_item(
            price='0.00', trade_mode='borrow', deposit_amount='30.00', borrow_days='14',
        )
        self.assertFalse(ItemPriceChange.objects.exists())
        self.assertFalse(Notification.objects.filter(kind='item_price_drop').exists())

    def test_expired_item_drop_is_kept_silent_until_it_is_available(self):
        self.item.status = 'expired'
        self.item.expires_at = None
        self.item.save(update_fields=['status', 'expires_at', 'updated_at'])
        Favorite.objects.create(user=self.buyer, item=self.item)

        self._edit_item(
            price='99.00',
            expires_at='',
        )

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'expired')
        self.assertTrue(ItemPriceChange.objects.filter(item=self.item).exists())
        self.assertFalse(Notification.objects.filter(kind='item_price_drop').exists())

    def test_relisting_with_future_deadline_allows_drop_notice(self):
        self.item.status = 'expired'
        self.item.expires_at = timezone.now() - timedelta(minutes=1)
        self.item.save(update_fields=['status', 'expires_at', 'updated_at'])
        Favorite.objects.create(user=self.buyer, item=self.item)

        response = self._edit_item(price='99.00')

        self.assertRedirects(response, reverse('item_detail', args=[self.item.pk]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'available')
        self.assertTrue(Notification.objects.filter(
            recipient=self.buyer, kind='item_price_drop', item=self.item,
        ).exists())

    def test_seller_who_favorites_own_item_is_not_notified(self):
        Favorite.objects.create(user=self.seller, item=self.item)
        Favorite.objects.create(user=self.buyer, item=self.item)

        self._edit_item(price='99.00')

        notifications = Notification.objects.filter(kind='item_price_drop')
        self.assertEqual(notifications.count(), 1)
        self.assertEqual(notifications.get().recipient, self.buyer)

    def test_preference_can_turn_off_price_drop_notice(self):
        disabled_user = User.objects.create_user(username='price-off', password='safe-password-123')
        Favorite.objects.create(user=disabled_user, item=self.item)
        Favorite.objects.create(user=self.buyer, item=self.item)
        NotificationPreference.objects.create(user=disabled_user, item_price_drop=False)

        self._edit_item(price='99.00')

        self.assertFalse(Notification.objects.filter(
            recipient=disabled_user, kind='item_price_drop',
        ).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.buyer, kind='item_price_drop',
        ).exists())

    def test_consecutive_drops_create_two_events_and_notices(self):
        Favorite.objects.create(user=self.buyer, item=self.item)

        self._edit_item(price='109.00')
        self._edit_item(price='99.00')

        changes = ItemPriceChange.objects.filter(item=self.item).order_by('pk')
        self.assertEqual(
            [(row.old_price, row.new_price) for row in changes],
            [
                (Decimal('120.00'), Decimal('109.00')),
                (Decimal('109.00'), Decimal('99.00')),
            ],
        )
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.buyer, kind='item_price_drop', item=self.item,
            ).count(),
            2,
        )
        self.assertEqual(
            Notification.objects.filter(kind='item_price_drop')
            .values('dedupe_key').distinct().count(),
            2,
        )

    def test_non_seller_cannot_trigger_the_edit_path(self):
        Favorite.objects.create(user=self.buyer, item=self.item)
        data = {
            'title': self.item.title, 'description': self.item.description,
            'trade_mode': 'sale', 'price': '99.00', 'deposit_amount': '0',
            'borrow_days': '7', 'category': self.category.pk,
            'location': self.location.pk, 'condition': self.item.condition,
            'expires_at': (timezone.now() + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M'),
            'images-TOTAL_FORMS': '3', 'images-INITIAL_FORMS': '0',
            'images-MIN_NUM_FORMS': '0', 'images-MAX_NUM_FORMS': '5',
        }
        self.client.login(username=self.buyer.username, password='safe-password-123')

        response = self.client.post(reverse('edit_item', args=[self.item.pk]), data)

        self.assertRedirects(response, reverse('item_detail', args=[self.item.pk]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.price, Decimal('120.00'))
        self.assertFalse(ItemPriceChange.objects.exists())
        self.assertFalse(Notification.objects.filter(kind='item_price_drop').exists())

    def test_notification_failure_does_not_rollback_committed_price_edit(self):
        Favorite.objects.create(user=self.buyer, item=self.item)

        with patch(
            'listings.price_changes.create_notification',
            side_effect=RuntimeError('notification storage unavailable'),
        ):
            response = self._edit_item(price='99.00')

        self.assertRedirects(response, reverse('item_detail', args=[self.item.pk]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.price, Decimal('99.00'))
        self.assertTrue(ItemPriceChange.objects.filter(item=self.item).exists())

    def test_kind_choice_preference_and_form_stay_aligned(self):
        choice_kinds = {kind for kind, _label in Notification.KIND_CHOICES}
        preference_kinds = {
            field.name for field in NotificationPreference._meta.get_fields()
            if getattr(field, 'concrete', False)
            and field.get_internal_type() == 'BooleanField'
            and field.name != 'quiet_hours_enabled'
        }
        form_kinds = set(NotificationPreferenceForm.Meta.fields) - {
            'quiet_hours_enabled', 'quiet_hours_start', 'quiet_hours_end',
        }
        self.assertIn('item_price_drop', choice_kinds)
        self.assertEqual(choice_kinds, preference_kinds)
        self.assertEqual(preference_kinds, form_kinds)
