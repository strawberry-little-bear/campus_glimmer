"""关注搜索工作台的分页、操作回跳与开放重定向防护。"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from . import views
from .models import SavedSearch


class SavedSearchPaginationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='saved-search-pagination-user', password='safe-password-123',
        )
        self.client.force_login(self.user)
        self.rows = [
            SavedSearch.objects.create(user=self.user, name=f'关注搜索 {index:02d}')
            for index in range(9)
        ]

    def test_results_are_paginated_at_eight_per_page(self):
        first = self.client.get(reverse('saved_search_list'))
        second = self.client.get(reverse('saved_search_list'), {'page': 2})

        self.assertEqual(first.context['page_obj'].number, 1)
        self.assertEqual(len(first.context['saved_searches']), 8)
        self.assertEqual(first.context['page_obj'].paginator.count, 9)
        self.assertEqual(second.context['page_obj'].number, 2)
        self.assertEqual(len(second.context['saved_searches']), 1)
        self.assertContains(first, '第 1–8 条 · 共 9 条')
        self.assertContains(first, 'aria-label="关注的搜索分页"')
        self.assertContains(first, 'aria-current="page"')

    def test_match_stats_only_run_for_the_rendered_page(self):
        with patch.object(
            views, '_attach_saved_search_stats', wraps=views._attach_saved_search_stats,
        ) as attach:
            first = self.client.get(reverse('saved_search_list'))
            second = self.client.get(reverse('saved_search_list'), {'page': 2})

        self.assertEqual(len(attach.call_args_list[0].args[0]), 8)
        self.assertEqual(len(attach.call_args_list[1].args[0]), 1)
        self.assertEqual(first.context['page_obj'].paginator.count, 9)
        self.assertEqual(second.context['page_obj'].number, 2)

    def test_out_of_range_and_invalid_pages_fall_back_safely(self):
        too_high = self.client.get(reverse('saved_search_list'), {'page': 999})
        invalid = self.client.get(reverse('saved_search_list'), {'page': 'not-a-number'})

        self.assertEqual(too_high.context['page_obj'].number, 2)
        self.assertEqual(len(too_high.context['saved_searches']), 1)
        self.assertEqual(invalid.context['page_obj'].number, 1)
        self.assertEqual(len(invalid.context['saved_searches']), 8)

    def test_active_searches_sort_before_paused_and_actions_keep_the_page(self):
        paused = self.rows[-1]
        paused.is_active = False
        paused.save(update_fields=['is_active', 'updated_at'])
        page_two = f"{reverse('saved_search_list')}?page=2"
        target = self.rows[0]

        first_page = self.client.get(reverse('saved_search_list'))
        self.assertNotIn(
            paused.pk,
            [row.pk for row in first_page.context['saved_searches']],
        )
        second_page = self.client.get(reverse('saved_search_list'), {'page': 2})
        self.assertEqual(second_page.context['saved_searches'][0].pk, paused.pk)

        response = self.client.post(
            reverse('toggle_saved_search', args=[target.pk]), {'next': page_two},
        )
        self.assertRedirects(response, page_two, fetch_redirect_response=False)

        response = self.client.post(
            reverse('update_saved_search_cadence', args=[target.pk]),
            {'notify_frequency': 'daily', 'next': page_two},
        )
        self.assertRedirects(response, page_two, fetch_redirect_response=False)

        response = self.client.post(
            reverse('delete_saved_search', args=[target.pk]), {'next': page_two},
        )
        self.assertRedirects(response, page_two, fetch_redirect_response=False)

    def test_external_next_destination_is_rejected(self):
        target = self.rows[0]
        for url_name in (
            'toggle_saved_search',
            'update_saved_search_cadence',
            'delete_saved_search',
        ):
            payload = {'next': 'https://example.com/not-a-local-destination'}
            if url_name == 'update_saved_search_cadence':
                payload['notify_frequency'] = 'daily'
            response = self.client.post(reverse(url_name, args=[target.pk]), payload)
            self.assertRedirects(
                response,
                reverse('saved_search_list'),
                fetch_redirect_response=False,
            )

    def test_action_forms_carry_the_current_page(self):
        response = self.client.get(reverse('saved_search_list'), {'page': 2})
        self.assertContains(
            response,
            'name="next" value="/listings/saved-searches/?page=2"',
            count=3,
            html=False,
        )
