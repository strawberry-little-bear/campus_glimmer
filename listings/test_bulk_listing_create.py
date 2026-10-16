from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusCampaign, CampusLocation, Category, Item


class BulkListingCreateTests(TestCase):
    """毕业季批量发布第一刀的服务端行为。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='graduation-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='毕业季闲置')
        self.location = CampusLocation.objects.create(name='南门快递点')
        now = timezone.now()
        self.campaign = CampusCampaign.objects.create(
            title='毕业季清仓',
            slug='graduation-clearance',
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=30),
            is_active=True,
        )
        self.url = reverse('bulk_create_items')

    def _row(self, index, *, title='', description='', trade_mode='sale',
             price=None, condition='良好', deposit='', borrow_days=''):
        prefix = f'form-{index}'
        data = {
            f'{prefix}-title': title,
            f'{prefix}-description': description,
            f'{prefix}-trade_mode': trade_mode,
            f'{prefix}-condition': condition,
            f'{prefix}-deposit_amount': deposit,
            f'{prefix}-borrow_days': borrow_days,
        }
        if price is not None:
            data[f'{prefix}-price'] = price
        return data

    def _payload(self, rows, *, expires_at=None):
        payload = {
            'category': self.category.id,
            'location': self.location.id,
            'campaign': self.campaign.id,
            'expires_at': expires_at or '',
            'form-TOTAL_FORMS': str(len(rows)),
            'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '20',
        }
        for index, row in enumerate(rows):
            payload.update(row)
        return payload

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_get_page_opens_with_initial_rows_and_limit(self):
        self.client.force_login(self.seller)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '批量发布商品')
        self.assertContains(response, '最多一次发布 20 件')
        self.assertEqual(len(response.context['formset'].forms), 5)
        self.assertEqual(response.context['max_bulk_rows'], 20)

    def test_creates_filled_rows_and_ignores_empty_rows(self):
        expires_at = timezone.now() + timedelta(days=10)
        expires_at_text = timezone.localtime(expires_at).strftime('%Y-%m-%dT%H:%M')
        self.client.force_login(self.seller)

        with (patch('listings.views.notify_saved_search_matches') as saved_mock,
              patch('listings.views.notify_demand_matches') as demand_mock):
            response = self.client.post(self.url, self._payload([
                self._row(0, title='数据结构教材', description='考研笔记齐全', price='18.50'),
                self._row(1, condition=''),
                self._row(2, title='台灯', description='宿舍使用一学期', trade_mode='free'),
                self._row(3, condition=''),
                self._row(4, title='折叠桌', description='搬运方便', trade_mode='borrow',
                          deposit='20.00', borrow_days='14'),
            ], expires_at=expires_at_text))

        self.assertRedirects(response, reverse('bulk_listing_image_queue'))
        items = Item.objects.order_by('title')
        self.assertEqual(items.count(), 3)
        self.assertEqual(
            list(items.values_list('title', flat=True)),
            ['台灯', '折叠桌', '数据结构教材'],
        )
        self.assertTrue(all(item.seller == self.seller for item in items))
        self.assertTrue(all(item.status == 'available' for item in items))
        self.assertTrue(all(item.category == self.category for item in items))
        self.assertTrue(all(item.location == self.location for item in items))
        self.assertTrue(all(item.campaign == self.campaign for item in items))
        # datetime-local 只精确到分钟；按站点时区核对同一批商品的共同截止时间。
        self.assertTrue(all(
            timezone.localtime(item.expires_at).strftime('%Y-%m-%dT%H:%M') == expires_at_text
            for item in items
        ))
        self.assertEqual(saved_mock.call_count, 3)
        self.assertEqual(demand_mock.call_count, 3)

        free_item = items.get(title='台灯')
        borrow_item = items.get(title='折叠桌')
        self.assertEqual(free_item.trade_mode, 'free')
        self.assertEqual(free_item.price, Decimal('0.00'))
        self.assertEqual(borrow_item.trade_mode, 'borrow')
        self.assertEqual(borrow_item.price, Decimal('0.00'))
        self.assertEqual(borrow_item.deposit_amount, Decimal('20.00'))
        self.assertEqual(borrow_item.borrow_days, 14)

    def test_invalid_row_writes_nothing(self):
        self.client.force_login(self.seller)

        response = self.client.post(self.url, self._payload([
            self._row(0, title='有效商品', description='这一行是有效的', price='10.00'),
            self._row(1, title='缺价格商品', description='这一行会让整批失败'),
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Item.objects.exists())
        self.assertContains(response, '出售商品需要填写价格。')
        self.assertContains(response, '任何一行不合格，本批次都不会写入。')

    def test_rejects_duplicate_titles_in_one_batch_without_writing(self):
        self.client.force_login(self.seller)

        response = self.client.post(self.url, self._payload([
            self._row(0, title='同一本教材', description='第一行', price='10.00'),
            self._row(1, title=' 同一本教材 ', description='第二行', price='12.00'),
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Item.objects.exists())
        self.assertContains(response, '同一批次内不能重复填写这个标题。')

    def test_rejects_more_than_twenty_rows(self):
        self.client.force_login(self.seller)

        response = self.client.post(self.url, self._payload([
            self._row(index, title=f'毕业清仓商品 {index}', description='批量边界测试',
                      price='5.00')
            for index in range(21)
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Item.objects.exists())
        self.assertTrue(response.context['formset'].non_form_errors)

    def test_requires_at_least_one_filled_row(self):
        self.client.force_login(self.seller)

        response = self.client.post(self.url, self._payload([
            self._row(index, condition='') for index in range(5)
        ]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Item.objects.exists())
        self.assertContains(response, '请至少填写一件商品。')

    def test_borrow_defaults_deposit_and_days(self):
        self.client.force_login(self.seller)

        with (patch('listings.views.notify_saved_search_matches'),
              patch('listings.views.notify_demand_matches')):
            response = self.client.post(self.url, self._payload([
                self._row(0, title='计算器', description='考试周可借', trade_mode='borrow'),
            ]))

        self.assertRedirects(response, reverse('bulk_listing_image_queue'))
        item = Item.objects.get(title='计算器')
        self.assertEqual(item.price, Decimal('0.00'))
        self.assertEqual(item.deposit_amount, Decimal('0.00'))
        self.assertEqual(item.borrow_days, 7)
