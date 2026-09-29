from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import CampusLocation, Category, CommunityContribution, DemandPost, DemandResponse, Item, Notification


class DemandResponseFlowTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(
            username='demand-requester', password='safe-password-123',
        )
        self.seller = User.objects.create_user(
            username='demand-seller', password='safe-password-123',
        )
        self.other_seller = User.objects.create_user(
            username='demand-other-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='响应测试分类')
        self.location = CampusLocation.objects.create(name='响应测试地点')
        self.demand = DemandPost.objects.create(
            requester=self.requester,
            title='求购高等数学教材',
            description='需要一本适合本学期使用的教材',
            category=self.category,
            location=self.location,
            min_price='20.00',
            max_price='80.00',
        )
        self.item = Item.objects.create(
            seller=self.seller,
            title='高等数学教材九成新',
            description='课本保存良好，适合本学期使用',
            category=self.category,
            location=self.location,
            condition='9成新',
            price='50.00',
        )

    def _respond(self, user=None, item=None, message='可以在图书馆门口交付。'):
        self.client.login(
            username=(user or self.seller).username,
            password='safe-password-123',
        )
        return self.client.post(
            reverse('respond_to_demand', args=[self.demand.id, (item or self.item).id]),
            {'message': message},
        )

    def test_seller_can_respond_once_and_requester_can_confirm(self):
        response = self._respond()
        self.assertRedirects(response, reverse('demand_detail', args=[self.demand.id]))
        demand_response = DemandResponse.objects.get(demand=self.demand, item=self.item)
        self.assertEqual(demand_response.status, 'pending')
        self.assertGreaterEqual(demand_response.match_score, 2)
        self.assertIn('地点一致', demand_response.match_reason)
        self.assertTrue(Notification.objects.filter(
            recipient=self.requester, kind='demand_response', demand=self.demand,
        ).exists())

        duplicate = self._respond()
        self.assertRedirects(duplicate, reverse('demand_detail', args=[self.demand.id]))
        self.assertEqual(DemandResponse.objects.filter(demand=self.demand, item=self.item).count(), 1)

        self.client.logout()
        self.client.login(username='demand-requester', password='safe-password-123')
        accepted = self.client.post(
            reverse('review_demand_response', args=[demand_response.id, 'accept']),
        )
        self.assertRedirects(accepted, reverse('demand_detail', args=[self.demand.id]))
        demand_response.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(demand_response.status, 'accepted')
        self.assertEqual(self.demand.status, 'fulfilled')
        self.assertTrue(Notification.objects.filter(
            recipient=self.seller, kind='demand_response', item=self.item,
            title='你的求购响应已被确认',
        ).exists())
        self.assertEqual(
            CommunityContribution.objects.filter(
                user=self.seller, kind='demand_helped',
            ).count(),
            1,
        )
        self.assertEqual(
            CommunityContribution.objects.get(
                user=self.seller, kind='demand_helped',
            ).points,
            8,
        )

        duplicate = self.client.post(
            reverse('review_demand_response', args=[demand_response.id, 'accept']),
        )
        self.assertRedirects(duplicate, reverse('demand_detail', args=[self.demand.id]))
        self.assertEqual(
            CommunityContribution.objects.filter(
                user=self.seller, kind='demand_helped',
            ).count(),
            1,
        )

    def test_accepting_one_response_rejects_other_pending_responses(self):
        other_item = Item.objects.create(
            seller=self.other_seller,
            title='高等数学教材另一本',
            description='同类教材',
            category=self.category,
            location=self.location,
            condition='8成新',
            price='40.00',
        )
        first = self._respond()
        self.assertEqual(first.status_code, 302)
        second = self._respond(user=self.other_seller, item=other_item, message='我也有一本。')
        self.assertEqual(second.status_code, 302)
        first_response = DemandResponse.objects.get(item=self.item, demand=self.demand)
        second_response = DemandResponse.objects.get(item=other_item, demand=self.demand)

        self.client.logout()
        self.client.login(username='demand-requester', password='safe-password-123')
        self.client.post(reverse('review_demand_response', args=[first_response.id, 'accept']))
        second_response.refresh_from_db()
        self.assertEqual(second_response.status, 'rejected')
        self.assertTrue(Notification.objects.filter(
            recipient=self.other_seller, kind='demand_response', item=other_item,
            title='求购响应未被采纳',
        ).exists())

    def test_non_matching_item_cannot_respond(self):
        other_location = CampusLocation.objects.create(name='其他响应地点')
        item = Item.objects.create(
            seller=self.seller,
            title='完全不同的商品',
            description='不符合求购条件',
            category=self.category,
            location=other_location,
            condition='全新',
            price='50.00',
        )
        response = self._respond(item=item)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DemandResponse.objects.filter(demand=self.demand, item=item).exists())

    def test_only_requester_can_review_response(self):
        self._respond()
        demand_response = DemandResponse.objects.get(demand=self.demand, item=self.item)
        self.client.logout()
        self.client.login(username='demand-seller', password='safe-password-123')
        response = self.client.post(
            reverse('review_demand_response', args=[demand_response.id, 'accept']),
        )
        self.assertRedirects(response, reverse('demand_detail', args=[self.demand.id]))
        demand_response.refresh_from_db()
        self.assertEqual(demand_response.status, 'pending')
