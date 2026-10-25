from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, Order


class MyOrdersWorkspaceTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(username='trade-buyer', password='safe-password-123')
        self.seller = User.objects.create_user(username='trade-seller', password='safe-password-123')
        self.other = User.objects.create_user(username='trade-other', password='safe-password-123')
        self.category = Category.objects.create(name='交易工作台分类')

    def make_item(self, title, seller):
        return Item.objects.create(
            title=title,
            description='交易工作台测试商品',
            price='36.00',
            category=self.category,
            condition='9成新',
            seller=seller,
        )

    def make_order(self, title, *, buyer, seller, status, **kwargs):
        return Order.objects.create(
            item=self.make_item(title, seller),
            buyer=buyer,
            seller=seller,
            agreed_price='36.00',
            status=status,
            **kwargs,
        )

    def test_login_is_required(self):
        response = self.client.get(reverse('my_orders'))

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('my_orders')}")

    def test_counts_include_buying_and_selling_but_hide_other_users(self):
        self.make_order('我买入并等待确认', buyer=self.buyer, seller=self.seller, status='pending')
        self.make_order('我买入且已完成', buyer=self.buyer, seller=self.seller, status='completed')
        self.make_order('我卖出的借用中商品', buyer=self.other, seller=self.buyer, status='borrowed')
        self.make_order('我卖出的已取消商品', buyer=self.other, seller=self.buyer, status='cancelled')
        self.make_order('与我无关的订单', buyer=self.other, seller=self.seller, status='pending')
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('my_orders'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['order_counts'],
            {
                'all': 4,
                'active': 2,
                'completed': 1,
                'cancelled': 1,
                'buying': 2,
                'selling': 2,
            },
        )
        self.assertContains(response, '我买入并等待确认')
        self.assertContains(response, '我卖出的借用中商品')
        self.assertNotContains(response, '与我无关的订单')
        self.assertContains(response, '我买到')
        self.assertContains(response, '我卖出')
        self.assertContains(response, '买入 2 · 卖出 2')

    def test_state_tabs_filter_the_current_users_order_snapshot(self):
        self.make_order('待确认交易', buyer=self.buyer, seller=self.seller, status='pending')
        self.make_order('已完成交易', buyer=self.buyer, seller=self.seller, status='completed')
        self.make_order('借用中交易', buyer=self.other, seller=self.buyer, status='borrowed')
        self.make_order('已取消交易', buyer=self.buyer, seller=self.seller, status='cancelled')
        self.client.force_login(self.buyer)

        active = self.client.get(reverse('my_orders'), {'state': 'active'})
        completed = self.client.get(reverse('my_orders'), {'state': 'completed'})
        cancelled = self.client.get(reverse('my_orders'), {'state': 'cancelled'})
        invalid = self.client.get(reverse('my_orders'), {'state': 'unknown'})

        self.assertEqual(active.context['selected_state'], 'active')
        self.assertEqual({order.item.title for order in active.context['orders']}, {'待确认交易', '借用中交易'})
        self.assertEqual([order.item.title for order in completed.context['orders']], ['已完成交易'])
        self.assertEqual([order.item.title for order in cancelled.context['orders']], ['已取消交易'])
        self.assertEqual(invalid.context['selected_state'], 'all')
        self.assertEqual(len(invalid.context['orders']), 4)

    def test_next_step_and_overdue_deadline_are_explicit(self):
        self.make_order(
            '超时待确认交易',
            buyer=self.buyer,
            seller=self.seller,
            status='pending',
            confirmation_deadline=timezone.now() - timedelta(hours=1),
        )
        self.make_order(
            '待归还借用交易',
            buyer=self.other,
            seller=self.buyer,
            status='borrowed',
            return_due_at=timezone.now() + timedelta(days=2),
        )
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('my_orders'))

        self.assertContains(response, '等待卖家确认预约')
        self.assertContains(response, '关注归还时间并确认收回')
        self.assertContains(response, '卖家确认截止')
        self.assertContains(response, '预计归还')
        self.assertContains(response, '已超时')
        self.assertEqual(sum(order.is_overdue for order in response.context['orders']), 1)

    def test_orders_are_paginated_without_losing_filter_or_full_counts(self):
        for index in range(13):
            self.make_order(
                f'分页待确认交易 {index:02d}',
                buyer=self.buyer,
                seller=self.seller,
                status='pending',
            )
        self.client.force_login(self.buyer)

        first_page = self.client.get(reverse('my_orders'), {'state': 'active'})
        second_page = self.client.get(reverse('my_orders'), {'state': 'active', 'page': 2})

        self.assertEqual(first_page.context['page_obj'].paginator.count, 13)
        self.assertEqual(len(first_page.context['orders']), 10)
        self.assertEqual(second_page.context['page_obj'].number, 2)
        self.assertEqual(len(second_page.context['orders']), 3)
        self.assertEqual(first_page.context['order_counts']['all'], 13)
        self.assertEqual(first_page.context['order_counts']['active'], 13)
        self.assertContains(first_page, '第 1–10 笔 · 共 13 笔')
        self.assertContains(second_page, '第 11–13 笔 · 共 13 笔')
        self.assertContains(second_page, '?state=active&amp;page=1', html=False)

    def test_out_of_range_page_falls_back_to_last_page(self):
        for index in range(11):
            self.make_order(
                f'末页交易 {index:02d}',
                buyer=self.buyer,
                seller=self.seller,
                status='completed',
            )
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('my_orders'), {'state': 'completed', 'page': 99})

        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(len(response.context['orders']), 1)
        self.assertContains(response, '第 11–11 笔 · 共 11 笔')

    def test_empty_workspace_keeps_counts_tabs_and_discovery_entry(self):
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('my_orders'))

        self.assertEqual(
            response.context['order_counts'],
            {
                'all': 0,
                'active': 0,
                'completed': 0,
                'cancelled': 0,
                'buying': 0,
                'selling': 0,
            },
        )
        self.assertContains(response, '还没有交易记录')
        self.assertContains(response, '全部交易记录')
        self.assertContains(response, '去发现好物')
