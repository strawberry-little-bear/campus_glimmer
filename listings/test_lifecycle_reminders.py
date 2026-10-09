from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .lifecycle_reminders import MAX_LISTED_ITEMS, send_lifecycle_reminders
from .models import Category, CampusLocation, Item, Notification, NotificationPreference, Order


class LifecycleReminderTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='remind-seller', password='safe-password-123')
        self.other_seller = User.objects.create_user(username='remind-seller-2', password='safe-password-123')
        self.buyer = User.objects.create_user(username='remind-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='提醒分类')
        self.location = CampusLocation.objects.create(name='北门')

    def make_item(self, seller=None, **kwargs):
        defaults = {
            'title': '提醒商品',
            'description': '用于测试生命周期提醒',
            'price': '35.00',
            'category': self.category,
            'location': self.location,
            'condition': '9成新',
            'seller': seller or self.seller,
        }
        age_days = kwargs.pop('age_days', None)
        defaults.update(kwargs)
        item = Item.objects.create(**defaults)
        if age_days:
            # created_at 是 auto_now_add，只能整列回改，改完再刷新实例。
            Item.objects.filter(pk=item.pk).update(
                created_at=timezone.now() - timedelta(days=age_days),
            )
            item.refresh_from_db()
        return item

    def notifications(self, seller=None):
        return Notification.objects.filter(recipient=seller or self.seller)
    def test_closed_preference_blocks_notice(self):
        preference, _ = NotificationPreference.objects.get_or_create(user=self.seller)
        preference.lifecycle_reminder = False
        preference.save()
        self.make_item(age_days=50)

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['skipped_preference'], 1)
        self.assertEqual(self.notifications().count(), 0)

    def test_quiet_hours_delay_notice_without_consuming_cooldown(self):
        item = self.make_item(age_days=50)
        preference, _ = NotificationPreference.objects.get_or_create(user=self.seller)
        preference.quiet_hours_enabled = True
        preference.quiet_hours_start = timezone.now().astimezone().time().replace(tzinfo=None)
        preference.quiet_hours_end = (timezone.now().astimezone() + timedelta(hours=2)).time().replace(tzinfo=None)
        preference.save()

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['quiet'], 1)
        self.assertEqual(self.notifications().count(), 0)
        # 冷却字段没有被回写，免打扰结束后仍能收到提醒。
        item.refresh_from_db()
        self.assertIsNone(item.attention_reminder_sent_at)

    def test_many_listings_are_truncated_in_notice(self):
        for index in range(MAX_LISTED_ITEMS + 3):
            self.make_item(age_days=50, title=f'批量商品{index}')

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 1)
        notice = self.notifications().get(kind='lifecycle_reminder')
        self.assertIn(f'另有 {MAX_LISTED_ITEMS + 3 - MAX_LISTED_ITEMS} 件未列出', notice.message)
        self.assertIn(f'你有 {MAX_LISTED_ITEMS + 3} 件商品需要处理', notice.message)

    def test_sellers_are_notified_independently(self):
        self.make_item(seller=self.seller, age_days=50, title='卖家一的商品')
        self.make_item(seller=self.other_seller, age_days=50, title='卖家二的商品')

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 2)
        self.assertEqual(result['attention_sellers'], 2)
        self.assertEqual(Notification.objects.count(), 2)

    def test_seller_filter_only_reminds_given_users(self):
        self.make_item(seller=self.seller, age_days=50, title='卖家一的商品')
        self.make_item(seller=self.other_seller, age_days=50, title='卖家二的商品')

        result = send_lifecycle_reminders(seller_ids=[self.other_seller.pk])

        self.assertEqual(result['sent'], 1)
        self.assertEqual(self.notifications(seller=self.other_seller).count(), 1)
        self.assertEqual(self.notifications(seller=self.seller).count(), 0)

    def test_dry_run_reports_without_writing(self):
        item = self.make_item(age_days=50)

        result = send_lifecycle_reminders(dry_run=True)

        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['marked'], 0)
        self.assertEqual(self.notifications().count(), 0)
        item.refresh_from_db()
        self.assertIsNone(item.attention_reminder_sent_at)

    def test_management_command_runs_and_reports_counts(self):
        self.make_item(age_days=50)

        call_command('send_lifecycle_reminders')

        self.assertEqual(self.notifications().count(), 1)

    def test_management_command_dry_run_writes_nothing(self):
        self.make_item(age_days=50)

        call_command('send_lifecycle_reminders', '--dry-run')

        self.assertEqual(self.notifications().count(), 0)

    def test_fresh_listings_are_never_reminded(self):
        self.make_item(age_days=3, title='还很新鲜')

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 0)
        self.assertEqual(self.notifications().count(), 0)

    def test_archived_listings_are_never_reminded(self):
        self.make_item(age_days=50, title='已成交', status='sold')

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 0)
        self.assertEqual(self.notifications().count(), 0)

    def test_stale_listing_earns_attention_notice_and_marks_cooldown(self):
        item = self.make_item(age_days=50, title='沉底商品')

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['attention_sellers'], 1)
        self.assertEqual(result['marked'], 1)
        notice = self.notifications().get(kind='lifecycle_reminder')
        self.assertEqual(notice.title, '商品需要你处理')
        self.assertIn('沉底商品', notice.message)
        item.refresh_from_db()
        self.assertIsNotNone(item.attention_reminder_sent_at)
        self.assertEqual(notice.target_url, '/listings/my_items/')

    def test_second_run_same_day_sends_nothing(self):
        self.make_item(age_days=50)

        first = send_lifecycle_reminders()
        second = send_lifecycle_reminders()

        self.assertEqual(first['sent'], 1)
        self.assertEqual(second['sent'], 0)
        self.assertEqual(self.notifications().count(), 1)

    def test_cooldown_window_blocks_repeat_until_it_elapses(self):
        item = self.make_item(age_days=50)
        send_lifecycle_reminders()

        blocked = send_lifecycle_reminders()
        self.assertEqual(blocked['sent'], 0)

        stamp = timezone.now() - timedelta(days=8)
        Item.objects.filter(pk=item.pk).update(attention_reminder_sent_at=stamp)
        Notification.objects.all().delete()
        later = send_lifecycle_reminders()
        self.assertEqual(later['sent'], 1)

    def test_price_reminder_cites_comparable_range(self):
        for index in range(6):
            self.make_item(
                seller=self.other_seller,
                title=f'同类商品{index}',
                price='25.00',
            )
        item = self.make_item(age_days=60, title='定价偏高', price='200.00')

        result = send_lifecycle_reminders()

        self.assertEqual(result['price_sellers'], 1)
        notice = self.notifications().get(kind='lifecycle_reminder')
        self.assertIn('定价偏高', notice.message)
        self.assertIn('现价 ¥200.00', notice.message)
        self.assertIn('同类常见区间', notice.message)
        self.assertIn('¥22.50～¥27.50', notice.message)
        self.assertIn('最终定价仍由你决定', notice.message)
        item.refresh_from_db()
        self.assertIsNotNone(item.price_drop_reminder_sent_at)

    def test_price_reminder_skips_listings_priced_within_range(self):
        for index in range(6):
            self.make_item(
                seller=self.other_seller,
                title=f'同类商品{index}',
                price='25.00',
            )
        item = self.make_item(age_days=60, title='价格正常', price='26.00')

        result = send_lifecycle_reminders()

        # 商品本身仍会收到待处理提醒，但不该出现价格建议段落。
        self.assertEqual(result['price_sellers'], 0)
        notice = self.notifications().get(kind='lifecycle_reminder')
        self.assertIn('价格正常', notice.message)
        self.assertNotIn('久未成交', notice.message)
        item.refresh_from_db()
        self.assertIsNone(item.price_drop_reminder_sent_at)

    def test_price_reminder_skips_listings_without_comparables(self):
        item = self.make_item(age_days=60, title='孤品', price='200.00')

        result = send_lifecycle_reminders()

        self.assertEqual(result['price_sellers'], 0)
        notice = self.notifications().get(kind='lifecycle_reminder')
        self.assertNotIn('久未成交', notice.message)
        item.refresh_from_db()
        self.assertIsNone(item.price_drop_reminder_sent_at)

    def test_price_reminder_skips_free_and_ordered_listings(self):
        for index in range(6):
            self.make_item(
                seller=self.other_seller,
                title=f'同类商品{index}',
                price='25.00',
            )
        free_item = self.make_item(age_days=60, title='免费送', trade_mode='free', price='0.00')
        ordered = self.make_item(age_days=60, title='已有预约', price='200.00')
        Order.objects.create(
            item=ordered, buyer=self.buyer, seller=self.seller, agreed_price='200.00',
        )

        result = send_lifecycle_reminders()

        self.assertEqual(result['price_sellers'], 0)
        notice = self.notifications().get(kind='lifecycle_reminder')
        self.assertNotIn('久未成交', notice.message)
        free_item.refresh_from_db()
        ordered.refresh_from_db()
        self.assertIsNone(free_item.price_drop_reminder_sent_at)
        self.assertIsNone(ordered.price_drop_reminder_sent_at)

    def test_both_reminder_kinds_merge_into_one_notice(self):
        for index in range(6):
            self.make_item(
                seller=self.other_seller,
                title=f'同类商品{index}',
                price='25.00',
            )
        self.make_item(age_days=50, title='沉底待处理', price='30.00')
        self.make_item(age_days=60, title='价格偏高', price='200.00')

        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 1)
        self.assertEqual(self.notifications().count(), 1)
        notice = self.notifications().get(kind='lifecycle_reminder')
        self.assertIn('沉底待处理', notice.message)
        self.assertIn('价格偏高', notice.message)
        # 两件商品都同时满足待处理和价格两类条件，各回写两个冷却字段，
        # 因此是 2 件商品 × 2 个字段 = 4，而不是重复发送两条通知。
        self.assertEqual(result['marked'], 4)
