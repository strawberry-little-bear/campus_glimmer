# -*- coding: utf-8 -*-
from django.test import SimpleTestCase

from .dashboard_build_performance import build_dashboard_build_performance


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