from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import BrowsingHistory, Category, Favorite, Item, Order
from .price_insights import build_price_insight


class PriceInsightTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='price-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='price-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='价格测试分类')
        self.item = Item.objects.create(
            title='价格参考商品', description='用于测试价格参考', price='20.00',
            category=self.category, condition='良好', seller=self.seller,
        )

    def _create_comparables(self, prices):
        for index, price in enumerate(prices):
            Item.objects.create(
                title=f'同类商品{index}', description='同类样本', price=price,
                category=self.category, condition='良好', seller=self.seller, status='sold',
            )

    def test_below_market_price_returns_explainable_range(self):
        self._create_comparables(['40.00', '50.00', '60.00'])

        insight = build_price_insight(self.item)

        self.assertIsNotNone(insight)
        self.assertEqual(insight.comparable_count, 3)
        self.assertEqual(insight.median_price, Decimal('50.00'))
        self.assertEqual(insight.position, 'below')
        self.assertEqual(insight.position_label, '低于同类常见区间')
        self.assertEqual(insight.confidence, 'medium')

    def test_demand_signal_combines_views_favorites_and_active_order(self):
        self._create_comparables(['40.00', '50.00', '60.00'])
        BrowsingHistory.objects.create(user=self.buyer, item=self.item, view_count=15)
        Favorite.objects.create(user=self.buyer, item=self.item)
        Order.objects.create(
            item=self.item, buyer=self.buyer, seller=self.seller,
            agreed_price='20.00', status='pending',
        )

        insight = build_price_insight(self.item)

        self.assertEqual(insight.demand_level, 'hot')
        self.assertIn('15 次浏览', insight.demand_detail)
        self.assertIn('已有进行中的预约', insight.demand_detail)

    def test_insight_is_not_available_for_free_items_or_without_samples(self):
        self.assertIsNone(build_price_insight(self.item))

        self.item.trade_mode = 'free'
        self.item.price = 0
        self.item.save(update_fields=['trade_mode', 'price'])
        self.assertIsNone(build_price_insight(self.item))

    def test_item_detail_renders_price_reference_card(self):
        self._create_comparables(['40.00', '50.00', '60.00'])

        response = self.client.get(reverse('item_detail', args=[self.item.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '数据化参考')
        self.assertContains(response, '低于同类常见区间')
        self.assertContains(response, '同类中位数')
