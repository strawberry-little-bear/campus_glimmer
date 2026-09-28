from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .models import CampusLocation, Category, Item, Notification, NotificationPreference, Report
from .operations_digest import send_operations_digest


class OperationsDigestTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='ops-admin', password='safe-password-123', is_staff=True,
        )
        self.second_staff = User.objects.create_user(
            username='ops-reviewer', password='safe-password-123', is_staff=True,
        )
        self.member = User.objects.create_user(
            username='member', password='safe-password-123',
        )
        self.category = Category.objects.create(name='运营测试分类')
        self.location = CampusLocation.objects.create(name='运营测试地点')
        for index in range(3):
            item = Item.objects.create(
                title=f'待审核商品 {index}', description='测试商品', price='10.00',
                category=self.category, location=self.location, condition='全新',
                seller=self.member,
            )
            Report.objects.create(
                item=item, reporter=self.member, reason='spam', detail='需要运营复核',
            )

    def test_digest_reaches_active_staff_and_links_to_dashboard(self):
        result = send_operations_digest(days=7)

        self.assertEqual(result['sent'], 2)
        self.assertEqual(result['alerts'], 1)
        notifications = Notification.objects.filter(kind='operations_digest')
        self.assertEqual(notifications.count(), 2)
        notification = notifications.get(recipient=self.staff)
        self.assertEqual(notification.target_url, f"{reverse('operations_dashboard')}?days=7")
        self.assertIn('举报审核队列积压', notification.message)

    def test_digest_is_idempotent_for_same_day_and_period(self):
        first = send_operations_digest(days=7)
        second = send_operations_digest(days=7)

        self.assertEqual(first['sent'], 2)
        self.assertEqual(second['sent'], 0)
        self.assertEqual(second['skipped'], 2)
        self.assertEqual(Notification.objects.filter(kind='operations_digest').count(), 2)

    def test_disabled_preference_and_non_staff_are_not_notified(self):
        preference, _ = NotificationPreference.objects.get_or_create(user=self.staff)
        preference.operations_digest = False
        preference.save(update_fields=['operations_digest'])

        result = send_operations_digest(days=7)

        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.filter(
            recipient=self.staff, kind='operations_digest',
        ).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.second_staff, kind='operations_digest',
        ).exists())
        self.assertFalse(Notification.objects.filter(
            recipient=self.member, kind='operations_digest',
        ).exists())

    def test_management_command_reports_when_no_alert_exists(self):
        Report.objects.all().delete()
        output = StringIO()

        call_command('send_operations_digest', days=7, stdout=output)

        self.assertIn('没有需要发送的运营告警', output.getvalue())
        self.assertFalse(Notification.objects.filter(kind='operations_digest').exists())
