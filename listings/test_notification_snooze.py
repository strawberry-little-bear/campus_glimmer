from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Notification
from .notifications import normalize_notification_ids


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


class NotificationBulkSnoozeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='bulk-snooze-user', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='bulk-snooze-other', password='safe-password-123',
        )
        self.client.force_login(self.user)

    def create_notification(self, *, recipient=None, **overrides):
        values = {
            'recipient': recipient or self.user,
            'kind': 'message_received',
            'title': '批量操作测试通知',
            'message': '用于验证批量延后与恢复。',
        }
        values.update(overrides)
        return Notification.objects.create(**values)

    def test_bulk_snooze_updates_only_owned_actionable_unread_notifications(self):
        first = self.create_notification()
        second = self.create_notification(title='第二条通知')
        already_read = self.create_notification(is_read=True, title='已经读过')
        future = self.create_notification(
            snoozed_until=timezone.now() + timedelta(days=1),
            title='已经延后',
        )
        other_users = self.create_notification(recipient=self.other, title='他人通知')
        future_deadline = future.snoozed_until
        now = timezone.now()

        response = self.client.post(reverse('snooze_selected_notifications'), {
            'notification_ids': [
                first.id, second.id, first.id, already_read.id,
                future.id, other_users.id, 'not-an-id',
            ],
            'duration': '2h',
            'next': reverse('notification_list') + '?status=unread',
        })

        self.assertRedirects(response, reverse('notification_list') + '?status=unread')
        for notification in (first, second):
            notification.refresh_from_db()
            self.assertIsNotNone(notification.snoozed_until)
            self.assertGreater(notification.snoozed_until, now + timedelta(hours=1))
        already_read.refresh_from_db()
        future.refresh_from_db()
        other_users.refresh_from_db()
        self.assertIsNone(already_read.snoozed_until)
        self.assertEqual(future.snoozed_until, future_deadline)
        self.assertIsNone(other_users.snoozed_until)
        summary = self.client.get(reverse('unread_summary')).json()
        self.assertEqual(summary['notifications'], 0)

    def test_bulk_snooze_accepts_each_whitelisted_duration(self):
        durations = (
            ('2h', timedelta(hours=2)),
            ('24h', timedelta(hours=24)),
            ('3d', timedelta(days=3)),
        )
        for duration, expected_delay in durations:
            with self.subTest(duration=duration):
                notification = self.create_notification(title=f'时长测试 {duration}')
                before = timezone.now()
                response = self.client.post(
                    reverse('snooze_selected_notifications'),
                    {'notification_ids': [notification.id], 'duration': duration},
                )
                after = timezone.now()

                self.assertRedirects(response, reverse('notification_list'))
                notification.refresh_from_db()
                self.assertGreaterEqual(notification.snoozed_until, before + expected_delay)
                self.assertLessEqual(notification.snoozed_until, after + expected_delay)

    def test_bulk_snooze_rejects_invalid_duration_get_and_external_redirect(self):
        notification = self.create_notification()

        get_response = self.client.get(reverse('snooze_selected_notifications'))
        self.assertEqual(get_response.status_code, 405)

        response = self.client.post(reverse('snooze_selected_notifications'), {
            'notification_ids': [notification.id],
            'duration': 'forever',
            'next': reverse('notification_list'),
        })
        self.assertRedirects(response, reverse('notification_list'))
        notification.refresh_from_db()
        self.assertIsNone(notification.snoozed_until)

        response = self.client.post(reverse('snooze_selected_notifications'), {
            'notification_ids': [notification.id],
            'duration': '24h',
            'next': 'https://evil.example/phishing',
        })
        self.assertRedirects(response, reverse('notification_list'))
        notification.refresh_from_db()
        self.assertIsNotNone(notification.snoozed_until)

    def test_bulk_restore_only_returns_still_snoozed_owned_unread_notifications(self):
        still_snoozed = self.create_notification(
            snoozed_until=timezone.now() + timedelta(hours=2),
        )
        already_due = self.create_notification(
            snoozed_until=timezone.now() - timedelta(minutes=1),
        )
        read_with_deadline = self.create_notification(
            is_read=True,
            snoozed_until=timezone.now() + timedelta(hours=2),
        )
        other_snoozed = self.create_notification(
            recipient=self.other,
            snoozed_until=timezone.now() + timedelta(hours=2),
        )
        due_deadline = already_due.snoozed_until
        read_deadline = read_with_deadline.snoozed_until
        other_deadline = other_snoozed.snoozed_until

        response = self.client.post(reverse('restore_selected_notifications'), {
            'notification_ids': [
                still_snoozed.id, already_due.id, read_with_deadline.id,
                other_snoozed.id, 'invalid',
            ],
            'next': reverse('notification_list') + '?status=snoozed',
        })

        self.assertRedirects(response, reverse('notification_list') + '?status=snoozed')
        still_snoozed.refresh_from_db()
        already_due.refresh_from_db()
        read_with_deadline.refresh_from_db()
        other_snoozed.refresh_from_db()
        self.assertIsNone(still_snoozed.snoozed_until)
        self.assertFalse(still_snoozed.is_read)
        self.assertEqual(already_due.snoozed_until, due_deadline)
        self.assertEqual(read_with_deadline.snoozed_until, read_deadline)
        self.assertEqual(other_snoozed.snoozed_until, other_deadline)
        self.assertEqual(self.client.get(reverse('unread_summary')).json()['notifications'], 2)

    def test_normalize_notification_ids_deduplicates_valid_ids_and_caps_batch(self):
        raw_ids = ['invalid', '1', '1', '0', '-2'] + [str(value) for value in range(2, 205)]

        notification_ids = normalize_notification_ids(raw_ids)

        self.assertEqual(notification_ids[0], 1)
        self.assertEqual(len(notification_ids), 200)
        self.assertEqual(len(set(notification_ids)), 200)

    def test_notification_center_renders_filter_specific_bulk_actions(self):
        self.create_notification(snoozed_until=timezone.now() + timedelta(hours=1))

        snoozed = self.client.get(reverse('notification_list'), {'status': 'snoozed'})
        self.assertContains(snoozed, '恢复所选')
        self.assertContains(snoozed, reverse('restore_selected_notifications'))
        self.assertNotContains(snoozed, '延后所选')
        self.assertContains(snoozed, 'status=snoozed')

        unread = self.client.get(reverse('notification_list'), {'status': 'unread'})
        self.assertContains(unread, '延后所选')
        self.assertContains(unread, reverse('snooze_selected_notifications'))
        self.assertNotContains(unread, '恢复所选')
