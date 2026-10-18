from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campaign_analytics import build_campaign_analytics
from .models import (
    BrowsingHistory,
    CampusCampaign,
    CampaignGoal,
    CampaignRegistration,
    Category,
    Favorite,
    Item,
    Order,
)


class CampaignGoalTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='campaign-goal-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='专题目标分类')
        self.campaign = CampusCampaign.objects.create(
            title='校园目标专题',
            slug='campaign-goal-report',
            starts_at=self.now - timedelta(days=10),
            ends_at=self.now + timedelta(days=10),
        )

    def _item(self, *, title, campaign=None, created_at=None):
        item = Item.objects.create(
            title=title,
            description='用于专题目标统计测试',
            price='20.00',
            category=self.category,
            condition='良好',
            seller=self.seller,
            campaign=campaign or self.campaign,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=created_at or (self.now - timedelta(days=3)),
        )
        return item

    def _view(self, item, *, index, viewed_at):
        user = User.objects.create_user(
            username=f'goal-viewer-{index}', password='safe-password-123',
        )
        history = BrowsingHistory.objects.create(user=user, item=item)
        BrowsingHistory.objects.filter(pk=history.pk).update(last_viewed_at=viewed_at)
        return user

    def _goal(self, metric, target=10, *, starts_at=None, ends_at=None, note=''):
        return CampaignGoal.objects.create(
            campaign=self.campaign,
            metric=metric,
            target_value=target,
            starts_at=starts_at or (self.now - timedelta(days=5)),
            ends_at=ends_at or (self.now + timedelta(days=5)),
            note=note,
        )

    def test_goal_recomputes_all_supported_metrics_in_its_own_window(self):
        item = self._item(title='目标窗口内商品')
        stale_item = self._item(
            title='目标开始前商品',
            created_at=self.now - timedelta(days=7),
        )
        self._view(item, index=1, viewed_at=self.now - timedelta(days=2))
        self._view(item, index=2, viewed_at=self.now - timedelta(days=1))
        self._view(stale_item, index=3, viewed_at=self.now - timedelta(days=7))

        favorite_user = User.objects.create_user(
            username='goal-favorite', password='safe-password-123',
        )
        favorite = Favorite.objects.create(user=favorite_user, item=item)
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=self.now - timedelta(days=2),
        )

        registrants = [
            User.objects.create_user(
                username=f'goal-registrant-{index}', password='safe-password-123',
            )
            for index in range(3)
        ]
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            user=registrants[0],
            joined_at=self.now - timedelta(days=2),
        )
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            user=registrants[1],
            joined_at=self.now - timedelta(days=1),
        )
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            user=registrants[2],
            joined_at=self.now - timedelta(days=7),
        )

        completed_buyer = User.objects.create_user(
            username='goal-completed-buyer', password='safe-password-123',
        )
        cancelled_buyer = User.objects.create_user(
            username='goal-cancelled-buyer', password='safe-password-123',
        )
        completed_order = Order.objects.create(
            item=item,
            buyer=completed_buyer,
            seller=self.seller,
            agreed_price='20.00',
            status='completed',
        )
        cancelled_order = Order.objects.create(
            item=item,
            buyer=cancelled_buyer,
            seller=self.seller,
            agreed_price='20.00',
            status='cancelled',
        )
        Order.objects.filter(pk=completed_order.pk).update(
            created_at=self.now - timedelta(days=2),
        )
        Order.objects.filter(pk=cancelled_order.pk).update(
            created_at=self.now - timedelta(days=1),
        )

        for metric in (
            'new_items', 'viewers', 'favorite_users', 'registration_users',
            'order_users', 'completed_order_users',
        ):
            self._goal(metric)

        analytics = build_campaign_analytics(days=30, now=self.now)
        goals = {row['metric']: row for row in analytics['all_goal_rows']}

        self.assertEqual(goals['new_items']['current_value'], 1)
        self.assertEqual(goals['viewers']['current_value'], 2)
        self.assertEqual(goals['favorite_users']['current_value'], 1)
        self.assertEqual(goals['registration_users']['current_value'], 2)
        self.assertEqual(goals['order_users']['current_value'], 2)
        self.assertEqual(goals['completed_order_users']['current_value'], 1)
        self.assertEqual(len(analytics['all_goal_rows']), 6)

    def test_goal_status_distinguishes_future_expired_and_behind_pace(self):
        self._goal('viewers', target=10)
        self._goal(
            'favorite_users',
            target=2,
            starts_at=self.now + timedelta(days=1),
            ends_at=self.now + timedelta(days=4),
        )
        self._goal(
            'new_items',
            target=10,
            starts_at=self.now - timedelta(days=8),
            ends_at=self.now - timedelta(days=4),
        )

        analytics = build_campaign_analytics(days=30, now=self.now)
        goals = {row['metric']: row for row in analytics['all_goal_rows']}

        self.assertEqual(goals['viewers']['status_label'], '需要推进')
        self.assertEqual(goals['viewers']['elapsed_percent'], 50.0)
        self.assertEqual(goals['viewers']['pace_delta'], -50.0)
        self.assertEqual(goals['favorite_users']['status_label'], '待开始')
        self.assertIsNone(goals['favorite_users']['pace_delta'])
        self.assertEqual(goals['new_items']['status_label'], '已到期未达成')
        self.assertEqual(analytics['summary']['goal_attention_count'], 2)

    def test_goal_model_rejects_invalid_target_window_and_duplicate_metric(self):
        goal = CampaignGoal(
            campaign=self.campaign,
            metric='new_items',
            target_value=0,
            starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            goal.full_clean()

        goal.target_value = 3
        goal.starts_at = self.campaign.starts_at - timedelta(hours=1)
        with self.assertRaises(ValidationError):
            goal.full_clean()

        goal.starts_at = self.now - timedelta(days=1)
        goal.ends_at = self.campaign.ends_at + timedelta(hours=1)
        with self.assertRaises(ValidationError):
            goal.full_clean()

        self._goal('new_items', target=3)
        duplicate = CampaignGoal(
            campaign=self.campaign,
            metric='new_items',
            target_value=4,
            starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_dashboard_and_csv_expose_goal_progress_and_formula(self):
        self._item(title='目标看板商品')
        self._goal('new_items', target=2, note='毕业季补齐教材供给')
        admin = User.objects.create_user(
            username='goal-dashboard-admin',
            password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(admin)

        page = self.client.get(reverse('operations_dashboard'))
        export = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '专题目标与节奏')
        self.assertContains(page, '周期新增商品')
        self.assertContains(page, '毕业季补齐教材供给')
        self.assertContains(page, '目标值实时从业务表重算')
        self.assertEqual(export.status_code, 200)
        content = export.content.decode('utf-8-sig')
        self.assertIn('专题目标与节奏', content)
        self.assertIn('完成率（%）', content)
        self.assertIn('节奏偏差（百分点）', content)
        self.assertIn('校园目标专题', content)
        self.assertIn('毕业季补齐教材供给', content)
