from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusCampaign, Category, Item


class BulkCampaignAssignmentTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='bulk-seller', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='other-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='批量专题分类')
        self.campaign = CampusCampaign.objects.create(
            title='教材交换周',
            slug='textbook-week',
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=5),
        )

    def _item(self, seller, title):
        return Item.objects.create(
            title=title,
            description='批量专题测试商品',
            price='8.00',
            category=self.category,
            condition='良好',
            seller=seller,
        )

    def test_owner_can_assign_multiple_items_but_cannot_touch_another_users_item(self):
        first = self._item(self.seller, '第一本教材')
        second = self._item(self.seller, '第二本教材')
        foreign = self._item(self.other_user, '别人的教材')
        self.client.force_login(self.seller)

        response = self.client.post(reverse('bulk_assign_campaign'), {
            'item_ids': [first.id, second.id, foreign.id],
            'campaign_id': self.campaign.id,
        })

        self.assertRedirects(response, reverse('my_items'))
        first.refresh_from_db()
        second.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(first.campaign, self.campaign)
        self.assertEqual(second.campaign, self.campaign)
        self.assertIsNone(foreign.campaign)

    def test_owner_can_clear_campaign_assignment(self):
        item = self._item(self.seller, '待移出专题的教材')
        item.campaign = self.campaign
        item.save(update_fields=['campaign'])
        self.client.force_login(self.seller)

        self.client.post(reverse('bulk_assign_campaign'), {
            'item_ids': [item.id],
            'campaign_id': 'none',
        })

        item.refresh_from_db()
        self.assertIsNone(item.campaign)

    def test_my_items_renders_bulk_campaign_controls(self):
        self._item(self.seller, '我的教材')
        self.client.force_login(self.seller)

        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '批量加入校园专题')
        self.assertContains(response, '教材交换周')
