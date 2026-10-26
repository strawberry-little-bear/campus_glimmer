from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campaign_analytics import build_campaign_analytics
from .models import (
    BrowsingHistory, CampusCampaign, CampaignRegistration, Category, Favorite, Item, Order,
)


class CampaignAnalyticsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='campaign-analytics-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='专题分析分类')
        self.user_index = 0
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

    def _closed_campaign(self, title, slug):
        return CampusCampaign.objects.create(
            title=title,
            slug=slug,
            starts_at=self.now - timedelta(days=20),
            ends_at=self.now - timedelta(days=5),
        )

    def _campaign_funnel(self, campaign, *, viewers, orders, completed):
        item = self._item(campaign, title=f'{campaign.title}转化商品')
        Item.objects.filter(pk=item.pk).update(
            created_at=self.now - timedelta(days=10),
        )
        viewer_users = []
        for _ in range(viewers):
            self.user_index += 1
            user = User.objects.create(
                username=f'comparison-viewer-{campaign.pk}-{self.user_index}',
            )
            history = BrowsingHistory.objects.create(user=user, item=item)
            BrowsingHistory.objects.filter(pk=history.pk).update(
                last_viewed_at=self.now - timedelta(days=8),
            )
            viewer_users.append(user)
        for index, user in enumerate(viewer_users[:orders]):
            order = Order.objects.create(
                item=item,
                buyer=user,
                seller=self.seller,
                agreed_price='20.00',
                status='completed' if index < completed else 'cancelled',
            )
            Order.objects.filter(pk=order.pk).update(
                created_at=self.now - timedelta(days=7),
            )
        return item

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
        registrants = [
            User.objects.create_user(
                username=f'campaign-registrant-{index}',
                password='safe-password-123',
            )
            for index in range(3)
        ]
        for registrant in registrants[:2]:
            CampaignRegistration.objects.create(
                campaign=self.success_campaign,
                user=registrant,
                joined_at=self.now - timedelta(days=1),
            )
        CampaignRegistration.objects.create(
            campaign=self.success_campaign,
            user=registrants[2],
            joined_at=self.now - timedelta(days=1),
            left_at=self.now - timedelta(hours=1),
            is_active=False,
        )

        analytics = build_campaign_analytics(days=30, now=self.now)

        self.assertEqual(analytics['summary']['campaign_count'], 2)
        self.assertEqual(analytics['summary']['active_registration_count'], 2)
        self.assertEqual(analytics['summary']['new_registration_count'], 3)
        row = analytics['rows'][0]
        self.assertEqual(row['title'], '毕业季专题')
        self.assertEqual(row['new_items'], 1)
        self.assertEqual(row['views'], 4)
        self.assertEqual(row['viewers'], 4)
        self.assertEqual(row['favorites'], 1)
        self.assertEqual(row['active_registration_count'], 2)
        self.assertEqual(row['new_registration_count'], 3)
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

    def test_comparison_ranks_by_the_visible_one_decimal_rate(self):
        first = self._closed_campaign('预览同分专题 A', 'comparison-visible-a')
        second = self._closed_campaign('预览同分专题 B', 'comparison-visible-b')
        self._campaign_funnel(first, viewers=28, orders=5, completed=5)
        self._campaign_funnel(second, viewers=39, orders=7, completed=7)

        comparison = build_campaign_analytics(days=30, now=self.now)['comparison']
        rows = {row['title']: row for row in comparison['rows']}

        self.assertEqual(rows['预览同分专题 A']['viewer_to_order_rate'], 17.9)
        self.assertEqual(rows['预览同分专题 B']['viewer_to_order_rate'], 17.9)
        for row in rows.values():
            self.assertEqual(row['viewer_rate_rank'], 1)
            self.assertTrue(row['viewer_rate_rank_tied'])
            self.assertEqual(row['path_rank'], 1)
            self.assertTrue(row['path_rank_tied'])

    def test_comparison_excludes_open_and_endless_campaigns(self):
        closed = self._closed_campaign('已结束对比专题', 'comparison-closed')
        self._campaign_funnel(closed, viewers=12, orders=3, completed=2)
        open_item = self._item(self.success_campaign, title='进行中对比商品')
        self._viewers(open_item, 12)
        endless = CampusCampaign.objects.create(
            title='无结束时间对比专题',
            slug='comparison-endless',
            starts_at=self.now - timedelta(days=20),
            ends_at=None,
        )
        self._campaign_funnel(endless, viewers=12, orders=3, completed=2)

        comparison = build_campaign_analytics(days=30, now=self.now)['comparison']

        self.assertEqual(
            [row['title'] for row in comparison['rows']],
            ['已结束对比专题'],
        )
        self.assertEqual(comparison['summary']['closed_campaign_count'], 1)
        self.assertIn('不输出跨专题方向', comparison['conclusion'])

    def test_comparison_hides_viewer_rates_when_viewer_sample_is_thin(self):
        campaign = self._closed_campaign('浏览薄样本专题', 'comparison-thin-viewers')
        self._campaign_funnel(campaign, viewers=11, orders=3, completed=2)

        comparison = build_campaign_analytics(days=30, now=self.now)['comparison']
        row = comparison['rows'][0]

        self.assertIsNone(row['viewer_to_order_rate'])
        self.assertIsNone(row['viewer_to_complete_rate'])
        self.assertEqual(row['order_to_complete_rate'], 66.7)
        self.assertEqual(row['status_label'], '浏览样本不足')
        self.assertIsNone(row['viewer_rate_rank'])
        self.assertIsNone(row['path_rank'])
        self.assertIn('只报数字', comparison['conclusion'])

    def test_comparison_hides_order_rate_when_order_sample_is_thin(self):
        campaign = self._closed_campaign('预约薄样本专题', 'comparison-thin-orders')
        self._campaign_funnel(campaign, viewers=12, orders=2, completed=1)

        comparison = build_campaign_analytics(days=30, now=self.now)['comparison']
        row = comparison['rows'][0]

        self.assertEqual(row['viewer_to_order_rate'], 16.7)
        self.assertIsNone(row['order_to_complete_rate'])
        self.assertEqual(row['viewer_to_complete_rate'], 8.3)
        self.assertEqual(row['status_label'], '预约样本不足')
        self.assertIsNone(row['order_rate_rank'])
        self.assertIn('不输出跨专题方向', comparison['conclusion'])

    def test_comparison_assigns_ranks_when_thin_rows_are_mixed_with_eligible_rows(self):
        first = self._closed_campaign('混合样本专题 A', 'comparison-mixed-a')
        second = self._closed_campaign('混合样本专题 B', 'comparison-mixed-b')
        thin = self._closed_campaign('混合样本专题 C', 'comparison-mixed-c')
        self._campaign_funnel(first, viewers=12, orders=3, completed=2)
        self._campaign_funnel(second, viewers=14, orders=4, completed=2)
        self._campaign_funnel(thin, viewers=11, orders=3, completed=2)

        comparison = build_campaign_analytics(days=30, now=self.now)['comparison']
        rows = {row['title']: row for row in comparison['rows']}

        self.assertIsNotNone(rows['混合样本专题 A']['viewer_rate_rank'])
        self.assertIsNotNone(rows['混合样本专题 B']['viewer_rate_rank'])
        self.assertIsNone(rows['混合样本专题 C']['viewer_rate_rank'])
        self.assertFalse(rows['混合样本专题 C']['viewer_rate_rank_tied'])
        self.assertIsNone(rows['混合样本专题 C']['path_rank'])

    def test_comparison_page_and_csv_share_rows_thresholds_and_conclusion(self):
        first = self._closed_campaign('页面 CSV 对比 A', 'comparison-page-csv-a')
        second = self._closed_campaign('页面 CSV 对比 B', 'comparison-page-csv-b')
        self._campaign_funnel(first, viewers=12, orders=3, completed=2)
        self._campaign_funnel(second, viewers=12, orders=6, completed=3)
        admin = User.objects.create_user(
            username='comparison-page-csv-admin',
            password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(admin)

        page = self.client.get(
            reverse('operations_dashboard_fragment', args=['ops-campaigns']),
            {'days': 30},
        )
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        csv_content = export.content.decode('utf-8-sig')
        conclusion = (
            '已结束专题 2 个，其中 2 个浏览样本达标、2 个预约样本达标。'
            '浏览→预约率范围 25.0%—50.0%，浏览→完成率范围 16.7%—25.0%。'
            '预约→完成率范围 50.0%—66.7%。排序仅作活动复盘参考，'
            '不据此判断专题质量或自动调整投放。'
        )

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '跨专题转化对比')
        self.assertContains(page, '页面 CSV 对比 A')
        self.assertContains(page, '页面 CSV 对比 B')
        self.assertContains(page, '至少 2 个专题达到同段样本门槛才给出参考位次')
        self.assertContains(page, conclusion)
        self.assertEqual(export.status_code, 200)
        self.assertIn('跨专题转化对比', csv_content)
        self.assertIn('浏览样本门槛,12,预约样本门槛,3', csv_content)
        self.assertIn('页面 CSV 对比 A', csv_content)
        self.assertIn('页面 CSV 对比 B', csv_content)
        self.assertIn(conclusion, csv_content)
        self.assertIn('不据此判断专题质量', csv_content)

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
        self.assertContains(response, '当前活跃报名')
        self.assertContains(response, '周期新增报名')

    def test_operations_csv_exports_registration_metrics_with_same_definition(self):
        registrants = [
            User.objects.create_user(
                username=f'campaign-csv-registrant-{index}',
                password='safe-password-123',
            )
            for index in range(2)
        ]
        for registrant in registrants:
            CampaignRegistration.objects.create(
                campaign=self.success_campaign,
                user=registrant,
                joined_at=self.now - timedelta(days=1),
            )
        admin = User.objects.create_user(
            username='campaign-csv-admin',
            password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        content = response.content.decode('utf-8-sig')
        self.assertIn('专题当前活跃报名', content)
        self.assertIn('专题周期新增报名', content)
        self.assertIn('当前活跃报名', content)
        self.assertIn('周期新增报名', content)
        self.assertIn('报名表示用户关注专题，不会预约商品、占用库存或创建订单', content)


class EmptyCampaignComparisonTests(TestCase):
    def test_empty_period_still_renders_comparison_empty_state(self):
        admin = User.objects.create_user(
            username='comparison-empty-admin',
            password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(admin)

        response = self.client.get(
            reverse('operations_dashboard_fragment', args=['ops-campaigns']),
            {'days': 30},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '跨专题转化对比')
        self.assertContains(response, '当前统计周期还没有已结束的专题')
