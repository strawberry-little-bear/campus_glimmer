from datetime import date
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import NotificationPreferenceForm
from .models import CampusLocation, Category, DemandPost, Item, LostFoundPost, Notification, NotificationPreference
from .opportunity_digest import send_opportunity_digest


class OpportunityDigestTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='digest-owner', password='safe-password-123',
        )
        self.requester = User.objects.create_user(
            username='digest-requester', password='safe-password-123',
        )
        self.category = Category.objects.create(name='摘要测试分类')
        self.location = CampusLocation.objects.create(name='摘要测试地点')

    def create_item_opportunity(self):
        item = Item.objects.create(
            seller=self.owner,
            title='摘要测试教材',
            description='用于测试互助机会摘要',
            category=self.category,
            location=self.location,
            condition='八成新',
            price='20.00',
        )
        demand = DemandPost.objects.create(
            requester=self.requester,
            title='求购摘要测试教材',
            description='希望在测试地点附近找到教材',
            category=self.category,
            location=self.location,
            max_price='50.00',
        )
        return item, demand

    def create_lost_found_opportunity(self):
        lost = LostFoundPost.objects.create(
            reporter=self.owner,
            post_type='lost',
            title='遗失摘要测试物品',
            description='在测试地点附近遗失物品',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now(),
            identifying_features='蓝色外壳',
        )
        found = LostFoundPost.objects.create(
            reporter=self.requester,
            post_type='found',
            title='捡到摘要测试物品',
            description='在测试地点附近捡到物品',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now(),
            identifying_features='蓝色外壳',
        )
        return lost, found

    def test_digest_sends_for_matching_demand_and_links_to_opportunity_page(self):
        self.create_item_opportunity()

        result = send_opportunity_digest(digest_date=date(2026, 9, 29))

        self.assertEqual(result, {'sent': 1, 'skipped': 0, 'empty': 0})
        notification = Notification.objects.get(
            recipient=self.owner, kind='opportunity_digest',
        )
        self.assertEqual(notification.target_url, reverse('opportunity_feed'))
        self.assertIn('1 条可响应求购', notification.message)
        self.assertEqual(
            notification.dedupe_key,
            'opportunity-digest:2026-09-29:%s' % self.owner.pk,
        )

    def test_digest_sends_for_lost_found_match(self):
        self.create_lost_found_opportunity()

        result = send_opportunity_digest(digest_date=date(2026, 9, 29))

        self.assertEqual(result['sent'], 2)
        notification = Notification.objects.get(
            recipient=self.owner, kind='opportunity_digest',
        )
        self.assertIn('失物招领线索', notification.message)

    def test_digest_is_idempotent_for_same_user_and_date(self):
        self.create_item_opportunity()

        first = send_opportunity_digest(digest_date=date(2026, 9, 29))
        second = send_opportunity_digest(digest_date=date(2026, 9, 29))

        self.assertEqual(first['sent'], 1)
        self.assertEqual(second['sent'], 0)
        self.assertEqual(second['skipped'], 1)
        self.assertEqual(Notification.objects.filter(kind='opportunity_digest').count(), 1)

    def test_disabled_preference_prevents_digest_and_form_exposes_switch(self):
        self.create_item_opportunity()
        preference, _ = NotificationPreference.objects.get_or_create(user=self.owner)
        preference.opportunity_digest = False
        preference.save(update_fields=['opportunity_digest'])

        result = send_opportunity_digest(digest_date=date(2026, 9, 29))

        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.filter(
            recipient=self.owner, kind='opportunity_digest',
        ).exists())
        self.assertIn('opportunity_digest', NotificationPreferenceForm(instance=preference).fields)

    def test_users_without_opportunities_are_not_notified(self):
        result = send_opportunity_digest(digest_date=date(2026, 9, 29))

        self.assertEqual(result, {'sent': 0, 'skipped': 0, 'empty': 0})
        self.assertFalse(Notification.objects.filter(kind='opportunity_digest').exists())

    def test_management_command_reports_delivery_summary(self):
        self.create_item_opportunity()
        output = StringIO()

        call_command(
            'send_opportunity_digest',
            stdout=output,
        )

        self.assertIn('已发送 1 位用户的互助机会摘要', output.getvalue())
        self.assertIn('跳过 0 位', output.getvalue())
        self.assertEqual(Notification.objects.filter(kind='opportunity_digest').count(), 1)
