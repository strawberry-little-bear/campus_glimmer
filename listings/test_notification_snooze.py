from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Notification


class NotificationSnoozeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='snooze-user', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='snooze-other', password='safe-password-123',
        )
        self.notification = Notification.objects.create(
            recipient=self.user,
            kind='message_received',
            title='收到一条私信',
            message='有同学回复了你的商品留言。',
            target_url=reverse('notification_list'),
        )

    def test_snooze_endpoint_hides_notification_from_unread_summary(self):
        self.client.force_login(self.user)
        before = timezone.now() + timedelta(hours=1)

        response = self.client.post(
            reverse('snooze_notification', args=[self.notification.id]),
            {'duration': '2h', 'next': reverse('notification_list')},
        )

        self.assertRedirects(response, reverse('notification_list'))
        self.notification.refresh_from_db()
        self.assertGreaterEqual(self.notification.snoozed_until, before)
        summary = self.client.get(reverse('unread_summary')).json()
        self.assertEqual(summary['notifications'], 0)
        self.assertEqual(summary['messages'], 0)

    def test_snoozed_notification_reappears_after_deadline(self):
        self.notification.snoozed_until = timezone.now() + timedelta(hours=1)
        self.notification.save(update_fields=['snoozed_until'])
        self.client.force_login(self.user)

        hidden = self.client.get(reverse('notification_list'), {'status': 'unread'})
        snoozed = self.client.get(reverse('notification_list'), {'status': 'snoozed'})
        summary = self.client.get(reverse('unread_summary')).json()

        self.assertNotContains(hidden, '收到一条私信')
        self.assertContains(snoozed, '收到一条私信')
        self.assertContains(snoozed, '已延后至')
        self.assertEqual(summary['notifications'], 0)

        self.notification.snoozed_until = timezone.now() - timedelta(minutes=1)
        self.notification.save(update_fields=['snoozed_until'])
        visible = self.client.get(reverse('notification_list'), {'status': 'unread'})
        summary = self.client.get(reverse('unread_summary')).json()

        self.assertContains(visible, '收到一条私信')
        self.assertEqual(summary['notifications'], 1)

    def test_snooze_does_not_cross_user_boundary_or_change_read_notifications(self):
        self.client.force_login(self.other)
        response = self.client.post(
            reverse('snooze_notification', args=[self.notification.id]),
            {'duration': '3d'},
        )

        self.assertEqual(response.status_code, 404)
        self.notification.refresh_from_db()
        self.assertIsNone(self.notification.snoozed_until)

        self.notification.is_read = True
        self.notification.save(update_fields=['is_read'])
        self.client.force_login(self.user)
        self.client.post(
            reverse('snooze_notification', args=[self.notification.id]),
            {'duration': '3d'},
        )
        self.notification.refresh_from_db()
        self.assertIsNone(self.notification.snoozed_until)

    def test_activity_center_excludes_snoozed_notifications_from_unread_count(self):
        self.notification.snoozed_until = timezone.now() + timedelta(hours=1)
        self.notification.save(update_fields=['snoozed_until'])
        self.client.force_login(self.user)

        response = self.client.get(reverse('activity_center'), {'status': 'unread'})

        self.assertNotContains(response, '收到一条私信')
        self.assertContains(response, '0 条未读')
