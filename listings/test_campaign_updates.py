from datetime import time, timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campaign_analytics import build_campaign_analytics
from .campaign_registration import join_campaign, leave_campaign
from .campaign_updates import (
    CampaignUpdateSubscriptionUnavailable,
    queue_campaign_update_matches,
    send_campaign_update_digest,
    set_campaign_update_preference,
)
from .forms import NotificationPreferenceForm
from .models import (
    CampusCampaign, CampaignRegistration, CampaignUpdateMatch, Category,
    Item, Notification, NotificationPreference, TaskRun,
)


class CampaignUpdateTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='campaign-update-seller', password='safe-password-123',
        )
        self.user = User.objects.create_user(
            username='campaign-update-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='专题摘要测试分类')
        self.campaign = self._campaign('毕业季上新专题', 'campaign-update-live')
        self.registration = CampaignRegistration.objects.create(
            campaign=self.campaign,
            user=self.user,
            joined_at=self.now - timedelta(hours=2),
            notify_updates=True,
            update_frequency='daily',
        )

    def _campaign(self, title, slug, **overrides):
        values = {
            'title': title,
            'slug': slug,
            'starts_at': self.now - timedelta(days=1),
            'ends_at': self.now + timedelta(days=10),
        }
        values.update(overrides)
        return CampusCampaign.objects.create(**values)

    def _item(self, campaign=None, *, title='专题新商品', **overrides):
        values = {
            'title': title,
            'description': '用于校园专题上新摘要测试',
            'price': '20.00',
            'category': self.category,
            'condition': '良好',
            'seller': self.seller,
            'campaign': campaign or self.campaign,
        }
        values.update(overrides)
        return Item.objects.create(**values)

    def _queue(self, *, title='专题新商品', campaign=None, now=None):
        item = self._item(campaign, title=title)
        queued = queue_campaign_update_matches(item, now=now or self.now)
        return item, queued

    def _preference(self):
        preference, _ = NotificationPreference.objects.get_or_create(user=self.user)
        return preference


class CampaignSubscriptionAndQueueTests(CampaignUpdateTestCase):
    def test_joining_a_campaign_does_not_automatically_subscribe(self):
        another_user = User.objects.create_user(
            username='campaign-update-joiner', password='safe-password-123',
        )

        result = join_campaign(another_user, self.campaign, now=self.now)

        self.assertTrue(result.changed)
        self.assertFalse(result.registration.notify_updates)
        self.assertEqual(result.registration.update_frequency, 'daily')

    def test_queue_is_idempotent_and_only_targets_opted_in_registrations(self):
        other_user = User.objects.create_user(
            username='campaign-update-unsubscribed', password='safe-password-123',
        )
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            user=other_user,
            joined_at=self.now - timedelta(hours=1),
        )
        item = self._item()

        first = queue_campaign_update_matches(item, now=self.now)
        second = queue_campaign_update_matches(item, now=self.now)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(CampaignUpdateMatch.objects.count(), 1)
        match = CampaignUpdateMatch.objects.get()
        self.assertEqual(match.registration, self.registration)
        self.assertEqual(match.item, item)

    def test_queue_skips_items_outside_a_live_campaign_or_not_available(self):
        ended = self._campaign(
            '已经结束的专题', 'campaign-update-ended',
            starts_at=self.now - timedelta(days=3),
            ends_at=self.now - timedelta(days=1),
        )
        sold = self._item(status='sold')
        expired = self._item(expires_at=self.now - timedelta(minutes=1))
        no_campaign = self._item()
        no_campaign.campaign = None
        no_campaign.save(update_fields=['campaign'])
        ended_item = self._item(ended)

        for item in (sold, expired, no_campaign, ended_item):
            self.assertEqual(queue_campaign_update_matches(item, now=self.now), 0)
        self.assertFalse(CampaignUpdateMatch.objects.exists())

    def test_closing_subscription_cancels_pending_matches(self):
        _, queued = self._queue()
        self.assertEqual(queued, 1)

        registration, changed = set_campaign_update_preference(
            self.user,
            self.campaign,
            notify_updates=False,
            update_frequency='weekly',
            now=self.now + timedelta(minutes=1),
        )

        self.assertTrue(changed)
        self.assertFalse(registration.notify_updates)
        self.assertEqual(registration.update_frequency, 'weekly')
        match = CampaignUpdateMatch.objects.get()
        self.assertIsNotNone(match.cancelled_at)
        self.assertIsNone(match.notified_at)

    def test_leaving_campaign_cancels_pending_matches(self):
        _, queued = self._queue()
        self.assertEqual(queued, 1)

        result = leave_campaign(
            self.user, self.campaign, now=self.now + timedelta(minutes=1),
        )

        self.assertTrue(result.changed)
        match = CampaignUpdateMatch.objects.get()
        self.assertIsNotNone(match.cancelled_at)
        self.assertFalse(CampaignRegistration.objects.get().is_active)

    def test_subscription_requires_an_active_registration_and_live_campaign(self):
        stranger = User.objects.create_user(
            username='campaign-update-stranger', password='safe-password-123',
        )
        with self.assertRaises(CampaignUpdateSubscriptionUnavailable):
            set_campaign_update_preference(
                stranger, self.campaign, notify_updates=True, now=self.now,
            )

        self.campaign.ends_at = self.now - timedelta(minutes=1)
        self.campaign.save(update_fields=['ends_at'])
        with self.assertRaises(CampaignUpdateSubscriptionUnavailable):
            set_campaign_update_preference(
                self.user, self.campaign, notify_updates=True, now=self.now,
            )


class CampaignUpdateDeliveryTests(CampaignUpdateTestCase):
    def test_digest_sends_once_and_marks_the_match_as_delivered(self):
        self._queue()

        first = send_campaign_update_digest(frequency='daily', now=self.now)
        second = send_campaign_update_digest(frequency='daily', now=self.now)

        self.assertEqual(first['sent'], 1)
        self.assertEqual(first['delivered_matches'], 1)
        self.assertEqual(second['sent'], 0)
        self.assertEqual(second['empty'], 1)
        notification = Notification.objects.get(kind='campaign_update')
        self.assertEqual(notification.target_url, reverse('my_campaigns'))
        self.assertEqual(Notification.objects.count(), 1)
        match = CampaignUpdateMatch.objects.get()
        self.assertIsNotNone(match.notified_at)
        self.assertEqual(match.notification, notification)

    def test_new_match_after_today_digest_waits_for_next_daily_period(self):
        self._queue(title='第一批上新')
        first = send_campaign_update_digest(frequency='daily', now=self.now)
        self.assertEqual(first['sent'], 1)

        later_item, queued = self._queue(
            title='第一批之后的新上架', now=self.now + timedelta(minutes=10),
        )
        same_period = send_campaign_update_digest(
            frequency='daily', now=self.now + timedelta(minutes=20),
        )
        next_period = send_campaign_update_digest(
            frequency='daily', now=self.now + timedelta(days=1),
        )

        self.assertEqual(queued, 1)
        self.assertEqual(same_period['sent'], 0)
        self.assertEqual(next_period['sent'], 1)
        self.assertEqual(Notification.objects.filter(kind='campaign_update').count(), 2)
        later_match = CampaignUpdateMatch.objects.get(item=later_item)
        self.assertIsNotNone(later_match.notified_at)

    def test_daily_and_weekly_digests_have_independent_periods(self):
        weekly_campaign = self._campaign('每周更新专题', 'campaign-update-weekly')
        CampaignRegistration.objects.create(
            campaign=weekly_campaign,
            user=self.user,
            joined_at=self.now - timedelta(hours=1),
            notify_updates=True,
            update_frequency='weekly',
        )
        self._queue(title='每日摘要商品')
        self._queue(title='每周摘要商品', campaign=weekly_campaign)

        daily = send_campaign_update_digest(frequency='daily', now=self.now)
        weekly = send_campaign_update_digest(frequency='weekly', now=self.now)
        daily_again = send_campaign_update_digest(frequency='daily', now=self.now)
        weekly_again = send_campaign_update_digest(frequency='weekly', now=self.now)

        self.assertEqual(daily['sent'], 1)
        self.assertEqual(weekly['sent'], 1)
        self.assertEqual(daily_again['sent'], 0)
        self.assertEqual(weekly_again['sent'], 0)
        keys = set(Notification.objects.filter(
            kind='campaign_update',
        ).values_list('dedupe_key', flat=True))
        self.assertEqual(len(keys), 2)
        self.assertTrue(any(':daily:' in key for key in keys))
        self.assertTrue(any(':weekly:' in key for key in keys))

    def test_matches_from_multiple_campaigns_merge_into_one_notification(self):
        second_campaign = self._campaign('教材流转专题', 'campaign-update-second')
        CampaignRegistration.objects.create(
            campaign=second_campaign,
            user=self.user,
            joined_at=self.now - timedelta(hours=1),
            notify_updates=True,
            update_frequency='daily',
        )
        self._queue(title='毕业季学士服')
        self._queue(title='二手教材合集', campaign=second_campaign)

        result = send_campaign_update_digest(frequency='daily', now=self.now)

        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['delivered_matches'], 2)
        notification = Notification.objects.get(kind='campaign_update')
        self.assertIn('毕业季上新专题', notification.message)
        self.assertIn('教材流转专题', notification.message)
        self.assertIn('毕业季学士服', notification.message)
        self.assertIn('二手教材合集', notification.message)
        self.assertEqual(
            CampaignUpdateMatch.objects.filter(notified_at__isnull=False).count(), 2,
        )

    def test_sold_reserved_expired_and_moved_items_are_cancelled_not_sent(self):
        sold = self._item(title='已售商品')
        reserved = self._item(title='已预订商品')
        expired = self._item(title='已过期商品')
        moved = self._item(title='已移出专题商品')
        for item in (sold, reserved, expired, moved):
            self.assertEqual(queue_campaign_update_matches(item, now=self.now), 1)

        Item.objects.filter(pk=sold.pk).update(status='sold')
        Item.objects.filter(pk=reserved.pk).update(status='reserved')
        Item.objects.filter(pk=expired.pk).update(
            expires_at=self.now - timedelta(minutes=1),
        )
        another_campaign = self._campaign('另一个专题', 'campaign-update-moved-to')
        Item.objects.filter(pk=moved.pk).update(campaign=another_campaign)

        result = send_campaign_update_digest(frequency='daily', now=self.now)

        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['invalid'], 4)
        self.assertFalse(Notification.objects.filter(kind='campaign_update').exists())
        self.assertEqual(
            CampaignUpdateMatch.objects.filter(cancelled_at__isnull=False).count(), 4,
        )

    def test_campaign_end_cancels_pending_matches_even_without_a_candidate(self):
        self._queue()
        CampusCampaign.objects.filter(pk=self.campaign.pk).update(
            ends_at=self.now - timedelta(minutes=1),
        )

        result = send_campaign_update_digest(frequency='daily', now=self.now)

        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['invalid'], 1)
        self.assertIsNotNone(CampaignUpdateMatch.objects.get().cancelled_at)

    def test_global_preference_off_keeps_pending_until_user_opts_back_in(self):
        self._queue()
        preference = self._preference()
        preference.campaign_update = False
        preference.save(update_fields=['campaign_update'])

        blocked = send_campaign_update_digest(frequency='daily', now=self.now)

        self.assertEqual(blocked['sent'], 0)
        self.assertEqual(blocked['skipped'], 1)
        self.assertIsNone(CampaignUpdateMatch.objects.get().cancelled_at)
        self.assertIsNone(CampaignUpdateMatch.objects.get().notified_at)
        self.assertIn(
            'campaign_update',
            NotificationPreferenceForm(instance=preference).fields,
        )

        preference.campaign_update = True
        preference.save(update_fields=['campaign_update'])
        delivered = send_campaign_update_digest(frequency='daily', now=self.now)
        self.assertEqual(delivered['sent'], 1)

    def test_quiet_hours_skip_delivery_without_cancelling_the_match(self):
        self._queue()
        preference = self._preference()
        current_hour = timezone.localtime(self.now).hour
        preference.quiet_hours_enabled = True
        preference.quiet_hours_start = time((current_hour - 1) % 24, 0)
        preference.quiet_hours_end = time((current_hour + 1) % 24, 0)
        preference.save(update_fields=[
            'quiet_hours_enabled', 'quiet_hours_start', 'quiet_hours_end',
        ])

        quiet = send_campaign_update_digest(frequency='daily', now=self.now)

        self.assertEqual(quiet['sent'], 0)
        self.assertEqual(quiet['quiet'], 1)
        self.assertIsNone(CampaignUpdateMatch.objects.get().notified_at)

        preference.quiet_hours_enabled = False
        preference.save(update_fields=['quiet_hours_enabled'])
        delivered = send_campaign_update_digest(frequency='daily', now=self.now)
        self.assertEqual(delivered['sent'], 1)

    def test_management_command_reports_and_records_task_run_metrics(self):
        self._queue()
        output = StringIO()

        call_command(
            'send_campaign_update_digest', '--frequency', 'daily', stdout=output,
        )

        self.assertIn('已发送 1 条专题上新摘要', output.getvalue())
        run = TaskRun.objects.get(name='send_campaign_update_digest')
        self.assertEqual(run.status, 'succeeded')
        self.assertEqual(run.metrics['sent'], 1)
        self.assertEqual(run.metrics['skipped'], 0)
        self.assertEqual(Notification.objects.filter(kind='campaign_update').count(), 1)


class CampaignSubscriptionViewTests(CampaignUpdateTestCase):
    def test_active_participant_can_change_frequency_and_opt_out(self):
        self.client.force_login(self.user)
        url = reverse('update_campaign_subscription', kwargs={'slug': self.campaign.slug})
        detail_url = reverse('campaign_detail', kwargs={'slug': self.campaign.slug})

        response = self.client.post(url, {
            'notify_updates': 'on',
            'update_frequency': 'weekly',
            'next': detail_url,
        })

        self.assertRedirects(response, detail_url)
        self.registration.refresh_from_db()
        self.assertTrue(self.registration.notify_updates)
        self.assertEqual(self.registration.update_frequency, 'weekly')

        response = self.client.post(url, {
            'update_frequency': 'daily',
            'next': detail_url,
        })
        self.assertRedirects(response, detail_url)
        self.registration.refresh_from_db()
        self.assertFalse(self.registration.notify_updates)

    def test_anonymous_and_get_requests_cannot_change_subscription(self):
        url = reverse('update_campaign_subscription', kwargs={'slug': self.campaign.slug})

        anonymous = self.client.post(url, {
            'notify_updates': 'on', 'update_frequency': 'weekly',
        })
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse('login'), anonymous['Location'])

        self.client.force_login(self.user)
        get_response = self.client.get(url)
        self.assertEqual(get_response.status_code, 405)
        self.registration.refresh_from_db()
        self.assertTrue(self.registration.notify_updates)
        self.assertEqual(self.registration.update_frequency, 'daily')


class CampaignUpdateAnalyticsTests(CampaignUpdateTestCase):
    def test_dashboard_and_csv_report_subscription_pending_and_read_metrics(self):
        self._queue()
        send_campaign_update_digest(frequency='daily', now=self.now)
        notification = Notification.objects.get(kind='campaign_update')
        Notification.objects.filter(pk=notification.pk).update(
            created_at=self.now - timedelta(minutes=1),
            is_read=True,
            read_at=self.now,
        )

        analytics = build_campaign_analytics(days=30, now=self.now)
        summary = analytics['summary']
        self.assertEqual(summary['current_subscription_count'], 1)
        self.assertEqual(summary['pending_update_match_count'], 0)
        self.assertEqual(summary['period_sent_digest_count'], 1)
        self.assertEqual(summary['period_read_digest_count'], 1)
        self.assertEqual(summary['digest_read_rate'], 100.0)

        admin = User.objects.create_superuser(
            username='campaign-update-admin',
            email='campaign-update@example.com',
            password='safe-password-123',
        )
        self.client.force_login(admin)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '当前摘要订阅')
        self.assertContains(response, '待发送命中')
        self.assertContains(response, '摘要阅读率')

        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(export.status_code, 200)
        text = export.content.decode('utf-8-sig')
        self.assertIn('专题当前摘要订阅', text)
        self.assertIn('专题待发送摘要命中', text)
        self.assertIn('专题摘要阅读率（%）', text)
        self.assertIn('毕业季上新专题', text)

