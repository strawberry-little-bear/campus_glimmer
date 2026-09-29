from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .circular_impact import build_circular_impact_report
from .models import CampusLocation, Category, Item, Order


class CircularImpactTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='impact-owner', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='impact-partner', password='safe-password-123',
        )
        self.category = Category.objects.create(name='循环报告分类')
        self.location = CampusLocation.objects.create(name='循环报告地点')

    def create_item(self, title, *, trade_mode='sale'):
        return Item.objects.create(
            seller=self.user,
            title=title,
            description='用于循环影响力报告测试',
            trade_mode=trade_mode,
            price='20.00',
            category=self.category,
            location=self.location,
            condition='八成新',
        )

    def create_order(self, item, *, status, updated_at=None):
        order = Order.objects.create(
            item=item,
            buyer=self.other,
            seller=self.user,
            agreed_price='20.00',
            status=status,
        )
        if updated_at:
            Order.objects.filter(pk=order.pk).update(updated_at=updated_at)
        return order

    def test_report_counts_only_verified_circulation_and_builds_breakdowns(self):
        now = timezone.now()
        self.create_order(
            self.create_item('完成出售'), status='completed',
            updated_at=now - timedelta(days=10),
        )
        self.create_order(
            self.create_item('完成赠送', trade_mode='free'), status='completed',
            updated_at=now - timedelta(days=35),
        )
        self.create_order(
            self.create_item('完成借用归还', trade_mode='borrow'), status='returned',
            updated_at=now - timedelta(days=70),
        )
        self.create_order(self.create_item('取消订单'), status='cancelled')
        self.create_order(self.create_item('仍在进行'), status='borrowed')

        report = build_circular_impact_report(self.user, months=6)

        self.assertEqual(report['total_orders'], 3)
        self.assertEqual(report['unique_items'], 3)
        self.assertEqual(report['sale_count'], 1)
        self.assertEqual(report['gift_count'], 1)
        self.assertEqual(report['borrow_return_count'], 1)
        self.assertEqual(report['participant_count'], 1)
        self.assertEqual(report['impact_score'], 30)
        self.assertEqual(report['categories'][0], {'name': '循环报告分类', 'count': 3})
        self.assertEqual(report['locations'][0], {'name': '循环报告地点', 'count': 3})
        self.assertEqual(sum(point['count'] for point in report['monthly']), 3)
        self.assertTrue(report['has_activity'])

    def test_report_handles_empty_state_and_clamps_period(self):
        report = build_circular_impact_report(self.user, months=99)

        self.assertEqual(report['total_orders'], 0)
        self.assertEqual(report['impact_score'], 0)
        self.assertEqual(len(report['monthly']), 12)
        self.assertFalse(report['has_activity'])
        self.assertTrue(all(point['percent'] == 0 for point in report['monthly']))

    def test_report_page_requires_login_and_renders_privacy_copy(self):
        response = self.client.get(reverse('circular_impact_report'))
        self.assertRedirects(
            response,
            reverse('login') + '?next=' + reverse('circular_impact_report'),
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse('circular_impact_report'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园循环影响力')
        self.assertContains(response, '只统计已经完成或完成归还的订单')
        self.assertContains(response, '影响力分')
