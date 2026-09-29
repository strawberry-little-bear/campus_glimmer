from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusLocation, Category, DemandPost, Item, LostFoundPost
from .opportunity_feed import build_opportunity_feed


class OpportunityFeedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='opportunity-owner', password='safe-password-123')
        self.other = User.objects.create_user(username='opportunity-other', password='safe-password-123')
        self.category = Category.objects.create(name='机会流测试分类')
        self.location = CampusLocation.objects.create(name='机会流测试地点')
        self.item = Item.objects.create(
            seller=self.user,
            title='高等数学教材',
            description='本学期使用过的教材',
            category=self.category,
            location=self.location,
            condition='八成新',
            price='30.00',
        )

    def test_feed_surfaces_matching_demand_and_lost_found_clue(self):
        demand = DemandPost.objects.create(
            requester=self.other,
            title='求购高等数学教材',
            description='希望在测试地点附近找到一本',
            category=self.category,
            location=self.location,
            max_price='50.00',
        )
        lost = LostFoundPost.objects.create(
            reporter=self.user,
            post_type='lost',
            title='遗失黑色高等数学教材',
            description='在测试地点附近遗失教材',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=2),
            identifying_features='黑色封面',
        )
        LostFoundPost.objects.create(
            reporter=self.other,
            post_type='found',
            title='捡到黑色高等数学教材',
            description='在测试地点捡到教材',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=1),
            identifying_features='黑色封面',
        )

        feed = build_opportunity_feed(self.user)

        self.assertEqual(feed['demand_count'], 1)
        self.assertEqual(feed['lost_found_count'], 1)
        self.assertEqual(feed['available_item_count'], 1)
        self.assertEqual({row['kind'] for row in feed['opportunities']}, {'demand', 'lost_found'})
        demand_row = next(row for row in feed['opportunities'] if row['kind'] == 'demand')
        lost_row = next(row for row in feed['opportunities'] if row['kind'] == 'lost_found')
        self.assertEqual(demand_row['target'], demand)
        self.assertEqual(demand_row['item'], self.item)
        self.assertIn('分类一致', demand_row['reason_text'])
        self.assertEqual(lost_row['source'], lost)
        self.assertIn('物品分类一致', lost_row['reason_text'])

    def test_feed_excludes_expired_or_self_owned_demands_and_requires_login(self):
        DemandPost.objects.create(
            requester=self.user,
            title='我的求购',
            description='不应该出现在可响应机会中',
            category=self.category,
            location=self.location,
        )
        DemandPost.objects.create(
            requester=self.other,
            title='已过期求购',
            description='已经过期',
            category=self.category,
            location=self.location,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        feed = build_opportunity_feed(self.user)
        self.assertEqual(feed['demand_count'], 0)
        response = self.client.get(reverse('opportunity_feed'))
        self.assertRedirects(response, reverse('login') + '?next=' + reverse('opportunity_feed'))

    def test_authenticated_page_renders_empty_state_and_navigation_link(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('opportunity_feed'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园互助机会')
        self.assertContains(response, '暂时没有合适的互助机会')
        self.assertContains(response, reverse('opportunity_feed'))

    def test_demand_opportunity_uses_post_form_for_direct_response(self):
        demand = DemandPost.objects.create(
            requester=self.other,
            title='求购高等数学教材',
            description='希望在测试地点附近找到一本',
            category=self.category,
            location=self.location,
            max_price='50.00',
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('opportunity_feed'))

        self.assertContains(
            response,
            f'action="{reverse("respond_to_demand", args=[demand.id, self.item.id])}"',
        )
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="message"')
    def test_feed_ignores_expired_lost_found_posts_and_deduplicates_targets(self):
        first_lost = LostFoundPost.objects.create(
            reporter=self.user,
            post_type='lost',
            title='遗失第一条教材记录',
            description='在测试地点附近遗失教材',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=3),
        )
        second_lost = LostFoundPost.objects.create(
            reporter=self.user,
            post_type='lost',
            title='遗失第二条教材记录',
            description='同一地点再次补充教材线索',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=2),
        )
        active_found = LostFoundPost.objects.create(
            reporter=self.other,
            post_type='found',
            title='捡到教材',
            description='在测试地点捡到教材',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=1),
        )
        LostFoundPost.objects.create(
            reporter=self.other,
            post_type='found',
            title='过期教材线索',
            description='这条线索不应再被推荐',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=1),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        expired_own_post = LostFoundPost.objects.create(
            reporter=self.user,
            post_type='lost',
            title='过期的我的记录',
            description='不应计入当前机会',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(days=2),
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        feed = build_opportunity_feed(self.user)

        self.assertEqual(feed['lost_found_count'], 1)
        self.assertEqual(feed['active_post_count'], 2)
        self.assertEqual(feed['opportunities'][0]['target'], active_found)
        self.assertIn(feed['opportunities'][0]['source'], {first_lost, second_lost})
        self.assertNotIn(expired_own_post, [row['source'] for row in feed['opportunities']])
