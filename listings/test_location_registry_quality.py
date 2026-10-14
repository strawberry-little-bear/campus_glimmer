# -*- coding: utf-8 -*-
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .location_registry_quality import (
    LIST_LIMIT, build_location_registry_quality,
)
from .models import (
    CampusLocation, Category, DemandPost, Item, MeetingAppointment, Order,
    SavedSearch,
)


class LocationRegistryQualityTests(TestCase):
    def setUp(self):
        self.now = timezone.now() + timedelta(seconds=1)
        self.user = User.objects.create_user(
            username='location-quality-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='地点档案质量分类')

    def make_location(self, name, **kwargs):
        return CampusLocation.objects.create(name=name, **kwargs)

    def make_item(self, location=None, **kwargs):
        options = {
            'title': '地点档案质量商品',
            'description': '用于地点档案质量测试',
            'category': self.category,
            'condition': '良好',
            'seller': self.user,
            'price': '12.00',
        }
        options.update(kwargs)
        if location is not None:
            options['location'] = location
        return Item.objects.create(**options)

    def make_order(self, *, item, location=None):
        return Order.objects.create(
            item=item,
            buyer=self.user,
            seller=self.user,
            agreed_price='12.00',
            meeting_location=location,
        )

    def make_appointment(self, order, location):
        return MeetingAppointment.objects.create(
            order=order,
            proposed_by=self.user,
            location=location,
            start_at=self.now + timedelta(hours=1),
            end_at=self.now + timedelta(hours=2),
        )

    def test_empty_platform_reports_zero_counts_and_no_shares(self):
        result = build_location_registry_quality(now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['summary']['location_total'], 0)
        self.assertEqual(result['coverage']['item_total'], 0)
        self.assertIsNone(result['coverage']['item_with_location_share'])
        self.assertEqual(result['field_rows'][0]['total'], 0)
        self.assertIsNone(result['field_rows'][0]['share'])
        self.assertEqual(result['delivery_rows'][3]['count'], 0)
        self.assertIsNone(result['delivery_rows'][3]['share'])

    def test_switch_matrix_is_mutually_exclusive_and_conserves_totals(self):
        self.make_location('启用公共', is_active=True, is_public=True)
        self.make_location('启用私有', is_active=True, is_public=False)
        self.make_location('停用公共', is_active=False, is_public=True)

        summary = build_location_registry_quality(now=self.now)['summary']

        self.assertEqual(summary['location_total'], 3)
        self.assertEqual(summary['active_total'], 2)
        self.assertEqual(summary['inactive_total'], 1)
        self.assertEqual(summary['active_public_total'], 1)
        self.assertEqual(summary['active_private_total'], 1)
        self.assertEqual(summary['inactive_public_total'], 1)

    def test_field_rows_cover_all_registry_fields(self):
        result = build_location_registry_quality(now=self.now)

        self.assertEqual(
            [(row['field'], row['scope']) for row in result['field_rows']],
            [
                ('building', '启用地点'),
                ('address', '启用地点'),
                ('description', '启用地点'),
                ('safety_note', '公共地点'),
                ('safety_note', '非公共地点'),
            ],
        )

    def test_safety_note_coverage_is_split_by_public_scope(self):
        self.make_location('公共有提示', is_public=True, safety_note='白天开放')
        self.make_location('公共缺提示', is_public=True)
        self.make_location('私有缺提示', is_public=False)

        rows = {
            row['scope']: row
            for row in build_location_registry_quality(now=self.now)['field_rows']
            if row['field'] == 'safety_note'
        }

        self.assertEqual(rows['公共地点']['total'], 2)
        self.assertEqual(rows['公共地点']['filled'], 1)
        self.assertEqual(rows['公共地点']['share'], 50.0)
        self.assertEqual(rows['非公共地点']['total'], 1)
        self.assertEqual(rows['非公共地点']['filled'], 0)
        self.assertEqual(rows['非公共地点']['share'], 0.0)

    def test_current_items_without_location_are_counted_separately(self):
        location = self.make_location('有地点')
        self.make_item(location=location)
        self.make_item(title='没有地点的商品')

        coverage = build_location_registry_quality(now=self.now)['coverage']

        self.assertEqual(coverage['item_total'], 2)
        self.assertEqual(coverage['item_with_location'], 1)
        self.assertEqual(coverage['item_without_location'], 1)
        self.assertEqual(coverage['item_with_location_share'], 50.0)

    def test_active_demands_without_location_are_counted_separately(self):
        location = self.make_location('求购地点')
        DemandPost.objects.create(
            requester=self.user, title='有地点求购', description='有地点',
            category=self.category, location=location,
        )
        DemandPost.objects.create(
            requester=self.user, title='无地点求购', description='没有地点',
            category=self.category,
        )

        coverage = build_location_registry_quality(now=self.now)['coverage']

        self.assertEqual(coverage['demand_total'], 2)
        self.assertEqual(coverage['demand_with_location'], 1)
        self.assertEqual(coverage['demand_without_location'], 1)

    def test_active_saved_searches_bound_to_inactive_locations_are_counted(self):
        active = self.make_location('继续启用')
        inactive = self.make_location('已经停用', is_active=False)
        SavedSearch.objects.create(
            user=self.user, name='正常关注', location=active,
        )
        SavedSearch.objects.create(
            user=self.user, name='停用关注', location=inactive,
        )
        SavedSearch.objects.create(user=self.user, name='全站关注')

        coverage = build_location_registry_quality(now=self.now)['coverage']

        self.assertEqual(coverage['saved_search_total'], 3)
        self.assertEqual(coverage['saved_search_active_location'], 1)
        self.assertEqual(coverage['saved_search_inactive_location'], 1)
        self.assertEqual(coverage['saved_search_without_location'], 1)

    def test_delivery_sources_are_mutually_exclusive_and_ordered_by_priority(self):
        order_location = self.make_location('订单地点')
        appointment_location = self.make_location('安排地点')
        item_location = self.make_location('商品地点')
        no_location = self.make_location('商品兜底地点')

        order = self.make_order(
            item=self.make_item(location=order_location), location=order_location,
        )
        self.make_appointment(order, appointment_location)

        appointment_only = self.make_order(item=self.make_item(location=item_location))
        self.make_appointment(appointment_only, appointment_location)

        item_only = self.make_order(item=self.make_item(location=no_location))
        self.make_order(item=self.make_item(title='没有地点的商品'))

        rows = build_location_registry_quality(now=self.now)['delivery_rows']

        self.assertEqual([row['count'] for row in rows], [1, 1, 1, 1])
        self.assertEqual(
            [row['label'] for row in rows],
            ['订单地点', '交付安排地点', '商品地点', '无可解释地点'],
        )

    def test_duplicate_names_fold_unicode_case_and_whitespace(self):
        first_name = '  North Library '
        second_name = 'north　library'
        first = self.make_location(first_name)
        second = self.make_location(second_name)
        self.make_item(location=first, title='重名商品一')
        self.make_item(location=second, title='重名商品二')

        groups = build_location_registry_quality(now=self.now)['duplicate_name_groups']

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['location_count'], 2)
        self.assertEqual(
            [row['name'] for row in groups[0]['locations']],
            [first_name, second_name],
        )
        self.assertEqual(sum(
            row['available_item_count'] for row in groups[0]['locations']
        ), 2)

    def test_inactive_location_with_current_references_is_listed(self):
        inactive = self.make_location('停用但仍被引用', is_active=False)
        self.make_item(location=inactive)
        DemandPost.objects.create(
            requester=self.user, title='引用停用地点', description='引用',
            category=self.category, location=inactive,
        )
        SavedSearch.objects.create(
            user=self.user, name='引用停用地点的关注', location=inactive,
        )

        result = build_location_registry_quality(now=self.now)

        self.assertEqual(result['stale_reference_row_total'], 1)
        row = result['stale_reference_rows'][0]
        self.assertEqual(row['available_item_count'], 1)
        self.assertEqual(row['active_demand_count'], 1)
        self.assertEqual(row['active_saved_search_count'], 1)
        self.assertEqual(row['reference_total'], 3)

    def test_display_lists_are_capped_without_changing_totals(self):
        for index in range(LIST_LIMIT + 2):
            location = self.make_location(f'地点档案 {index:02d}')
            self.make_item(location=location, title=f'地点商品 {index:02d}')

        result = build_location_registry_quality(now=self.now)

        self.assertEqual(len(result['location_rows']), LIST_LIMIT)
        self.assertEqual(result['location_row_total'], LIST_LIMIT + 2)
        self.assertEqual(result['location_row_hidden_total'], 2)

    def test_builder_is_read_only(self):
        location = self.make_location(
            '只读地点', building='原文楼栋', is_active=True,
        )
        item = self.make_item(location=location)
        demand = DemandPost.objects.create(
            requester=self.user, title='只读求购', description='只读',
            category=self.category, location=location,
        )
        saved = SavedSearch.objects.create(
            user=self.user, name='只读关注', location=location,
        )
        order = self.make_order(item=item, location=location)
        appointment = self.make_appointment(order, location)

        build_location_registry_quality(now=self.now)

        location.refresh_from_db()
        item.refresh_from_db()
        demand.refresh_from_db()
        saved.refresh_from_db()
        order.refresh_from_db()
        appointment.refresh_from_db()
        self.assertEqual(location.building, '原文楼栋')
        self.assertEqual(item.title, '地点档案质量商品')
        self.assertEqual(demand.title, '只读求购')
        self.assertEqual(saved.name, '只读关注')
        self.assertEqual(order.status, 'pending')
        self.assertEqual(appointment.status, 'pending')

    def test_invalid_period_falls_back_to_thirty_days(self):
        self.assertEqual(
            build_location_registry_quality(days=13, now=self.now)['period_days'], 30,
        )


class LocationRegistryQualityDashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='location-quality-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_operations_dashboard_renders_the_panel(self):
        location = CampusLocation.objects.create(
            name='档案质量地点', building='东区', address='东门内侧', safety_note='夜间照明到 22:00',
        )

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园地点档案质量')
        self.assertContains(response, '档案质量地点')
        self.assertContains(response, '停用是选择器可见性')
        self.assertContains(response, '只读：不改地点开关')

    def test_operations_dashboard_export_contains_the_same_sections(self):
        CampusLocation.objects.create(name='档案质量导出地点')

        response = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('校园地点档案质量', text)
        self.assertIn('档案质量导出地点', text)
        self.assertIn('地点字段覆盖', text)
        self.assertIn('周期交付地点来源', text)
        self.assertIn('只报告，不自动合并、不改外键', text)

    def test_non_staff_users_cannot_read_the_dashboard_or_export(self):
        User.objects.create_user(
            username='location-quality-reader', password='safe-password-123',
        )
        self.client.force_login(
            User.objects.get(username='location-quality-reader'),
        )

        self.assertEqual(self.client.get(reverse('operations_dashboard')).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('operations_dashboard_export')).status_code, 403,
        )
