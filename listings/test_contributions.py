from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .contributions import build_contribution_summary, record_contribution, record_order_contribution
from .models import Category, CommunityContribution, Item, Order


class CommunityContributionTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='contribution-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='contribution-buyer', password='safe-password-123')
        category = Category.objects.create(name='贡献测试分类')
        item = Item.objects.create(
            title='贡献测试商品', description='用于测试贡献档案', price='20.00',
            category=category, condition='九成新', seller=self.seller, status='sold',
        )
        self.order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller, agreed_price='20.00', status='completed',
        )

    def test_order_contribution_is_idempotent_and_explainable(self):
        record_order_contribution(self.order, phase='trade_completed')
        record_order_contribution(self.order, phase='trade_completed')

        self.assertEqual(CommunityContribution.objects.count(), 2)
        seller_summary = build_contribution_summary(self.seller)
        buyer_summary = build_contribution_summary(self.buyer)
        self.assertEqual(seller_summary['total_points'], 10)
        self.assertEqual(buyer_summary['total_points'], 6)
        self.assertEqual(seller_summary['level'], '热心参与者')
        self.assertEqual(seller_summary['next_level'], '可靠交易伙伴')
        self.assertEqual(seller_summary['remaining_points'], 20)
        self.assertEqual(seller_summary['recent_events'][0].title, '完成一笔交易')
        badge_map = {badge['code']: badge for badge in seller_summary['badges']}
        self.assertTrue(badge_map['first_contribution']['earned'])
        self.assertFalse(badge_map['gift_giver']['earned'])
        self.assertEqual(badge_map['community_builder']['remaining'], 4)

    def test_delivery_completion_awards_both_parties_once(self):
        self.order.status = 'meeting'
        self.order.item.status = 'reserved'
        self.order.save(update_fields=['status', 'updated_at'])
        self.order.item.save(update_fields=['status', 'updated_at'])

        self.client.force_login(self.seller)
        self.client.post(reverse('confirm_delivery', args=[self.order.id]))
        self.client.force_login(self.buyer)
        self.client.post(reverse('confirm_delivery', args=[self.order.id]))

        self.assertEqual(
            CommunityContribution.objects.filter(kind='trade_completed').count(),
            2,
        )
        self.assertEqual(build_contribution_summary(self.seller)['total_points'], 10)
        self.assertEqual(build_contribution_summary(self.buyer)['total_points'], 6)

    def test_manual_contribution_appears_on_public_profile(self):
        record_contribution(
            user=self.seller,
            kind='lost_found_help',
            points=30,
            title='协助找回失物',
            description='帮助同学确认了失物线索。',
            source_key='lost-found:test-1',
        )

        response = self.client.get(reverse('public_profile', args=[self.seller.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园互助贡献')
        self.assertContains(response, '30 分')
        self.assertContains(response, '协助找回失物')
        self.assertContains(response, '失物线索员')
        self.assertContains(response, '已解锁')

    def test_profile_summary_has_progress_for_next_level(self):
        record_contribution(
            user=self.seller,
            kind='demand_helped',
            points=10,
            title='响应校园求购',
            source_key='demand:test-1',
        )
        response = self.client.get(reverse('profile'))
        self.assertRedirects(response, reverse('login') + '?next=' + reverse('profile'))

        self.client.force_login(self.seller)
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园互助贡献')
        self.assertContains(response, '距离“可靠交易伙伴”还差 20 分')

    def test_contribution_center_requires_login(self):
        response = self.client.get(reverse('contribution_center'))
        self.assertRedirects(response, reverse('login') + '?next=' + reverse('contribution_center'))

    def test_contribution_center_renders_level_badges_and_recent_events(self):
        record_contribution(
            user=self.seller,
            kind='demand_helped',
            points=10,
            title='响应校园求购',
            description='帮助同学找到需要的教材。',
            source_key='demand:center-test',
        )
        self.client.force_login(self.seller)
        response = self.client.get(reverse('contribution_center'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '我的互助贡献')
        self.assertContains(response, '热心参与者')
        self.assertContains(response, '迈出第一步')
        self.assertContains(response, '已解锁')
        self.assertContains(response, '响应校园求购')
        self.assertContains(response, '距离“可靠交易伙伴”还差 20 分')

