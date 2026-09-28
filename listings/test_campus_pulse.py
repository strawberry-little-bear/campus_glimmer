from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campus_pulse import build_campus_pulse
from .models import CampusLocation, Category, DemandPost, Item, Order


class CampusPulseTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='pulse-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='pulse-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='热力测试分类')
        self.location = CampusLocation.objects.create(
            name='北区图书馆大厅', building='北校区', is_public=True,
        )

    def _item(self, *, status='available', title='热力商品'):
        return Item.objects.create(
            title=title,
            description='用于校园供需热力测试',
            price='18.00',
            category=self.category,
            condition='良好',
            seller=self.seller,
            location=self.location,
            status=status,
        )

    def test_aggregates_supply_demand_and_activity(self):
        item = self._item()
        DemandPost.objects.create(
            requester=self.buyer,
            title='求购热力商品',
            description='希望在北区交付',
            category=self.category,
            location=self.location,
            status='active',
        )
        Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.location,
            agreed_price='18.00',
            status='completed',
        )

        pulse = build_campus_pulse()

        self.assertEqual(pulse['summary']['location_count'], 1)
        self.assertEqual(pulse['summary']['available_supply'], 1)
        self.assertEqual(pulse['summary']['active_demands'], 1)
        self.assertEqual(pulse['summary']['period_orders'], 1)
        row = pulse['rows'][0]
        self.assertEqual(row['completed_orders'], 1)
        self.assertEqual(row['heat_percent'], 100)

    def test_demand_heavier_than_supply_is_marked_high_pressure(self):
        for index in range(3):
            DemandPost.objects.create(
                requester=self.buyer,
                title=f'紧张求购{index}',
                description='需求测试',
                category=self.category,
                location=self.location,
                status='active',
            )

        pulse = build_campus_pulse()

        row = pulse['rows'][0]
        self.assertEqual(row['available_supply'], 0)
        self.assertEqual(row['active_demands'], 3)
        self.assertEqual(row['pressure'], 'high')
        self.assertEqual(row['pressure_label'], '需求紧张')
        self.assertEqual(pulse['summary']['high_pressure_count'], 1)

    def test_expired_and_inactive_records_do_not_count_as_current_supply_or_demand(self):
        item = self._item()
        item.expires_at = timezone.now() - timedelta(hours=1)
        item.save(update_fields=['expires_at'])
        DemandPost.objects.create(
            requester=self.buyer,
            title='已关闭求购',
            description='不应进入当前统计',
            category=self.category,
            location=self.location,
            status='closed',
        )

        pulse = build_campus_pulse()

        self.assertEqual(len(pulse['rows']), 1)
        self.assertEqual(pulse['rows'][0]['available_supply'], 0)
        self.assertEqual(pulse['rows'][0]['active_demands'], 0)
        self.assertEqual(pulse['summary']['location_count'], 1)

    def test_empty_dataset_returns_empty_rows(self):
        self.assertEqual(build_campus_pulse()['rows'], [])

    def test_operations_dashboard_renders_campus_pulse(self):
        self._item()
        admin = User.objects.create_user(
            username='pulse-admin', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园供需热力')
        self.assertContains(response, '北区图书馆大厅')
