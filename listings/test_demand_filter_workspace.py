# -*- coding: utf-8 -*-
from urllib.parse import parse_qs

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import CampusLocation, Category, DemandPost


class DemandFilterWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = User.objects.create_user(
            username='demand-filter-viewer', password='safe-password-123',
        )
        cls.other_user = User.objects.create_user(
            username='demand-filter-other', password='safe-password-123',
        )
        cls.category = Category.objects.create(name='数码设备')
        cls.location = CampusLocation.objects.create(name='图书馆大厅')
        cls.demand = DemandPost.objects.create(
            requester=cls.other_user,
            title='求购一台宿舍显示器',
            description='希望是 24 英寸，预算合适即可。',
            category=cls.category,
            location=cls.location,
            max_price='500.00',
        )
        cls.other_demand = DemandPost.objects.create(
            requester=cls.other_user,
            title='求购羽毛球拍',
            description='周末可以当面交易。',
        )

    def test_filter_workspace_reports_results_and_removable_conditions(self):
        response = self.client.get(reverse('demand_list'), {
            'q': '显示器',
            'category': self.category.id,
            'location': self.location.id,
            'sort': 'popular',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(response.context['demand_active_filter_count'], 3)
        self.assertEqual(response.context['demand_sort_label'], '浏览最多')
        self.assertContains(response, '当前显示 1 条 · 已启用 3 个条件')
        self.assertContains(response, '关键词：显示器')
        self.assertContains(response, '分类：数码设备')
        self.assertContains(response, '地点：图书馆大厅')
        self.assertContains(response, '正在寻找的需求')
        self.assertContains(response, '按浏览最多排序')

        filters = {
            row['label']: row for row in response.context['demand_active_filters']
        }
        self.assertEqual(
            parse_qs(filters['关键词']['remove_query']),
            {'category': [str(self.category.id)], 'location': [str(self.location.id)], 'sort': ['popular']},
        )
        self.assertEqual(
            parse_qs(filters['分类']['remove_query']),
            {'q': ['显示器'], 'location': [str(self.location.id)], 'sort': ['popular']},
        )

    def test_default_browse_page_has_no_filter_summary(self):
        response = self.client.get(reverse('demand_list'))

        self.assertEqual(response.context['demand_active_filter_count'], 0)
        self.assertContains(response, '当前显示 2 条')
        self.assertNotContains(response, '清除筛选')
        self.assertContains(response, '求购一台宿舍显示器')
        self.assertContains(response, '求购羽毛球拍')

    def test_my_demands_keeps_the_same_filter_workspace_and_ownership_scope(self):
        own_demand = DemandPost.objects.create(
            requester=self.viewer,
            title='求购我的专业课教材',
            description='需要最新版。',
        )
        self.client.force_login(self.viewer)

        response = self.client.get(reverse('my_demands'), {'q': '教材'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_mine'])
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertContains(response, own_demand.title)
        self.assertNotContains(response, self.demand.title)
        self.assertContains(response, '我的求购')
        self.assertContains(response, '当前显示 1 条 · 已启用 1 个条件')
        self.assertContains(response, f'href="{reverse("my_demands")}"')
