# -*- coding: utf-8 -*-
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .category_price_reference import build_category_price_reference
from .models import Category, Item


class CategoryPriceReferenceTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.seller = User.objects.create_user(
            username='category-price-seller', password='safe-password-123',
        )

    def make_item(
        self, *, category, price, status='available', created_at=None,
        expires_at=None, trade_mode='sale', title='分类价格商品',
    ):
        item = Item.objects.create(
            title=title,
            description='用于分类价格参考测试',
            trade_mode=trade_mode,
            price=Decimal(str(price)),
            category=category,
            condition='良好',
            seller=self.seller,
            status=status,
            expires_at=expires_at,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=created_at or self.now,
        )
        return item

    def test_aggregates_recent_samples_and_current_positions(self):
        category = Category.objects.create(name='电子参考分类')
        self.make_item(category=category, price='5', title='低价在售')
        self.make_item(category=category, price='20', title='区间内在售')
        self.make_item(category=category, price='100', title='高价在售')
        self.make_item(category=category, price='10', status='reserved', title='价格证据')

        result = build_category_price_reference(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['name'], '电子参考分类')
        self.assertEqual(row['sample_count'], 4)
        self.assertEqual(row['sample_scope'], '近 180 天')
        self.assertEqual(row['median_price'], Decimal('15.00'))
        self.assertEqual(row['lower_price'], Decimal('8.75'))
        self.assertEqual(row['upper_price'], Decimal('40.00'))
        self.assertEqual(row['active_count'], 3)
        self.assertEqual(
            (row['below_count'], row['within_count'], row['above_count']), (1, 1, 1),
        )
        self.assertEqual(row['out_of_range_count'], 2)
        self.assertEqual(row['out_of_range_share'], Decimal('66.7'))
        self.assertEqual(row['confidence'], 'medium')
        self.assertTrue(row['is_attention_needed'])
        self.assertTrue(row['is_wide_range'])
        self.assertEqual(result['summary']['out_of_range_item_total'], 2)

    def test_reserved_sold_and_expired_do_not_enter_current_action_counts(self):
        category = Category.objects.create(name='当前供给分类')
        self.make_item(category=category, price='10', status='sold', title='旧样本一')
        self.make_item(category=category, price='20', status='sold', title='旧样本二')
        self.make_item(category=category, price='30', status='sold', title='旧样本三')
        self.make_item(category=category, price='40', status='sold', title='旧样本四')
        self.make_item(category=category, price='50', status='reserved', title='高价证据')
        self.make_item(category=category, price='90', title='当前高价')
        self.make_item(
            category=category, price='80', expires_at=self.now - timedelta(minutes=1),
            title='已过期',
        )
        self.make_item(
            category=category, price='300', trade_mode='free', title='免费赠送',
        )
        self.make_item(
            category=category, price='0', title='零元出售',
        )

        row = build_category_price_reference(days=30, now=self.now)['rows'][0]

        self.assertEqual(row['sample_count'], 7)
        self.assertEqual(row['active_count'], 1)
        self.assertEqual(row['above_count'], 1)
        self.assertEqual(row['below_count'], 0)
        self.assertEqual(row['within_count'], 0)
        self.assertEqual(row['out_of_range_count'], 1)

    def test_historical_fallback_and_small_sample_do_not_invent_share(self):
        category = Category.objects.create(name='历史参考分类')
        self.make_item(
            category=category, price='50',
            created_at=self.now - timedelta(days=200), title='历史在售',
        )

        row = build_category_price_reference(days=30, now=self.now)['rows'][0]

        self.assertEqual(row['sample_scope'], '历史样本')
        self.assertEqual(row['sample_count'], 1)
        self.assertEqual(row['confidence'], 'low')
        self.assertTrue(row['is_widened'])
        self.assertEqual(row['lower_price'], Decimal('45.00'))
        self.assertEqual(row['upper_price'], Decimal('55.00'))
        self.assertEqual(row['within_count'], 1)
        self.assertIsNone(row['out_of_range_share'])
        self.assertIn('样本不足', row['status_label'])

    def test_degenerate_quartiles_are_widened_and_sorted_by_actionable_supply(self):
        narrow = Category.objects.create(name='A 窄区间分类')
        busy = Category.objects.create(name='B 集中偏离分类')
        self.make_item(category=narrow, price='30', title='窄区间一')
        self.make_item(category=narrow, price='30', title='窄区间二')
        for index, price in enumerate(('10', '20', '30', '40')):
            self.make_item(
                category=busy, price=price, status='sold', title=f'_BUSY_SOLD_{index}',
            )
        self.make_item(category=busy, price='100', title='集中高价一')
        self.make_item(category=busy, price='100', title='集中高价二')

        result = build_category_price_reference(days=30, now=self.now)

        self.assertEqual(
            [row['name'] for row in result['rows']], ['B 集中偏离分类', 'A 窄区间分类'],
        )
        narrow_row = result['rows'][1]
        self.assertTrue(narrow_row['is_widened'])
        self.assertEqual(narrow_row['lower_price'], Decimal('27.00'))
        self.assertEqual(narrow_row['upper_price'], Decimal('33.00'))
        self.assertEqual(narrow_row['within_count'], 2)
        self.assertEqual(result['summary']['top_category_name'], 'B 集中偏离分类')

    def test_empty_state_and_builder_do_not_write_business_rows(self):
        category = Category.objects.create(name='空分类')
        result = build_category_price_reference(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['rows'], [])
        self.assertIsNone(result['summary']['top_category_name'])
        self.assertEqual(category.name, '空分类')

    def test_page_and_export_render_category_level_privacy_boundary(self):
        staff = User.objects.create_user(
            username='category-price-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)
        category = Category.objects.create(name='导出参考分类')
        self.make_item(category=category, price='18', title='不应出现在面板里的商品')

        page = self.client.get(reverse('operations_dashboard'))
        export = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '分类调价参考')
        self.assertContains(page, '导出参考分类')
        self.assertContains(page, '当前价格快照')
        section = page.content.decode('utf-8').split('id="category-price-reference"', 1)[1]
        section = section.split('</section>', 1)[0]
        self.assertNotIn('不应出现在面板里的商品', section)
        text = export.content.decode('utf-8-sig')
        self.assertIn('分类调价参考', text)
        self.assertIn('分类,样本范围,样本数,中位数,常见区间下限,常见区间上限', text)
        self.assertIn('导出参考分类', text)
        category_section = text.split('分类调价参考', 1)[1].split('出售价格轨迹质量', 1)[0]
        self.assertNotIn('不应出现在面板里的商品', category_section)
