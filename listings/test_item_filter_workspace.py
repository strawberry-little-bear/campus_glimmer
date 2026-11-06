# -*- coding: utf-8 -*-
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import CampusLocation, Category


class ItemFilterWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='catalog-filter-user', password='safe-password-123',
        )
        cls.category = Category.objects.create(name='数码设备')
        cls.location = CampusLocation.objects.create(name='图书馆大厅')

    def _request_with_all_filters(self):
        return self.client.get(
            reverse('item_list_by_category', args=[self.category.id]),
            {
                'q': '不存在的键盘',
                'condition': '九成新',
                'location': self.location.id,
                'min_price': '50',
                'max_price': '200',
                'sort': 'price_desc',
            },
        )

    def test_filter_workspace_reports_removable_conditions_without_losing_others(self):
        response = self._request_with_all_filters()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertEqual(response.context['item_active_filter_count'], 5)
        self.assertContains(response, '已启用 5 个条件')
        self.assertContains(response, '关键词：不存在的键盘')
        self.assertContains(response, '分类：数码设备')
        self.assertContains(response, '地点：图书馆大厅')
        self.assertContains(response, '成色：九成新')
        self.assertContains(response, '价格：¥50–¥200')
        self.assertContains(response, '这组条件暂时没有结果')

        filters = {
            row['label']: row for row in response.context['item_active_filters']
        }
        expected_remaining = {
            'condition': ['九成新'],
            'location': [str(self.location.id)],
            'min_price': ['50'],
            'max_price': ['200'],
            'sort': ['price_desc'],
        }

        query_url = urlsplit(filters['关键词']['remove_url'])
        self.assertEqual(query_url.path, reverse('item_list_by_category', args=[self.category.id]))
        self.assertEqual(parse_qs(query_url.query), expected_remaining)

        category_url = urlsplit(filters['分类']['remove_url'])
        self.assertEqual(category_url.path, reverse('item_list'))
        self.assertEqual(
            parse_qs(category_url.query),
            {'q': ['不存在的键盘'], **expected_remaining},
        )

        price_url = urlsplit(filters['价格']['remove_url'])
        self.assertEqual(price_url.path, reverse('item_list_by_category', args=[self.category.id]))
        self.assertEqual(
            parse_qs(price_url.query),
            {
                'q': ['不存在的键盘'],
                'condition': ['九成新'],
                'location': [str(self.location.id)],
                'sort': ['price_desc'],
            },
        )

    def test_filtered_empty_state_offers_clear_save_and_demand_next_steps(self):
        response = self._request_with_all_filters()

        self.assertContains(response, '浏览全部商品')
        self.assertContains(response, '登录后保存关注')
        self.assertContains(response, reverse('demand_list'))
        self.assertContains(response, '清除筛选')

    def test_authenticated_empty_state_links_to_the_live_saved_search_form(self):
        self.client.force_login(self.user)
        response = self._request_with_all_filters()

        self.assertContains(response, 'id="saved-search-prompt"')
        self.assertContains(response, 'href="#saved-search-prompt"')
        self.assertNotContains(response, '登录后保存关注')

    def test_unfiltered_empty_catalog_has_a_different_message(self):
        response = self.client.get(reverse('item_list'))

        self.assertEqual(response.context['item_active_filter_count'], 0)
        self.assertContains(response, '暂时还没有可浏览的商品')
        self.assertNotContains(response, '这组条件暂时没有结果')
        self.assertNotContains(response, '已启用 0 个条件')

    def test_unapplied_invalid_price_is_not_reported_as_an_active_filter(self):
        response = self.client.get(reverse('item_list'), {
            'q': '不存在的键盘',
            'condition': '   ',
            'min_price': 'not-a-number',
            'max_price': '-10',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['item_active_filter_count'], 1)
        self.assertEqual(response.context['item_active_filters'][0]['label'], '关键词')
        self.assertNotContains(response, '价格：')
        self.assertNotContains(response, '成色：')
