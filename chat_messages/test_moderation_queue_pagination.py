# -*- coding: utf-8 -*-
"""内容审核队列的分页、筛选保留与操作回跳测试。"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from listings.models import Category, Item
from .models import ModerationAppeal, ModerationEvent


class ModerationQueuePaginationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='pagination-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='pagination-buyer', password='safe-password-123',
        )
        self.staff = User.objects.create_user(
            username='pagination-staff', password='safe-password-123', is_staff=True,
        )
        category = Category.objects.create(name='审核分页测试')
        self.item = Item.objects.create(
            title='审核分页测试商品', description='测试商品', price='10.00',
            category=category, condition='九成新', seller=self.seller,
        )
        self.client.login(username='pagination-staff', password='safe-password-123')

    def create_events(self, count, **overrides):
        defaults = {
            'channel': 'comment',
            'author': self.buyer,
            'item': self.item,
            'content': '待审核内容',
            'matched_terms': '外部链接',
            'risk_score': 20,
            'risk_level': 'low',
            'risk_tier': 'caution',
            'status': 'pending',
        }
        defaults.update(overrides)
        events = []
        for index in range(count):
            event_data = defaults.copy()
            event_data['content'] = f"{defaults['content']} {index + 1:02d}"
            events.append(ModerationEvent(**event_data))
        return ModerationEvent.objects.bulk_create(events)

    def test_queue_loads_twelve_records_per_page(self):
        self.create_events(25)
        url = reverse('moderation_queue')

        first = self.client.get(url)
        second = self.client.get(url, {'page': 2})
        third = self.client.get(url, {'page': 3})

        self.assertEqual(len(first.context['moderation_events']), 12)
        self.assertEqual(len(second.context['moderation_events']), 12)
        self.assertEqual(len(third.context['moderation_events']), 1)
        self.assertEqual(first.context['moderation_filtered_count'], 25)
        self.assertContains(first, '第 1–12 条 · 共 25 条')
        self.assertContains(third, '第 25–25 条 · 共 25 条')
        self.assertContains(first, 'aria-label="内容审核分页"')

    def test_invalid_and_out_of_range_pages_are_clamped(self):
        self.create_events(25)
        url = reverse('moderation_queue')

        invalid = self.client.get(url, {'page': 'not-a-number'})
        too_large = self.client.get(url, {'page': 999})

        self.assertEqual(invalid.context['page_obj'].number, 1)
        self.assertEqual(too_large.context['page_obj'].number, 3)
        self.assertEqual(len(too_large.context['moderation_events']), 1)

    def test_page_links_keep_non_default_filters(self):
        events = self.create_events(
            13,
            author=None,
            item=None,
            content='关键词审核记录',
            risk_level='high',
            risk_tier='elevated',
        )
        ModerationAppeal.objects.bulk_create([
            ModerationAppeal(
                event=event,
                appellant=self.buyer,
                reason='missing_context',
                statement='一条待处理申诉',
            )
            for event in events
        ])
        response = self.client.get(reverse('moderation_queue'), {
            'status': 'all',
            'channel': 'comment',
            'risk': 'high',
            'tier': 'elevated',
            'appeal': 'pending',
            'release': 'missing_material',
            'q': '关键词',
        })

        expected_query = (
            'status=all&channel=comment&risk=high&tier=elevated&'
            'appeal=pending&release=missing_material&q=%E5%85%B3%E9%94%AE%E8%AF%8D'
        )
        self.assertEqual(response.context['moderation_filter_query'], expected_query)
        rendered_query = expected_query.replace('&', '&amp;')
        self.assertContains(
            response,
            f'?page=2&amp;{rendered_query}',
            html=False,
        )

    def test_default_filter_is_not_repeated_in_page_links(self):
        self.create_events(13)
        response = self.client.get(reverse('moderation_queue'))

        self.assertEqual(response.context['moderation_filter_query'], '')
        self.assertContains(response, 'href="?page=2"', html=False)

    def test_pagination_is_hidden_when_queue_fits_on_one_page(self):
        self.create_events(12)
        response = self.client.get(reverse('moderation_queue'))

        self.assertNotContains(response, 'aria-label="内容审核分页"')
        self.assertContains(response, '第 1–12 条 · 共 12 条')

    def test_empty_filtered_queue_renders_empty_state(self):
        response = self.client.get(reverse('moderation_queue'), {'q': '不存在的关键词'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '当前筛选下没有审核记录')
        self.assertNotContains(response, 'aria-label="内容审核分页"')

    def test_review_action_returns_to_the_paginated_queue(self):
        event = self.create_events(1)[0]
        next_url = f"{reverse('moderation_queue')}?status=all&page=2"

        response = self.client.post(
            reverse('review_moderation_event', args=[event.id]),
            {'decision': 'confirmed', 'next': next_url},
        )

        self.assertRedirects(response, next_url)
        event.refresh_from_db()
        self.assertEqual(event.status, 'confirmed')
