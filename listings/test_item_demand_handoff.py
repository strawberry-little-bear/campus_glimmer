# -*- coding: utf-8 -*-
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusLocation, Category, DemandPost


class ItemSearchToDemandHandoffTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='search-demand-user', password='safe-password-123',
        )
        cls.category = Category.objects.create(name='数码设备')
        cls.location = CampusLocation.objects.create(name='图书馆大厅')

    def _handoff_query(self, **overrides):
        params = {
            'q': '不存在的键盘',
            'category': self.category.id,
            'location': self.location.id,
            'condition': '九成新',
            'min_price': '50',
            'max_price': '200',
        }
        params.update(overrides)
        return urlencode(params)

    def _handoff_url(self, **overrides):
        return f"{reverse('new_demand')}?{self._handoff_query(**overrides)}"

    def test_filtered_empty_state_links_to_prefilled_demand_form(self):
        response = self.client.get(
            reverse('item_list_by_category', args=[self.category.id]),
            {
                'q': '不存在的键盘',
                'location': self.location.id,
                'condition': '九成新',
                'min_price': '50',
                'max_price': '200',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("new_demand")}?')
        prefill = parse_qs(response.context['demand_prefill_query'])
        self.assertEqual(prefill['q'], ['不存在的键盘'])
        self.assertEqual(prefill['category'], [str(self.category.id)])
        self.assertEqual(prefill['location'], [str(self.location.id)])
        self.assertEqual(prefill['condition'], ['九成新'])
        self.assertEqual(prefill['min_price'], ['50'])
        self.assertEqual(prefill['max_price'], ['200'])

    def test_demand_form_receives_allow_listed_search_values(self):
        self.client.force_login(self.user)
        response = self.client.get(self._handoff_url())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['demand_prefill_applied'])
        self.assertContains(response, '已带上刚才的搜索条件')
        form = response.context['form']
        self.assertEqual(form.initial['title'], '不存在的键盘')
        self.assertEqual(str(form.initial['category']), str(self.category.id))
        self.assertEqual(str(form.initial['location']), str(self.location.id))
        self.assertEqual(form.initial['min_price'], Decimal('50'))
        self.assertEqual(form.initial['max_price'], Decimal('200'))
        self.assertIn('期望成色：九成新', form.initial['description'])

    def test_direct_visit_keeps_the_form_empty(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('new_demand'))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['demand_prefill_applied'])
        form = response.context['form']
        self.assertNotIn('title', form.initial)
        self.assertNotIn('description', form.initial)
        self.assertNotIn('category', form.initial)
        self.assertNotIn('location', form.initial)

    def test_invalid_handoff_values_are_ignored(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('new_demand'), {
            'q': '   ',
            'condition': '   ',
            'category': 'not-an-id',
            'location': '999999',
            'min_price': '-10',
            'max_price': 'not-a-price',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['demand_prefill_applied'])
        form = response.context['form']
        self.assertNotIn('title', form.initial)
        self.assertNotIn('description', form.initial)
        self.assertNotIn('category', form.initial)
        self.assertNotIn('location', form.initial)
        self.assertNotIn('min_price', form.initial)
        self.assertNotIn('max_price', form.initial)

    def test_zero_price_is_not_lost_in_the_handoff(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('new_demand'), {
            'min_price': '0',
            'max_price': '0',
        })

        form = response.context['form']
        self.assertEqual(form.initial['min_price'], Decimal('0'))
        self.assertEqual(form.initial['max_price'], Decimal('0'))
        self.assertTrue(response.context['demand_prefill_applied'])

    def test_anonymous_visitor_keeps_full_prefill_after_login(self):
        target = self._handoff_url()
        response = self.client.get(target)

        self.assertEqual(response.status_code, 302)
        login_target = urlsplit(response.url)
        self.assertEqual(login_target.path, reverse('login'))
        self.assertEqual(parse_qs(login_target.query)['next'], [target])

    def test_post_values_override_url_prefill_values(self):
        self.client.force_login(self.user)
        response = self.client.post(self._handoff_url(), {
            'title': '以表单提交为准',
            'description': '用户最终填写的需求说明。',
            'category': self.category.id,
            'location': self.location.id,
            'min_price': '10',
            'max_price': '20',
            'expires_at': (timezone.now() + timedelta(days=10)).strftime('%Y-%m-%dT%H:%M'),
        })

        demand = DemandPost.objects.get(requester=self.user)
        self.assertRedirects(response, reverse('demand_detail', args=[demand.id]))
        self.assertEqual(demand.title, '以表单提交为准')
        self.assertEqual(demand.description, '用户最终填写的需求说明。')
