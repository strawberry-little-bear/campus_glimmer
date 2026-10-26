from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import NotificationPreferenceForm
from .models import Notification, NotificationPreference
from .notifications import active_unread_notifications, quiet_hours_active


class NotificationQuietHoursTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='quiet-hours-user', password='safe-password-123',
        )
        self.notification = Notification.objects.create(
            recipient=self.user,
            kind='message_received',
            title='收到一条私信',
            message='有人回复了你的商品留言。',
            target_url=reverse('notification_list'),
        )

    def test_cross_midnight_window_is_detected_without_hiding_stored_notifications(self):
        preference = NotificationPreference.objects.create(
            user=self.user,
            quiet_hours_enabled=True,
            quiet_hours_start=time(22, 0),
            quiet_hours_end=time(8, 0),
        )
        timezone_obj = timezone.get_current_timezone()
        late_night = timezone.make_aware(datetime(2026, 9, 29, 23, 0), timezone_obj)
        early_morning = timezone.make_aware(datetime(2026, 9, 30, 7, 30), timezone_obj)
        noon = timezone.make_aware(datetime(2026, 9, 29, 12, 0), timezone_obj)

        self.assertTrue(quiet_hours_active(self.user, now=late_night))
        self.assertTrue(quiet_hours_active(self.user, now=early_morning))
        self.assertFalse(quiet_hours_active(self.user, now=noon))
        self.assertEqual(active_unread_notifications(self.user, now=late_night).count(), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 1)
        preference.quiet_hours_enabled = False
        preference.save(update_fields=['quiet_hours_enabled'])
        self.assertEqual(active_unread_notifications(self.user, now=late_night).count(), 1)

    def test_daytime_window_is_detected(self):
        NotificationPreference.objects.create(
            user=self.user,
            quiet_hours_enabled=True,
            quiet_hours_start=time(9, 0),
            quiet_hours_end=time(17, 0),
        )
        timezone_obj = timezone.get_current_timezone()
        morning = timezone.make_aware(datetime(2026, 9, 29, 10, 0), timezone_obj)
        evening = timezone.make_aware(datetime(2026, 9, 29, 18, 0), timezone_obj)

        self.assertTrue(quiet_hours_active(self.user, now=morning))
        self.assertFalse(quiet_hours_active(self.user, now=evening))

    def test_unread_summary_silences_current_quiet_window_and_restores_afterwards(self):
        preference = NotificationPreference.objects.create(
            user=self.user,
            quiet_hours_enabled=True,
            quiet_hours_start=time(0, 0),
            quiet_hours_end=time(23, 59),
        )
        self.client.force_login(self.user)
        quiet_summary = self.client.get(reverse('unread_summary')).json()
        self.assertEqual(quiet_summary['notifications'], 0)

        preference.quiet_hours_enabled = False
        preference.save(update_fields=['quiet_hours_enabled'])
        active_summary = self.client.get(reverse('unread_summary')).json()
        self.assertEqual(active_summary['notifications'], 1)

    def test_summary_counts_only_notification_switches_and_tracks_quiet_hours(self):
        notification_fields = [
            field_name
            for field_name in NotificationPreferenceForm.base_fields
            if not field_name.startswith('quiet_hours_')
        ]
        self.assertEqual(len(notification_fields), 27)

        self.client.force_login(self.user)
        response = self.client.post(reverse('notification_preferences'), {
            'quiet_hours_enabled': 'on',
            'quiet_hours_start': '22:00',
            'quiet_hours_end': '08:00',
        })
        self.assertRedirects(response, reverse('notification_preferences'))

        preference = NotificationPreference.objects.get(user=self.user)
        self.assertTrue(preference.quiet_hours_enabled)
        self.assertFalse(any(getattr(preference, field_name) for field_name in notification_fields))

        response = self.client.get(reverse('notification_preferences'))
        self.assertEqual(response.context['notification_kind_count'], 27)
        self.assertEqual(response.context['enabled_notification_count'], 0)
        self.assertTrue(response.context['quiet_hours_enabled'])
        self.assertContains(response, '当前已开启 0 / 27 类提醒')
        self.assertContains(response, '免打扰时段：已开启')

    def test_preference_form_rejects_identical_enabled_times(self):
        preference = NotificationPreference.objects.create(user=self.user)
        form_data = {
            field.name: getattr(preference, field.name)
            for field in NotificationPreference._meta.fields
            if field.name not in {'id', 'user', 'updated_at'}
        }
        form_data.update({
            'quiet_hours_enabled': 'on',
            'quiet_hours_start': '22:00',
            'quiet_hours_end': '22:00',
        })
        self.client.force_login(self.user)
        response = self.client.post(reverse('notification_preferences'), form_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '免打扰开始时间和结束时间不能相同')
