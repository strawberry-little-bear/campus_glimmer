# -*- coding: utf-8 -*-
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .dashboard_build_performance import build_dashboard_build_performance
from .views import (
    OPERATIONS_DASHBOARD_SHARED_BUILDERS, _build_shared_operations_dashboard,
)


class DashboardBuildPerformanceTests(SimpleTestCase):
    def run_report(self, *runs):
        return build_dashboard_build_performance(list(runs))

    def test_empty_runs_keep_percentiles_unclaimed(self):
        report = self.run_report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['metrics']['builder_total'], 0)
        self.assertIsNone(report['metrics']['p50_ms'])
        self.assertIsNone(report['metrics']['p90_ms'])
        self.assertEqual(report['metrics']['slowest_key'], '')
        self.assertIn('没有可读', report['summary'])

    def test_one_run_uses_that_observation_for_both_percentiles(self):
        report = self.run_report({
            'key': 'recommendation_quality',
            'label': '推荐质量',
            'elapsed_ms': 12.5,
        })

        self.assertTrue(report['has_data'])
        self.assertEqual(report['metrics']['p50_ms'], 12.5)
        self.assertEqual(report['metrics']['p90_ms'], 12.5)
        self.assertEqual(report['metrics']['slowest_label'], '推荐质量')
        self.assertEqual(report['rows'][0]['share_percent'], 100.0)
        self.assertEqual(report['rows'][0]['cumulative_percent'], 100.0)
        self.assertTrue(report['rows'][0]['is_slowest'])

    def test_rows_are_sorted_and_use_nearest_rank_percentiles(self):
        report = self.run_report(
            {'key': 'core', 'label': '核心看板', 'elapsed_ms': 10},
            {'key': 'search', 'label': '搜索趋势', 'elapsed_ms': 20},
            {'key': 'notification', 'label': '通知响应', 'elapsed_ms': 30},
            {'key': 'recommendation', 'label': '推荐质量', 'elapsed_ms': 40},
        )

        self.assertEqual(
            [row['key'] for row in report['rows']],
            ['recommendation', 'notification', 'search', 'core'],
        )
        self.assertEqual(report['metrics']['p50_ms'], 20.0)
        self.assertEqual(report['metrics']['p90_ms'], 40.0)
        self.assertEqual([row['share_percent'] for row in report['rows']], [40.0, 30.0, 20.0, 10.0])
        self.assertEqual(
            [row['cumulative_percent'] for row in report['rows']],
            [40.0, 70.0, 90.0, 100.0],
        )

    def test_duplicate_keys_are_surfaced_without_collapsing_rows(self):
        report = self.run_report(
            {'key': 'demand_match_quality', 'label': '求购匹配质量', 'elapsed_ms': 8},
            {'key': 'search_trend', 'label': '搜索趋势', 'elapsed_ms': 3},
            {'key': 'demand_match_quality', 'label': '求购匹配质量', 'elapsed_ms': 5},
        )

        self.assertEqual(report['metrics']['duplicate_key_total'], 1)
        demand_rows = [row for row in report['rows'] if row['key'] == 'demand_match_quality']
        self.assertEqual(len(demand_rows), 2)
        self.assertTrue(all(row['is_duplicate'] for row in demand_rows))
        self.assertFalse(report['rows'][-1]['is_duplicate'])

    def test_zero_timings_do_not_invent_a_share(self):
        report = self.run_report(
            {'key': 'core', 'label': '核心看板', 'elapsed_ms': 0},
            {'key': 'search', 'label': '搜索趋势', 'elapsed_ms': 0},
        )

        self.assertTrue(report['has_data'])
        self.assertEqual(report['metrics']['total_elapsed_ms'], 0.0)
        self.assertEqual(report['metrics']['p50_ms'], 0.0)
        self.assertIsNone(report['rows'][0]['share_percent'])
        self.assertIsNone(report['rows'][0]['cumulative_percent'])

    def test_negative_timing_is_rejected(self):
        with self.assertRaisesMessage(ValueError, '构建耗时不能是负数'):
            self.run_report({'key': 'core', 'label': '核心看板', 'elapsed_ms': -1})

class SharedOperationsDashboardAssemblyTests(SimpleTestCase):
    def test_the_shared_assembly_times_the_core_and_each_panel_once(self):
        calls = []

        def base_builder(*, days):
            calls.append(('core', days))
            return {'period_days': days, 'metrics': {}}

        def first_panel(*, days):
            calls.append(('first', days))
            return {'has_data': True}

        def second_panel(*, days):
            calls.append(('second', days))
            return {'has_data': False}

        report = _build_shared_operations_dashboard(
            days=7,
            base_builder=base_builder,
            panel_builders=(
                ('first_panel', '第一张面板', first_panel),
                ('second_panel', '第二张面板', second_panel),
            ),
        )['dashboard_build_performance']

        self.assertEqual(calls, [('core', 7), ('first', 7), ('second', 7)])
        self.assertEqual(report['metrics']['builder_total'], 3)
        self.assertEqual(report['metrics']['duplicate_key_total'], 0)
        self.assertCountEqual(
            [row['key'] for row in report['rows']],
            ['operations_core', 'first_panel', 'second_panel'],
        )

    def test_the_production_registry_has_unique_shared_keys(self):
        keys = [row[0] for row in OPERATIONS_DASHBOARD_SHARED_BUILDERS]

        self.assertEqual(len(keys), 65)
        self.assertEqual(len(set(keys)), 65)
        self.assertIn('price_response', keys)
        self.assertIn('category_price_reference', keys)
        self.assertIn('search_impression_pairing_quality', keys)
        self.assertIn('search_exposure_cold_start', keys)
        self.assertIn('lost_found_report_quality', keys)
        self.assertIn('contribution_milestone_coverage', keys)
        self.assertIn('contribution_milestone_phase_rhythm', keys)
        self.assertIn('campaign_phase_review', keys)
        self.assertIn('recommendation_impression_quality', keys)
        self.assertIn('opportunity_impression_quality', keys)
        self.assertIn('location_registry_quality', keys)
        self.assertIn('location_maintenance_queue', keys)
        self.assertIn('location_maintenance_decision_review', keys)
        self.assertIn('task_run_ledger_integrity', keys)
        self.assertIn('lifecycle_reminder_term_trend', keys)
        self.assertIn('campus_location_service_rule_quality', keys)


class DashboardBuildPerformanceExportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='build-performance-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)

    def test_the_page_and_export_use_the_same_performance_contract(self):
        page = self.client.get(reverse('operations_dashboard'))
        export = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '看板自身构建耗时')
        self.assertContains(page, '最近秩')
        self.assertContains(page, '页面先显示最慢的 12 个构建器')
        text = export.content.decode('utf-8-sig')
        self.assertIn('看板自身构建耗时', text)
        self.assertIn('共享构建器,耗时（ms）,占比（%）,累计占比（%）,读数', text)
        self.assertIn('构建：运营看板核心统计', text)
        self.assertIn('不落库、不做跨请求平均', text)
