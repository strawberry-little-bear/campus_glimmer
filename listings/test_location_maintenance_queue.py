# -*- coding: utf-8 -*-
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .location_maintenance_queue import (
    LIST_LIMIT, build_location_maintenance_queue,
)
from .models import CampusLocation, Category, DemandPost, Item, SavedSearch


class LocationMaintenanceQueueTests(TestCase):
    def setUp(self):
        self.now = timezone.now() + timedelta(seconds=1)
        self.user = User.objects.create_user(
            username='location-queue-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='地点维护队列分类')

    def make_location(self, name, **kwargs):
        options = {
            'building': '东区',
            'address': '东门内侧',
            'description': '白天有人经过',
            'safety_note': '夜间照明到 22:00',
            'is_public': True,
            'is_active': True,
        }
        options.update(kwargs)
        return CampusLocation.objects.create(name=name, **options)

    def make_item(self, location=None, **kwargs):
        options = {
            'title': '地点维护队列商品',
            'description': '用于地点维护队列测试',
            'category': self.category,
            'condition': '良好',
            'seller': self.user,
            'price': '12.00',
        }
        options.update(kwargs)
        if location is not None:
            options['location'] = location
        return Item.objects.create(**options)

    def make_demand(self, location):
        return DemandPost.objects.create(
            requester=self.user, title='地点维护队列求购',
            description='用于地点维护队列测试', category=self.category,
            location=location,
        )

    def make_saved_search(self, location):
        return SavedSearch.objects.create(
            user=self.user, name=f'关注 {location.name}', location=location,
        )

    def test_empty_platform_keeps_a_stable_shape(self):
        result = build_location_maintenance_queue(now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['summary']['location_total'], 0)
        self.assertEqual(result['summary']['queued_total'], 0)
        self.assertEqual(
            [row['key'] for row in result['priority_rows']],
            ['check_first', 'disambiguate', 'enrich'],
        )
        self.assertEqual(
            [row['key'] for row in result['reason_rows']],
            [
                'stale_reference', 'public_safety_gap', 'duplicate_name',
                'profile_gap',
            ],
        )
        self.assertEqual(result['location_rows'], [])

    def test_clean_locations_do_not_enter_the_queue(self):
        self.make_location('启用且完整')
        self.make_location('停用且无当前引用', is_active=False)

        result = build_location_maintenance_queue(now=self.now)

        self.assertTrue(result['has_data'])
        self.assertEqual(result['summary']['queued_total'], 0)
        self.assertEqual(result['summary']['active_clean_total'], 1)
        self.assertEqual(result['summary']['inactive_clean_total'], 1)
        self.assertEqual(result['location_rows'], [])

    def test_inactive_location_with_current_references_is_checked_first(self):
        location = self.make_location('停用仍被引用', is_active=False)
        self.make_item(location=location)
        self.make_demand(location)
        self.make_saved_search(location)

        result = build_location_maintenance_queue(now=self.now)
        row = result['location_rows'][0]

        self.assertEqual(result['summary']['queued_total'], 1)
        self.assertEqual(row['primary_reason'], 'stale_reference')
        self.assertEqual(row['priority'], 'check_first')
        self.assertEqual(row['available_item_count'], 1)
        self.assertEqual(row['active_demand_count'], 1)
        self.assertEqual(row['active_saved_search_count'], 1)
        self.assertEqual(row['current_use_total'], 3)
        self.assertIn('核对旧记录应迁移', row['action_hint'])

    def test_public_safety_gap_is_not_mixed_into_profile_fields(self):
        self.make_location('公共地点缺安全提示', safety_note='')

        row = build_location_maintenance_queue(now=self.now)['location_rows'][0]

        self.assertEqual(row['primary_reason'], 'public_safety_gap')
        self.assertEqual(row['priority'], 'check_first')
        self.assertEqual(row['missing_fields'], [])
        self.assertIn('确认它确实无需提示', row['action_hint'])

    def test_nonpublic_safety_gap_is_a_profile_field(self):
        self.make_location(
            '非公共地点', is_public=False, safety_note='', building='',
        )

        row = build_location_maintenance_queue(now=self.now)['location_rows'][0]

        self.assertEqual(row['primary_reason'], 'profile_gap')
        self.assertEqual(row['priority'], 'enrich')
        self.assertEqual(
            [field['field'] for field in row['missing_fields']],
            ['building', 'safety_note'],
        )

    def test_duplicate_names_keep_both_locations_and_original_names(self):
        first_name = '  North Gate  '
        second_name = 'north　gate'
        self.make_location(first_name)
        self.make_location(second_name)

        result = build_location_maintenance_queue(now=self.now)

        self.assertEqual(result['summary']['signal_totals']['duplicate_name'], 2)
        self.assertEqual(
            [row['name'] for row in result['location_rows']],
            [first_name, second_name],
        )
        self.assertTrue(all(
            row['primary_reason'] == 'duplicate_name' for row in result['location_rows']
        ))
        self.assertTrue(all(
            row['duplicate_group_size'] == 2 for row in result['location_rows']
        ))

    def test_primary_priorities_are_exclusive_while_signals_may_overlap(self):
        stale = self.make_location(
            '停用且公共缺提示', is_active=False, safety_note='',
        )
        self.make_item(location=stale)
        first = self.make_location('South Gate', safety_note='')
        self.make_location('south　gate')

        result = build_location_maintenance_queue(now=self.now)
        summary = result['summary']

        self.assertEqual(summary['queued_total'], 3)
        self.assertEqual(summary['priority_totals']['check_first'], 2)
        self.assertEqual(summary['priority_totals']['disambiguate'], 1)
        self.assertEqual(summary['priority_totals']['enrich'], 0)
        self.assertEqual(summary['signal_totals']['stale_reference'], 1)
        self.assertEqual(summary['signal_totals']['public_safety_gap'], 1)
        self.assertEqual(summary['signal_totals']['duplicate_name'], 2)
        self.assertEqual(sum(summary['priority_totals'].values()), summary['queued_total'])

        overlapping = next(
            row for row in result['location_rows'] if row['name'] == 'South Gate'
        )
        self.assertEqual(
            overlapping['reason_keys'], ['public_safety_gap', 'duplicate_name'],
        )

    def test_rows_are_sorted_by_use_signal_count_and_name(self):
        low = self.make_location('A 少引用', building='')
        high = self.make_location('B 多引用', building='')
        same = self.make_location('C 同样引用', building='')

        self.make_item(location=high)
        self.make_item(location=same)

        rows = build_location_maintenance_queue(now=self.now)['location_rows']

        self.assertEqual(
            [row['name'] for row in rows],
            ['B 多引用', 'C 同样引用', 'A 少引用'],
        )

    def test_display_list_is_capped_without_changing_totals(self):
        for index in range(LIST_LIMIT + 2):
            self.make_location(f'待补充 {index:02d}', building='')

        result = build_location_maintenance_queue(now=self.now)

        self.assertEqual(len(result['location_rows']), LIST_LIMIT)
        self.assertEqual(result['location_row_total'], LIST_LIMIT + 2)
        self.assertEqual(result['location_row_hidden_total'], 2)
        self.assertEqual(result['summary']['queued_total'], LIST_LIMIT + 2)

    def test_current_references_exclude_expired_and_disabled_records(self):
        location = self.make_location('过期引用', is_active=False)
        self.make_item(
            location=location, expires_at=self.now - timedelta(days=1),
        )
        DemandPost.objects.create(
            requester=self.user, title='关闭求购', description='关闭',
            category=self.category, location=location, status='closed',
        )
        SavedSearch.objects.create(
            user=self.user, name='停用关注', location=location, is_active=False,
        )

        result = build_location_maintenance_queue(now=self.now)

        self.assertEqual(result['summary']['queued_total'], 0)
        self.assertEqual(result['summary']['inactive_clean_total'], 1)

    def test_builder_is_read_only(self):
        location = self.make_location('只读队列地点', building='')
        item = self.make_item(location=location)
        demand = self.make_demand(location)
        saved = self.make_saved_search(location)

        build_location_maintenance_queue(now=self.now)

        location.refresh_from_db()
        item.refresh_from_db()
        demand.refresh_from_db()
        saved.refresh_from_db()
        self.assertEqual(location.building, '')
        self.assertEqual(item.title, '地点维护队列商品')
        self.assertEqual(demand.title, '地点维护队列求购')
        self.assertEqual(saved.name, '关注 只读队列地点')

    def test_invalid_period_falls_back_to_thirty_days(self):
        result = build_location_maintenance_queue(days=13, now=self.now)

        self.assertEqual(result['period_days'], 30)
