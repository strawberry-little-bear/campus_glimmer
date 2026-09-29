from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .analytics import build_operational_alerts
from .demand_radar import build_demand_radar
from .models import CampusLocation, Category, DemandOpportunityTask, DemandPost, Item, SearchQuery


class DemandRadarTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='radar-user', password='safe-password-123')
        self.other_user = User.objects.create_user(username='radar-other', password='safe-password-123')
        self.category = Category.objects.create(name='数码设备')
        self.location = CampusLocation.objects.create(name='图书馆东门')

    def test_radar_combines_search_and_demand_signals(self):
        for query_user in (self.user, self.user, self.other_user):
            SearchQuery.objects.create(
                user=query_user, query='充电宝', category=self.category,
                location=self.location, result_count=0,
            )
        DemandPost.objects.create(
            requester=self.user, title='充电宝', description='通勤临时使用',
            category=self.category, location=self.location, status='active',
        )
        DemandPost.objects.create(
            requester=self.other_user, title='充电宝', description='容量 10000mAh',
            category=self.category, location=self.location, status='active',
        )

        radar = build_demand_radar(30)

        self.assertEqual(radar['summary']['opportunity_count'], 1)
        row = next(row for row in radar['rows'] if row['label'] == '充电宝')
        self.assertEqual(row['search_count'], 3)
        self.assertEqual(row['unique_searchers'], 2)
        self.assertEqual(row['demand_count'], 2)
        self.assertEqual(row['unique_requesters'], 2)
        self.assertEqual(row['available_supply'], 0)
        self.assertEqual(row['level'], 'critical')
        self.assertIn('补充相关供给', row['recommendations'])
        self.assertIn('完善搜索同义词', row['recommendations'])

    def test_radar_only_counts_current_matching_supply(self):
        SearchQuery.objects.create(
            user=self.user, query='台灯', category=self.category,
            location=self.location, result_count=0,
        )
        Item.objects.create(
            title='宿舍台灯', description='暖光台灯', price='20.00',
            category=self.category, location=self.location, condition='全新', seller=self.user,
        )
        Item.objects.create(
            title='过期台灯', description='不应计入', price='10.00',
            category=self.category, location=self.location, condition='八成新', seller=self.user,
            expires_at=timezone.now(),
        )

        radar = build_demand_radar(30)

        row = next(row for row in radar['rows'] if row['label'] == '台灯')
        self.assertEqual(row['available_supply'], 1)
        self.assertNotIn('补充相关供给', row['recommendations'])

    def test_operations_dashboard_renders_demand_radar_and_export(self):
        staff = User.objects.create_superuser(
            username='radar-staff', email='radar@example.com', password='safe-password-123',
        )
        SearchQuery.objects.create(user=self.user, query='雨伞', result_count=0)
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园需求机会')
        self.assertContains(response, '雨伞')

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '需求主题')
        self.assertContains(export, '雨伞')


    def test_critical_radar_opportunity_enters_operations_alerts(self):
        alerts = build_operational_alerts(
            {
                'searches': 1, 'zero_result_searches': 1, 'zero_result_rate': 100,
                'searches_with_results': 0, 'search_click_through_rate': 0,
                'pending_reports': 0,
            },
            [],
            {
                'rows': [{
                    'label': '充电宝', 'level': 'critical', 'opportunity_score': 15,
                    'available_supply': 0,
                    'evidence': ['无结果搜索 3 次', '有效求购 2 条'],
                    'recommendations': ['补充相关供给', '关注“图书馆东门”附近'],
                }],
            },
        )

        alert = next(alert for alert in alerts if alert['key'] == 'demand_radar_opportunity')
        self.assertEqual(alert['severity'], 'critical')
        self.assertEqual(alert['metric'], '15')
        self.assertIn('补充相关供给', alert['message'])
        self.assertEqual(alert['action_url_name'], 'operations_dashboard')

    def test_staff_can_create_and_complete_demand_follow_up_task(self):
        staff = User.objects.create_superuser(
            username='task-staff', email='task@example.com', password='safe-password-123',
        )
        SearchQuery.objects.create(
            user=self.user, query='折叠伞', category=self.category,
            location=self.location, result_count=0,
        )
        radar = build_demand_radar(30)
        row = radar['rows'][0]

        self.client.force_login(self.user)
        forbidden = self.client.post(reverse('create_demand_opportunity_task'), {
            'radar_key': row['radar_key'], 'days': 30,
        })
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(staff)
        created = self.client.post(reverse('create_demand_opportunity_task'), {
            'radar_key': row['radar_key'], 'days': 30,
        })
        self.assertEqual(created.status_code, 302)
        task = DemandOpportunityTask.objects.get()
        self.assertEqual(task.title, '折叠伞')
        self.assertEqual(task.created_by, staff)
        self.assertEqual(task.search_count, 1)

        self.client.post(reverse('create_demand_opportunity_task'), {
            'radar_key': row['radar_key'], 'days': 30,
        })
        self.assertEqual(DemandOpportunityTask.objects.count(), 1)

        started = self.client.post(
            reverse('update_demand_opportunity_task', args=[task.id]),
            {'status': 'in_progress', 'days': 30},
        )
        self.assertEqual(started.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.assigned_to, staff)

        self.client.post(
            reverse('update_demand_opportunity_task', args=[task.id]),
            {'status': 'completed', 'days': 30},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, 'completed')
