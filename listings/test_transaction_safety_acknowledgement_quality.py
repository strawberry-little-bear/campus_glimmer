# -*- coding: utf-8 -*-
"""为「交易安全卡确认质量」钉住窗口、版本与隐私口径。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, CampusLocation, Item, Order, OrderSafetyAcknowledgement
from .transaction_safety import build_transaction_safety, transaction_safety_fingerprint
from .transaction_safety_acknowledgement_quality import (
    build_transaction_safety_acknowledgement_quality,
)


def local_noon():
    return timezone.localtime(timezone.now()).replace(
        hour=12, minute=0, second=0, microsecond=0,
    )


class TransactionSafetyAcknowledgementQualityTests(TestCase):
    def setUp(self):
        self.now = local_noon()
        self.buyer = User.objects.create_user(
            username='safety-quality-buyer', password='safe-password-123',
        )
        self.seller = User.objects.create_user(
            username='safety-quality-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='安全确认质量分类')
        self.location = CampusLocation.objects.create(
            name='安全确认质量地点', is_public=True,
        )
        self.sequence = 0

    def make_item(self, title=None):
        self.sequence += 1
        return Item.objects.create(
            title=title or f'安全确认质量商品 {self.sequence}',
            description='用于安全卡确认质量统计', price='20.00',
            category=self.category, location=self.location,
            condition='良好', seller=self.seller,
        )

    def make_order(self, *, days_ago=1, status='confirmed'):
        item = self.make_item()
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='20.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        order.refresh_from_db()
        return order

    def acknowledge(self, order, role, *, hours_ago=None, fingerprint=None):
        record = OrderSafetyAcknowledgement.objects.create(
            order=order,
            role=role,
            fingerprint=fingerprint or transaction_safety_fingerprint(
                build_transaction_safety(order)
            ),
        )
        if hours_ago is not None:
            OrderSafetyAcknowledgement.objects.filter(pk=record.pk).update(
                acknowledged_at=self.now - timedelta(hours=hours_ago),
            )
            record.refresh_from_db()
        return record

    def test_empty_platform_keeps_zero_denominator_without_fake_rates(self):
        report = build_transaction_safety_acknowledgement_quality(
            days=30, now=self.now,
        )

        self.assertFalse(report['has_data'])
        self.assertEqual(report['coverage']['total'], 0)
        self.assertIsNone(report['coverage']['buyer_rate'])
        self.assertIsNone(report['coverage']['seller_rate'])
        self.assertIsNone(report['coverage']['both_rate'])
        self.assertEqual(report['pending']['total'], 0)

    def test_current_coverage_is_split_by_role(self):
        buyer_only = self.make_order()
        seller_only = self.make_order()
        both = self.make_order()
        self.make_order()

        self.acknowledge(buyer_only, 'buyer', hours_ago=4)
        self.acknowledge(seller_only, 'seller', hours_ago=3)
        self.acknowledge(both, 'buyer', hours_ago=2)
        self.acknowledge(both, 'seller', hours_ago=1)

        report = build_transaction_safety_acknowledgement_quality(
            days=30, now=self.now,
        )
        coverage = report['coverage']

        self.assertEqual(coverage['total'], 4)
        self.assertEqual(coverage['buyer_only_count'], 1)
        self.assertEqual(coverage['seller_only_count'], 1)
        self.assertEqual(coverage['both_count'], 1)
        self.assertEqual(coverage['neither_count'], 1)
        self.assertEqual(coverage['buyer_count'], 2)
        self.assertEqual(coverage['seller_count'], 2)
        self.assertEqual(coverage['buyer_rate'], 50.0)
        self.assertEqual(coverage['both_rate'], 25.0)

    def test_latency_only_uses_current_complete_chains(self):
        order = self.make_order(days_ago=1)
        self.acknowledge(order, 'buyer', hours_ago=2)
        self.acknowledge(order, 'seller', hours_ago=1)
        incomplete = self.make_order()
        self.acknowledge(incomplete, 'buyer', hours_ago=1)

        report = build_transaction_safety_acknowledgement_quality(
            days=30, now=self.now,
        )
        latency = report['latency']

        self.assertEqual(latency['completed_count'], 1)
        self.assertEqual(latency['incomplete_count'], 1)
        self.assertEqual(latency['first_median_label'], '22 小时')
        self.assertEqual(latency['complete_median_label'], '23 小时')
        self.assertEqual(latency['first_bucket_rows'][2]['count'], 1)
        self.assertEqual(latency['complete_bucket_rows'][2]['count'], 1)

    def test_changed_fingerprint_does_not_count_as_current_coverage(self):
        order = self.make_order()
        self.acknowledge(order, 'buyer', hours_ago=1)
        private_location = CampusLocation.objects.create(
            name='安全确认私人地点', is_public=False,
        )
        order.meeting_location = private_location
        order.save(update_fields=['meeting_location'])

        report = build_transaction_safety_acknowledgement_quality(
            days=30, now=self.now,
        )

        self.assertEqual(report['coverage']['buyer_count'], 0)
        self.assertEqual(report['coverage']['neither_count'], 1)
        self.assertEqual(report['pending']['total'], 1)
        self.assertEqual(report['pending']['rows'][0]['missing_roles_label'], '买家、卖家')
        self.assertEqual(report['versions']['current_pending_after_prior_count'], 1)

    def test_completed_and_out_of_period_orders_do_not_enter_current_denominator(self):
        old_active = self.make_order(days_ago=40)
        completed = self.make_order(days_ago=1, status='completed')
        self.acknowledge(completed, 'buyer', hours_ago=1)
        self.acknowledge(completed, 'seller', hours_ago=1)

        report = build_transaction_safety_acknowledgement_quality(
            days=30, now=self.now,
        )

        self.assertEqual(report['coverage']['total'], 0)
        self.assertEqual(report['latency']['completed_count'], 0)
        self.assertEqual(report['pending']['total'], 1)
        self.assertEqual(report['pending']['rows'][0]['order_id'], old_active.id)

    def test_version_count_is_an_observable_lower_bound(self):
        order = self.make_order()
        old_fingerprint = 'a' * 64
        self.acknowledge(order, 'buyer', hours_ago=3, fingerprint=old_fingerprint)
        self.acknowledge(order, 'buyer', hours_ago=2)
        self.acknowledge(order, 'seller', hours_ago=1)

        report = build_transaction_safety_acknowledgement_quality(
            days=30, now=self.now,
        )
        versions = report['versions']

        self.assertEqual(versions['observed_order_count'], 1)
        self.assertEqual(versions['one_version_count'], 0)
        self.assertEqual(versions['two_version_count'], 1)
        self.assertEqual(versions['three_or_more_count'], 0)
        self.assertEqual(versions['current_after_prior_count'], 1)

    def test_pending_list_is_ordered_truncated_and_rendered_without_usernames(self):
        staff = User.objects.create_user(
            username='safety-quality-staff', password='safe-password-123', is_staff=True,
        )
        for index in range(21):
            order = self.make_order()
            Order.objects.filter(pk=order.pk).update(
                created_at=self.now - timedelta(minutes=40 - index),
            )

        report = build_transaction_safety_acknowledgement_quality(
            days=30, now=self.now,
        )
        pending = report['pending']

        self.assertEqual(pending['total'], 21)
        self.assertEqual(len(pending['rows']), pending['row_limit'])
        self.assertTrue(pending['is_truncated'])
        self.assertEqual(pending['rows'][0]['missing_count'], 2)
        self.assertEqual(pending['missing_both_count'], 21)

        self.client.force_login(staff)
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '交易安全卡确认到哪一步了')
        self.assertContains(response, '当前共 21 笔')
        self.assertNotContains(response, 'safety-quality-buyer')
        self.assertNotContains(response, 'safety-quality-seller')

    def test_dashboard_and_export_are_staff_only_and_export_contains_panel(self):
        staff = User.objects.create_user(
            username='safety-quality-export-staff', password='safe-password-123',
            is_staff=True,
        )
        ordinary = User.objects.create_user(
            username='safety-quality-ordinary', password='safe-password-123',
        )
        self.make_order()

        self.client.force_login(ordinary)
        self.assertEqual(self.client.get(reverse('operations_dashboard')).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('operations_dashboard_export')).status_code, 403,
        )

        self.client.force_login(staff)
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(export.status_code, 200)
        self.assertIn('交易安全卡的双方确认', export.content.decode('utf-8'))
