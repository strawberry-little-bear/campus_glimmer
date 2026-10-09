from datetime import date, datetime, timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import NotificationPreferenceForm
from .models import (
    CampusLocation, Category, Item, Notification, NotificationPreference,
    SavedSearch, SavedSearchMatch,
)
from .saved_search_digest import send_saved_search_digest
from .saved_searches import (
    describe_saved_search_match, matches_saved_search, notify_saved_search_matches,
)


class SavedSearchCadenceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='cadence-owner', password='safe-password-123')
        self.seller = User.objects.create_user(username='cadence-seller', password='safe-password-123')
        self.category = Category.objects.create(name='节奏测试分类')
        self.location = CampusLocation.objects.create(name='节奏测试地点')
        self.saved_search = SavedSearch.objects.create(
            user=self.owner,
            name='节奏测试关注',
            query='考研',
            category=self.category,
            location=self.location,
            max_price='60.00',
        )

    def create_item(self, title='考研英语真题'):
        return Item.objects.create(
            seller=self.seller,
            title=title,
            description='适合复习使用的资料',
            category=self.category,
            location=self.location,
            condition='九成新',
            price='35.00',
        )

    def test_matches_saved_search_honours_every_criterion(self):
        item = self.create_item()
        self.assertTrue(matches_saved_search(self.saved_search, item))

        item.title = '高等数学教材'
        item.save()
        self.assertFalse(matches_saved_search(self.saved_search, item))

    def test_describe_match_lists_the_dimensions_that_fired(self):
        item = self.create_item()
        reasons = describe_saved_search_match(self.saved_search, item)

        self.assertTrue(any('考研' in reason for reason in reasons))
        self.assertTrue(any('节奏测试分类' in reason for reason in reasons))
        self.assertTrue(any('节奏测试地点' in reason for reason in reasons))

    def test_instant_saved_search_notifies_immediately(self):
        item = self.create_item()

        notify_saved_search_matches(item)

        notification = Notification.objects.get(
            recipient=self.owner, kind='saved_search_match',
        )
        self.assertIn('考研英语真题', notification.message)
        self.assertIn('节奏测试关注', notification.message)
        self.assertEqual(notification.dedupe_key, f'saved-search:{self.saved_search.pk}:{item.pk}')

    def test_daily_saved_search_buffers_instead_of_notifying(self):
        self.saved_search.notify_frequency = 'daily'
        self.saved_search.save(update_fields=['notify_frequency'])
        item = self.create_item()

        notify_saved_search_matches(item)

        self.assertFalse(Notification.objects.exists())
        self.assertEqual(SavedSearchMatch.objects.filter(saved_search=self.saved_search).count(), 1)

    def test_repeated_publish_of_same_item_does_not_duplicate_buffer(self):
        self.saved_search.notify_frequency = 'daily'
        self.saved_search.save(update_fields=['notify_frequency'])
        item = self.create_item()

        notify_saved_search_matches(item)
        notify_saved_search_matches(item)

        self.assertEqual(SavedSearchMatch.objects.filter(saved_search=self.saved_search).count(), 1)

    def test_digest_merges_buffered_hits_into_one_notice(self):
        self.saved_search.notify_frequency = 'daily'
        self.saved_search.max_matches_per_notice = 2
        self.saved_search.save(update_fields=['notify_frequency', 'max_matches_per_notice'])
        for title in ('考研英语真题', '考研政治讲义', '考研数学笔记'):
            notify_saved_search_matches(self.create_item(title))

        result = send_saved_search_digest(frequency='daily', today=date(2026, 9, 29))

        self.assertEqual(result['sent'], 1)
        notification = Notification.objects.get(recipient=self.owner, kind='saved_search_digest')
        self.assertIn('3', notification.message)
        self.assertIn('考研英语真题', notification.message)
        # max_matches_per_notice caps how many titles are listed.
        self.assertNotIn('考研数学笔记', notification.message)
        self.assertIn('另有 1 条未展示', notification.message)
        self.assertTrue(
            SavedSearchMatch.objects.filter(saved_search=self.saved_search, notified_at__isnull=False).exists()
        )

    def test_digest_is_idempotent_for_the_same_period(self):
        self.saved_search.notify_frequency = 'daily'
        self.saved_search.save(update_fields=['notify_frequency'])
        notify_saved_search_matches(self.create_item('考研英语真题'))

        first = send_saved_search_digest(frequency='daily', today=date(2026, 9, 29))
        second = send_saved_search_digest(frequency='daily', today=date(2026, 9, 29))

        self.assertEqual(first['sent'], 1)
        self.assertEqual(second['sent'], 0)
        self.assertEqual(Notification.objects.filter(kind='saved_search_digest').count(), 1)

    def test_weekly_cadence_uses_its_own_period_key(self):
        self.saved_search.notify_frequency = 'weekly'
        self.saved_search.save(update_fields=['notify_frequency'])
        notify_saved_search_matches(self.create_item('考研英语真题'))

        monday = send_saved_search_digest(frequency='weekly', today=date(2026, 9, 28))
        later = send_saved_search_digest(frequency='weekly', today=date(2026, 10, 1))

        self.assertEqual(monday['sent'], 1)
        self.assertEqual(later['sent'], 0)

    def test_quiet_saved_search_is_skipped_but_buffered(self):
        self.saved_search.notify_frequency = 'daily'
        self.saved_search.quiet_until = timezone.now() + timedelta(days=3)
        self.saved_search.save(update_fields=['notify_frequency', 'quiet_until'])
        item = self.create_item()

        notify_saved_search_matches(item)
        result = send_saved_search_digest(frequency='daily', today=date(2026, 9, 29))

        self.assertTrue(self.saved_search.is_quiet)
        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['quiet'], 1)
        self.assertEqual(SavedSearchMatch.objects.filter(saved_search=self.saved_search).count(), 1)

    def test_switching_to_instant_flushes_pending_hits(self):
        self.saved_search.notify_frequency = 'daily'
        self.saved_search.save(update_fields=['notify_frequency'])
        item = self.create_item()
        notify_saved_search_matches(item)

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('update_saved_search_cadence', args=[self.saved_search.pk]),
            {'notify_frequency': 'instant', 'max_matches_per_notice': '3'},
        )

        self.assertRedirects(response, reverse('saved_search_list'))
        self.saved_search.refresh_from_db()
        self.assertEqual(self.saved_search.notify_frequency, 'instant')
        self.assertFalse(SavedSearchMatch.objects.filter(notified_at__isnull=True).exists())
        self.assertTrue(
            Notification.objects.filter(recipient=self.owner, kind='saved_search_match').exists()
        )

    def test_cadence_view_rejects_invalid_limit(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('update_saved_search_cadence', args=[self.saved_search.pk]),
            {'notify_frequency': 'daily', 'max_matches_per_notice': '99'},
            follow=True,
        )

        self.saved_search.refresh_from_db()
        self.assertEqual(self.saved_search.notify_frequency, 'instant')
        self.assertContains(response, '1 到 9')

    def test_management_command_reports_counts(self):
        self.saved_search.notify_frequency = 'daily'
        self.saved_search.save(update_fields=['notify_frequency'])
        notify_saved_search_matches(self.create_item('考研英语真题'))

        out = StringIO()
        call_command('send_saved_search_digest', '--frequency', 'daily', stdout=out)

        self.assertIn('已发送 1 条', out.getvalue())


class SavedSearchPreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pref-owner', password='safe-password-123')

    def test_saved_search_digest_switch_is_editable(self):
        preference = NotificationPreference.objects.create(user=self.user)
        form = NotificationPreferenceForm(
            {'saved_search_digest': False}, instance=preference,
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        preference.refresh_from_db()
        self.assertFalse(preference.saved_search_digest)

    def test_digest_respects_the_global_switch(self):
        preference = NotificationPreference.objects.create(user=self.user, saved_search_digest=False)
        self.assertFalse(preference.saved_search_digest)
