from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campaign_analytics import build_campaign_analytics
from .models import BrowsingHistory, CampusCampaign, Category, Favorite, Item, Order


class CampaignAnalyticsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='campaign-analytics-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='专题分析分类')
        self.success_campaign = CampusCampaign.objects.create(
            title='毕业季专题',
            slug='analytics-graduation',
            starts_at=self.now - timedelta(days=10),
            ends_at=self.now + timedelta(days=10),
        )
        self.watch_campaign = CampusCampaign.objects.create(
            title='教材交换专题',
            slug='analytics-textbooks',
            starts_at=self.now - timedelta(days=10),
            ends_at=self.now + timedelta(days=10),
        )

    def _item(self, campaign, *, title):
        item = Item.objects.create(
            title=title,
            description='用于专题运营分析测试',
            price='20.00',
            category=self.category,
            condition='良好',
            seller=self.seller,
            campaign=campaign,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=self.now - timedelta(days=3),
        )
        return item

    def _viewers(self, item, count):
        for index in range(count):
            user = User.objects.create_user(
                username=f'campaign-viewer-{item.pk}-{index}',
                password='safe-password-123',
            )
            history = BrowsingHistory.objects.create(user=user, item=item)
            BrowsingHistory.objects.filter(pk=history.pk).update(
                last_viewed_at=self.now - timedelta(days=2),
            )

    def test_builds_campaign_funnel_and_ranks_completed_campaign(self):
        item = self._item(self.success_campaign, title='毕业季教材')
        self._viewers(item, 4)
        favorite_user = User.objects.create_user(
            username='campaign-favorite', password='safe-password-123',
        )
        favorite = Favorite.objects.create(user=favorite_user, item=item)
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=self.now - timedelta(days=2),
        )
        buyer = User.objects.create_user(
            username='campaign-buyer', password='safe-password-123',
        )
        order = Order.objects.create(
            item=item,
            buyer=buyer,
            seller=self.seller,
            agreed_price='20.00',
            status='completed',
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=2),
        )

        analytics = build_campaign_analytics(days=30, now=self.now)

        self.assertEqual(analytics['summary']['campaign_count'], 2)
        row = analytics['rows'][0]
        self.assertEqual(row['title'], '毕业季专题')
        self.assertEqual(row['new_items'], 1)
        self.assertEqual(row['views'], 4)
        self.assertEqual(row['viewers'], 4)
        self.assertEqual(row['favorites'], 1)
        self.assertEqual(row['orders'], 1)
        self.assertEqual(row['completed_orders'], 1)
        self.assertEqual(row['view_to_order_rate'], 25.0)
        self.assertEqual(row['completion_rate'], 100.0)
        self.assertEqual(row['performance_tone'], 'success')

    def test_flags_campaign_with_attention_without_orders(self):
        item = self._item(self.watch_campaign, title='无人预约教材')
        self._viewers(item, 5)

        analytics = build_campaign_analytics(days=30, now=self.now)

        self.assertEqual(analytics['summary']['attention_count'], 2)
        self.assertEqual(analytics['attention_rows'][0]['title'], '教材交换专题')
        self.assertEqual(analytics['attention_rows'][0]['performance_label'], '浏览未转化')
        self.assertEqual(analytics['attention_rows'][0]['view_to_order_rate'], 0)

    def test_ignores_campaigns_outside_report_window(self):
        old_campaign = CampusCampaign.objects.create(
            title='很久以前的专题',
            slug='analytics-old',
            starts_at=self.now - timedelta(days=90),
            ends_at=self.now - timedelta(days=60),
        )
        self._item(old_campaign, title='旧商品')

        analytics = build_campaign_analytics(days=30, now=self.now)

        titles = {row['title'] for row in analytics['rows']}
        self.assertNotIn('很久以前的专题', titles)
        self.assertIn('毕业季专题', titles)
        self.assertEqual(analytics['summary']['attention_count'], 2)

    def test_operations_dashboard_renders_campaign_analytics(self):
        item = self._item(self.success_campaign, title='看板专题商品')
        self._viewers(item, 1)
        admin = User.objects.create_user(
            username='campaign-analytics-admin', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园专题表现')
        self.assertContains(response, '毕业季专题')
