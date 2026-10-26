# -*- coding: utf-8 -*-
from urllib.parse import parse_qs

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusLocation, Category, LostFoundPost


class LostFoundFilterWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = User.objects.create_user(
            username='lost-found-filter-viewer', password='safe-password-123',
        )
        cls.other_user = User.objects.create_user(
            username='lost-found-filter-other', password='safe-password-123',
        )
        cls.category = Category.objects.create(name='数码设备')
        cls.location = CampusLocation.objects.create(name='图书馆大厅')
        cls.lost_post = LostFoundPost.objects.create(
            reporter=cls.other_user,
            post_type='lost',
            title='遗失黑色水杯',
            description='在图书馆大厅自习时遗失，杯身有蓝色贴纸。',
            category=cls.category,
            location=cls.location,
            occurred_at=timezone.now(),
            identifying_features='蓝色贴纸和银色挂件',
            view_count=9,
        )
        cls.found_post = LostFoundPost.objects.create(
            reporter=cls.other_user,
            post_type='found',
            title='捡到一把蓝色雨伞',
            description='在一号教学楼门口捡到。',
            occurred_at=timezone.now(),
        )

    def test_filter_workspace_reports_results_and_removable_conditions(self):
        response = self.client.get(reverse('lost_found_list'), {
            'q': '黑色水杯',
            'type': 'lost',
            'category': self.category.id,
            'location': self.location.id,
            'sort': 'popular',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(response.context['lost_found_active_filter_count'], 4)
        self.assertEqual(response.context['lost_found_sort_label'], '浏览最多')
        self.assertContains(response, '当前显示 1 条 · 已启用 4 个条件')
        self.assertContains(response, '关键词：黑色水杯')
        self.assertContains(response, '类型：我丢失了')
        self.assertContains(response, '分类：数码设备')
        self.assertContains(response, '地点：图书馆大厅')
        self.assertContains(response, '校园失物线索')
        self.assertContains(response, '按浏览最多排序')

        filters = {
            row['label']: row for row in response.context['lost_found_active_filters']
        }
        self.assertEqual(
            parse_qs(filters['关键词']['remove_query']),
            {
                'type': ['lost'],
                'category': [str(self.category.id)],
                'location': [str(self.location.id)],
                'sort': ['popular'],
            },
        )
        self.assertEqual(
            parse_qs(filters['分类']['remove_query']),
            {
                'q': ['黑色水杯'],
                'type': ['lost'],
                'location': [str(self.location.id)],
                'sort': ['popular'],
            },
        )

    def test_invalid_filters_are_safely_normalized(self):
        inactive_location = CampusLocation.objects.create(
            name='暂停使用的地点', is_active=False,
        )

        response = self.client.get(reverse('lost_found_list'), {
            'q': '  ',
            'type': 'unknown',
            'category': '999999',
            'location': inactive_location.id,
            'sort': 'unknown',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['lost_found_active_filter_count'], 0)
        self.assertEqual(response.context['lost_found_active_filters'], [])
        self.assertEqual(response.context['lost_found_type'], '')
        self.assertEqual(response.context['lost_found_category'], '')
        self.assertEqual(response.context['lost_found_location'], '')
        self.assertEqual(response.context['lost_found_sort'], 'latest')
        self.assertEqual(response.context['page_obj'].paginator.count, 2)
        self.assertNotContains(response, '清除筛选')

    def test_my_lost_found_keeps_the_same_filter_workspace_and_ownership_scope(self):
        own_post = LostFoundPost.objects.create(
            reporter=self.viewer,
            post_type='found',
            title='捡到校园卡',
            description='卡套内没有公开个人信息。',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now(),
        )
        self.client.force_login(self.viewer)

        response = self.client.get(reverse('my_lost_found'), {
            'q': '校园卡',
            'type': 'found',
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_mine'])
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertContains(response, own_post.title)
        self.assertNotContains(response, self.lost_post.title)
        self.assertContains(response, '我的失物与拾到记录')
        self.assertContains(response, '当前显示 1 条 · 已启用 2 个条件')
        self.assertContains(response, f'href="{reverse("my_lost_found")}"')

    def test_pagination_keeps_the_active_filters(self):
        for index in range(13):
            LostFoundPost.objects.create(
                reporter=self.other_user,
                post_type='lost',
                title=f'分页水杯 {index}',
                description='用于验证筛选后的翻页链接。',
                category=self.category,
                location=self.location,
                occurred_at=timezone.now(),
            )

        response = self.client.get(reverse('lost_found_list'), {
            'q': '分页水杯',
            'type': 'lost',
            'category': self.category.id,
            'sort': 'popular',
            'page': 2,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 13)
        self.assertEqual(response.context['lost_found_active_filter_count'], 3)
        self.assertContains(response, 'q=%E5%88%86%E9%A1%B5%E6%B0%B4%E6%9D%AF')
        self.assertContains(response, f'category={self.category.id}')
        self.assertContains(response, 'sort=popular')

    def test_empty_state_distinguishes_filters_from_an_empty_library(self):
        filtered = self.client.get(reverse('lost_found_list'), {'q': '不存在的物品'})
        self.assertContains(filtered, '没有符合这组条件的记录')
        self.assertContains(filtered, '清除筛选')

        LostFoundPost.objects.all().delete()
        empty = self.client.get(reverse('lost_found_list'))
        self.assertContains(empty, '还没有公开的失物招领记录')
        self.assertNotContains(empty, '没有符合这组条件的记录')
