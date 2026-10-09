from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .lifecycle_diagnostics import (
    MAX_REFRESH_PER_MONTH,
    build_seller_lifecycle,
    diagnose_item,
    listings_due_for_attention_reminder,
    listings_due_for_price_drop_reminder,
)
from .models import BrowsingHistory, Category, CampusLocation, Item, Notification, Order


class ListingLifecycleTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='life-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='life-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='生命周期分类')
        self.location = CampusLocation.objects.create(name='东门')

    def make_item(self, **kwargs):
        defaults = {
            'title': '生命周期商品',
            'description': '用于测试生命周期诊断',
            'price': '35.00',
            'category': self.category,
            'location': self.location,
            'condition': '9成新',
            'seller': self.seller,
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def aged(self, item, days):
        """Move the listing's publish time back without touching refresh state."""
        stamp = timezone.now() - timedelta(days=days)
        Item.objects.filter(pk=item.pk).update(created_at=stamp)
        item.refresh_from_db()
        return item

    def test_fresh_listing_reports_fresh_level_with_reason(self):
        item = self.make_item()

        row = diagnose_item(item)

        self.assertEqual(row.level, 'fresh')
        self.assertEqual(row.level_label, '新鲜在售')
        self.assertEqual(row.tone, 'positive')
        self.assertFalse(row.needs_attention)
        self.assertTrue(any('新鲜期' in reason for reason in row.reasons))
        self.assertTrue(row.actions)

    def test_active_level_between_fresh_and_slowing(self):
        item = self.aged(self.make_item(), 10)

        row = diagnose_item(item)

        self.assertEqual(row.level, 'active')
        self.assertEqual(row.age_days, 10)

    def test_slowing_after_21_days_and_stale_after_45_days(self):
        slowing = diagnose_item(self.aged(self.make_item(title='放缓'), 21))
        stale = diagnose_item(self.aged(self.make_item(title='沉底'), 45))

        self.assertEqual(slowing.level, 'slowing')
        self.assertTrue(slowing.needs_attention)
        self.assertEqual(stale.level, 'stale')
        self.assertTrue(stale.needs_attention)

    def test_refresh_resets_the_age_used_by_the_diagnosis(self):
        item = self.aged(self.make_item(), 60)
        self.assertEqual(diagnose_item(item).level, 'stale')

        now = timezone.now()
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=now, refresh_count=1, updated_at=now,
        )
        item.refresh_from_db()

        row = diagnose_item(item)
        self.assertEqual(row.level, 'fresh')
        self.assertEqual(row.refresh_count, 1)
        self.assertEqual(row.age_days, 0)

    def test_expiring_listing_is_flagged_before_deadline(self):
        item = self.make_item(expires_at=timezone.now() + timedelta(days=2, hours=1))

        row = diagnose_item(item)

        self.assertEqual(row.level, 'expiring')
        self.assertEqual(row.days_until_expiry, 3)
        self.assertTrue(row.needs_attention)
        self.assertTrue(any('到期' in reason for reason in row.reasons))

    def test_sold_listing_is_archived_and_not_actionable(self):
        item = self.aged(self.make_item(status='sold'), 90)

        row = diagnose_item(item)

        self.assertEqual(row.level, 'archived')
        self.assertFalse(row.needs_attention)
        self.assertFalse(row.can_refresh)
        self.assertEqual(row.refresh_block_reason, '只有仍在展示的商品可以刷新。')

    def test_signals_are_aggregated_per_listing(self):
        first = self.make_item(title='有互动')
        second = self.make_item(title='无互动')
        BrowsingHistory.objects.create(user=self.buyer, item=first, view_count=12)
        Order.objects.create(
            item=second, buyer=self.buyer, seller=self.seller, agreed_price='10.00',
        )

        result = build_seller_lifecycle(self.seller)
        by_id = {row.item_id: row for row in result['diagnoses']}

        self.assertEqual(by_id[first.pk].signals.views, 12)
        self.assertEqual(by_id[first.pk].signals.favorites, 0)
        self.assertEqual(by_id[second.pk].signals.orders, 1)

    def test_seller_page_orders_attention_first(self):
        fresh = self.make_item(title='新鲜商品')
        stale = self.aged(self.make_item(title='沉底商品'), 50)
        expiring = self.make_item(title='临期商品', expires_at=timezone.now() + timedelta(days=1))

        result = build_seller_lifecycle(self.seller)

        levels = [row.level for row in result['diagnoses']]
        self.assertEqual(levels[0], 'expiring')
        self.assertIn('stale', levels)
        self.assertLess(levels.index('expiring'), levels.index('stale'))
        self.assertLess(levels.index('stale'), levels.index('fresh'))
        self.assertEqual(result['summary']['attention'], 2)
        self.assertEqual(result['summary']['total'], 3)

    def test_lifecycle_only_covers_own_listings(self):
        mine = self.make_item(title='我的商品')
        other = User.objects.create_user(username='other-seller', password='safe-password-123')
        Item.objects.create(
            title='别人的商品', description='不属于当前卖家', price='12.00',
            category=self.category, condition='8成新', seller=other,
        )

        result = build_seller_lifecycle(self.seller)

        self.assertEqual([row.item_id for row in result['diagnoses']], [mine.pk])


class RefreshListingTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='refresh-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='refresh-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='刷新分类')
        self.item = Item.objects.create(
            title='可刷新商品', description='测试刷新', price='20.00',
            category=self.category, condition='9成新', seller=self.seller,
        )
        self.url = reverse('refresh_item', args=[self.item.pk])

    def test_refresh_moves_listing_to_the_front_and_counts(self):
        Item.objects.filter(pk=self.item.pk).update(
            created_at=timezone.now() - timedelta(days=30),
        )
        self.client.login(username='refresh-seller', password='safe-password-123')

        response = self.client.post(self.url)

        self.assertRedirects(response, reverse('my_items'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.refresh_count, 1)
        self.assertIsNotNone(self.item.last_refreshed_at)
        self.assertEqual(diagnose_item(self.item).level, 'fresh')

    def test_second_refresh_within_a_day_is_rejected(self):
        self.client.login(username='refresh-seller', password='safe-password-123')

        self.client.post(self.url)
        response = self.client.post(self.url)

        self.assertRedirects(response, reverse('my_items'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.refresh_count, 1)

    def test_monthly_refresh_cap_is_enforced(self):
        Item.objects.filter(pk=self.item.pk).update(
            created_at=timezone.now() - timedelta(days=30),
            last_refreshed_at=timezone.now() - timedelta(days=2),
            refresh_count=MAX_REFRESH_PER_MONTH,
        )
        self.client.login(username='refresh-seller', password='safe-password-123')

        response = self.client.post(self.url)

        self.assertRedirects(response, reverse('my_items'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.refresh_count, MAX_REFRESH_PER_MONTH)
        # 上限未放开时，last_refreshed_at 保持为预设值，不会被本次请求改写。
        self.assertLess(self.item.last_refreshed_at, timezone.now())

    def test_only_the_owner_can_refresh_a_listing(self):
        self.client.login(username='refresh-buyer', password='safe-password-123')

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.item.refresh_from_db()
        self.assertEqual(self.item.refresh_count, 0)
        self.assertIsNone(self.item.last_refreshed_at)

    def test_refresh_requires_post(self):
        self.client.login(username='refresh-seller', password='safe-password-123')

        response = self.client.get(self.url)

        self.assertRedirects(response, reverse('my_items'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.refresh_count, 0)


class SellerLifecyclePageTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='page-seller', password='safe-password-123')
        self.category = Category.objects.create(name='页面分类')

    def test_my_items_page_shows_diagnosis_and_refresh_action(self):
        stale = Item.objects.create(
            title='沉底商品', description='发布很久了', price='25.00',
            category=self.category, condition='8成新', seller=self.seller,
        )
        Item.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(days=60))
        self.client.login(username='page-seller', password='safe-password-123')

        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '商品生命周期诊断')
        self.assertContains(response, '沉底较久')
        self.assertContains(response, reverse('refresh_item', args=[stale.pk]))

    def test_my_items_page_hides_panel_without_listings(self):
        self.client.login(username='page-seller', password='safe-password-123')

        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '商品生命周期诊断')

    def test_page_requires_authentication(self):
        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)


class LifecycleReminderTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='remind-seller', password='safe-password-123')
        self.category = Category.objects.create(name='提醒分类')

    def aged_item(self, title, days, **kwargs):
        defaults = {
            'title': title,
            'description': '提醒测试',
            'price': '30.00',
            'category': self.category,
            'condition': '9成新',
            'seller': self.seller,
        }
        defaults.update(kwargs)
        item = Item.objects.create(**defaults)
        Item.objects.filter(pk=item.pk).update(created_at=timezone.now() - timedelta(days=days))
        item.refresh_from_db()
        return item

    def test_stale_listing_is_due_for_attention_reminder(self):
        stale = self.aged_item('沉底待提醒', 50)
        self.aged_item('还新鲜', 2)

        due = listings_due_for_attention_reminder()

        self.assertEqual([item.pk for item, _signals in due], [stale.pk])

    def test_attention_reminder_respects_cooldown(self):
        item = self.aged_item('已提醒过', 50)
        Item.objects.filter(pk=item.pk).update(
            attention_reminder_sent_at=timezone.now() - timedelta(days=1),
        )

        self.assertEqual(listings_due_for_attention_reminder(), [])

        Item.objects.filter(pk=item.pk).update(
            attention_reminder_sent_at=timezone.now() - timedelta(days=10),
        )
        self.assertEqual(len(listings_due_for_attention_reminder()), 1)

    def test_price_drop_reminder_skips_items_with_orders_and_free_items(self):
        stale_sale = self.aged_item('久未成交', 60)
        with_order = self.aged_item('已有预约', 60)
        free_item = self.aged_item('免费赠送', 60, trade_mode='free', price='0.00')
        buyer = User.objects.create_user(username='remind-buyer', password='safe-password-123')
        Order.objects.create(
            item=with_order, buyer=buyer, seller=self.seller, agreed_price='30.00',
        )

        due = listings_due_for_price_drop_reminder()

        self.assertEqual([item.pk for item, _signals in due], [stale_sale.pk])
        self.assertNotIn(free_item.pk, [item.pk for item, _signals in due])
        self.assertNotIn(with_order.pk, [item.pk for item, _signals in due])

    def test_price_drop_reminder_respects_cooldown(self):
        item = self.aged_item('降价提醒过', 60)
        Item.objects.filter(pk=item.pk).update(
            price_drop_reminder_sent_at=timezone.now() - timedelta(days=3),
        )

        self.assertEqual(listings_due_for_price_drop_reminder(), [])

        Item.objects.filter(pk=item.pk).update(
            price_drop_reminder_sent_at=timezone.now() - timedelta(days=20),
        )
        self.assertEqual(len(listings_due_for_price_drop_reminder()), 1)
