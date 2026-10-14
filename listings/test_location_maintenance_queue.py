# -*- coding: utf-8 -*-
from datetime import timedelta

from django.contrib.admin import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages.storage.cookie import CookieStorage
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import CampusLocationMaintenanceDecisionAdmin
from .location_maintenance_queue import (
    LIST_LIMIT, build_location_maintenance_queue,
    get_location_maintenance_fingerprint, signal_fingerprint,
)
from .models import (
    CampusLocation, CampusLocationMaintenanceDecision, Category, DemandPost,
    Item, SavedSearch,
)


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


class CampusLocationMaintenanceDecisionTests(TestCase):
    def setUp(self):
        self.now = timezone.now() + timedelta(seconds=1)
        self.user = User.objects.create_user(
            username='location-decision-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='地点决策分类')

    def make_location(self, name, **kwargs):
        options = {
            'building': '东区', 'address': '东门内侧',
            'description': '白天有人经过', 'safety_note': '夜间有照明',
            'is_public': True, 'is_active': True,
        }
        options.update(kwargs)
        return CampusLocation.objects.create(name=name, **options)

    def make_decision(self, location, outcome='accepted', **kwargs):
        options = {
            'signal_fingerprint': get_location_maintenance_fingerprint(
                location.id, now=self.now,
            ),
            'outcome': outcome,
            'note': '两个入口确实不相同。' if outcome == 'accepted' else '已在地点后台补充。',
            'reviewed_by': self.user,
        }
        options.update(kwargs)
        return CampusLocationMaintenanceDecision.objects.create(
            location=location, **options
        )

    def test_fingerprint_is_order_insensitive_and_signal_sensitive(self):
        self.assertEqual(
            signal_fingerprint(['profile_gap', 'duplicate_name']),
            signal_fingerprint(['duplicate_name', 'profile_gap']),
        )
        self.assertNotEqual(
            signal_fingerprint(['profile_gap']),
            signal_fingerprint(['stale_reference', 'profile_gap']),
        )

    def test_decisions_match_current_signals_and_unreviewed_rows_come_first(self):
        reviewed = self.make_location('A 已复核地点', building='')
        unreviewed = self.make_location('B 待核对地点', building='')
        self.make_decision(reviewed, outcome='accepted')

        result = build_location_maintenance_queue(now=self.now)

        self.assertEqual(
            [row['name'] for row in result['location_rows']],
            ['B 待核对地点', 'A 已复核地点'],
        )
        reviewed_row = result['location_rows'][1]
        self.assertEqual(reviewed_row['review_status'], 'reviewed')
        self.assertEqual(reviewed_row['review_status_label'], '已复核')
        self.assertEqual(reviewed_row['review_outcome'], 'accepted')
        self.assertEqual(reviewed_row['review_outcome_label'], '维持现状')
        self.assertIsNotNone(reviewed_row['decision_updated_at'])
        self.assertEqual(result['summary']['unreviewed_total'], 1)
        self.assertEqual(result['summary']['reviewed_total'], 1)
        self.assertEqual(result['summary']['outcome_totals']['accepted'], 1)

    def test_changed_signal_combination_requires_a_new_decision(self):
        location = self.make_location('信号变化地点', building='')
        decision = self.make_decision(location, outcome='accepted')

        location.is_active = False
        location.save()
        Item.objects.create(
            title='信号变化商品', description='用于信号变化测试',
            category=self.category, condition='良好', seller=self.user,
            price='12.00', location=location,
        )

        result = build_location_maintenance_queue(now=self.now)
        row = result['location_rows'][0]

        self.assertEqual(row['reason_keys'], ['stale_reference'])
        self.assertEqual(row['review_status'], 'unreviewed')
        self.assertNotEqual(
            row['signal_fingerprint'], decision.signal_fingerprint,
        )

    def test_review_totals_are_not_affected_by_display_truncation(self):
        for index in range(LIST_LIMIT + 1):
            self.make_location(f'待补充 {index:02d}', building='')
        reviewed = self.make_location('最后已复核', building='')
        self.make_decision(reviewed, outcome='deferred')

        result = build_location_maintenance_queue(now=self.now)

        self.assertEqual(result['location_row_total'], LIST_LIMIT + 2)
        self.assertFalse(
            any(row['name'] == '最后已复核' for row in result['location_rows'])
        )
        self.assertEqual(result['summary']['reviewed_total'], 1)
        self.assertEqual(result['summary']['outcome_totals']['deferred'], 1)

    def test_model_validation_and_unique_constraint(self):
        location = self.make_location('决策模型地点', building='')
        blank_note = CampusLocationMaintenanceDecision(
            location=location, signal_fingerprint='profile_gap',
            outcome='accepted', note=' ', reviewed_by=self.user,
        )
        with self.assertRaises(ValidationError):
            blank_note.full_clean()

        self.make_decision(location, outcome='accepted')
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                CampusLocationMaintenanceDecision.objects.create(
                    location=location, signal_fingerprint='profile_gap',
                    outcome='handled', note='重复决策', reviewed_by=self.user,
                )

    def test_queue_builder_does_not_change_decisions(self):
        location = self.make_location('只读决策地点', building='')
        decision = self.make_decision(location, outcome='accepted')

        build_location_maintenance_queue(now=self.now)

        decision.refresh_from_db()
        self.assertEqual(decision.outcome, 'accepted')
        self.assertEqual(decision.note, '两个入口确实不相同。')
        self.assertEqual(decision.reviewed_by, self.user)


class CampusLocationMaintenanceDecisionAdminTests(TestCase):
    def setUp(self):
        self.now = timezone.now() + timedelta(seconds=1)
        self.staff = User.objects.create_user(
            username='location-decision-staff', password='safe-password-123',
            is_staff=True, is_superuser=True,
        )
        self.factory = RequestFactory()
        self.admin = CampusLocationMaintenanceDecisionAdmin(
            CampusLocationMaintenanceDecision, AdminSite(),
        )

    def request(self):
        request = self.factory.post('/admin/')
        request.user = self.staff
        request._messages = CookieStorage(request)
        return request

    def test_save_model_generates_fingerprint_and_reviewer(self):
        location = CampusLocation.objects.create(
            name='后台决策地点', building='东区', address='东门内侧',
            description='白天有人经过', safety_note='',
        )
        decision = CampusLocationMaintenanceDecision(
            location=location, outcome='accepted', note='该入口无需额外提示。',
        )

        self.admin.save_model(
            self.request(), decision, form=None, change=False,
        )

        decision.refresh_from_db()
        self.assertEqual(decision.signal_fingerprint, 'public_safety_gap')
        self.assertEqual(decision.reviewed_by, self.staff)

    def test_save_model_rejects_location_without_current_signal(self):
        location = CampusLocation.objects.create(
            name='干净后台地点', building='东区', address='东门内侧',
            description='白天有人经过', safety_note='夜间有照明',
        )
        decision = CampusLocationMaintenanceDecision(
            location=location, outcome='accepted', note='没有信号也创建。',
        )

        self.admin.save_model(
            self.request(), decision, form=None, change=False,
        )

        self.assertFalse(
            CampusLocationMaintenanceDecision.objects.filter(
                location=location,
            ).exists()
        )


class LocationMaintenanceQueueDashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='location-queue-staff', password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_operations_dashboard_renders_the_queue_panel(self):
        CampusLocation.objects.create(
            name='维护队列地点', building='东区', address='东门内侧',
            description='白天有人经过', safety_note='',
        )

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '校园地点维护队列')
        self.assertContains(response, '维护队列地点')
        self.assertContains(response, '主优先级互斥；信号允许重叠')
        self.assertContains(response, '不自动合并近似重名')

    def test_operations_dashboard_export_contains_the_same_sections(self):
        CampusLocation.objects.create(
            name='维护队列导出地点', building='东区', address='东门内侧',
            description='白天有人经过', safety_note='',
        )

        response = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('校园地点维护队列', text)
        self.assertIn('维护队列导出地点', text)
        self.assertIn('主优先级,地点数,读法', text)
        self.assertIn('命中信号,地点数,读法', text)
        self.assertIn('主优先级互斥，信号可重叠', text)
