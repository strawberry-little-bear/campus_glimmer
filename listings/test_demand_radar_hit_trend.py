# -*- coding: utf-8 -*-
"""这些用例守住三件事：任务按创建时间归期、稀疏周期不指方向、只看报告不改权重。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .demand_radar_hit_trend import (
    COMPARED_METRICS,
    MIN_JUDGED_TASKS,
    build_demand_radar_hit_trend,
)
from .search_trend import _period_bounds
from .models import Category, CampusLocation, DemandOpportunityTask, SearchQuery


class DemandRadarHitTrendTests(TestCase):
    """命中率趋势要能回答「雷达是不是变准了」，而不是把日历变化说成雷达变化。"""

    def setUp(self):
        self.user = User.objects.create_user(username='trend-user', password='safe-password-123')
        self.category = Category.objects.create(name='数码设备')
        self.location = CampusLocation.objects.create(name='图书馆东门')

    def _make_task(self, term, created_at, **kwargs):
        """created_at 是 auto_now_add，必须先落库再 update 写回时间。

        否则所有任务都落在"现在"，两个周期窗口就分不开了。
        """
        defaults = {
            'radar_key': f'{term}|category:0|location:0',
            'title': term,
            'category': self.category,
            'location': self.location,
            'level': 'critical',
            'available_supply': 0,
            'created_by': self.user,
        }
        defaults.update(kwargs)
        task = DemandOpportunityTask.objects.create(**defaults)
        DemandOpportunityTask.objects.filter(pk=task.pk).update(created_at=created_at)
        task.refresh_from_db()
        return task

    def _search(self, query, result_count, created_at):
        record = SearchQuery.objects.create(
            user=self.user, query=query, category=self.category,
            location=self.location, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=record.pk).update(created_at=created_at)
        return record

    def _fill_window(self, term, *, start, days, zero_ratio):
        """在以 start 起算、长度为 days 天的窗口里铺搜索记录。

        zero_ratio 控制其中无结果搜索的比例，用来构造不同的命中率。
        窗口的第一天和最后一天都写满，保证刚好落在需求闭环模块的
        基线期（任务创建前 14 天）与观察期（任务创建后 14 天）内。
        """
        total = 6
        zero_count = int(total * zero_ratio)
        for index in range(total):
            is_zero = index < zero_count
            self._search(
                term, 0 if is_zero else 3,
                start + timedelta(days=index * (days - 1) / (total - 1)),
            )

    def _make_scored_task(self, term, created_at, *, zero_ratio):
        """造一个证据充分、可被判定的任务。

        基线期固定全部无结果（代表缺口存在），观察期用 zero_ratio 控制
        效果：0.0 表示缺口收敛，1.0 表示缺口扩大。
        """
        task = self._make_task(term, created_at)
        window = 14
        # 基线期：created_at-14 .. created_at
        self._fill_window(
            term, start=created_at - timedelta(days=window), days=window, zero_ratio=1.0,
        )
        # 观察期：created_at .. created_at+14
        self._fill_window(
            term, start=created_at, days=window, zero_ratio=zero_ratio,
        )
        return task

    def test_tasks_are_counted_in_the_period_they_were_created_in(self):
        """任务必须按创建时间归期，不能按结果落地的周期归期。"""
        now = timezone.now()
        # 上周期任务：创建于 60 天前，观察期在 46 天前就已结束
        self._make_scored_task(
            '上周期主题', now - timedelta(days=40), zero_ratio=0.0,
        )
        # 本周期任务：创建于 10 天前
        self._make_scored_task(
            '本周期主题', now - timedelta(days=20), zero_ratio=1.0,
        )

        report = build_demand_radar_hit_trend(days=30, now=now)

        current_titles = [row['title'] for row in report['current']['rows']]
        previous_titles = [row['title'] for row in report['previous']['rows']]
        self.assertIn('本周期主题', current_titles)
        self.assertNotIn('上周期主题', current_titles)
        self.assertIn('上周期主题', previous_titles)
        self.assertNotIn('本周期主题', previous_titles)

    def test_rising_hit_rate_is_reported_when_the_radar_improves(self):
        """命中率上升要报出来，这是雷达在被调准的信号。"""
        now = timezone.now()
        # 上周期：任务全部缺口扩大（命中率 0）
        self._make_scored_task(
            '旧主题一', now - timedelta(days=40), zero_ratio=1.0,
        )
        self._make_scored_task(
            '旧主题二', now - timedelta(days=45), zero_ratio=1.0,
        )
        self._make_scored_task(
            '旧主题三', now - timedelta(days=50), zero_ratio=1.0,
        )
        # 本周期：任务全部缺口收敛（命中率 100）
        self._make_scored_task(
            '新主题一', now - timedelta(days=20), zero_ratio=0.0,
        )
        self._make_scored_task(
            '新主题二', now - timedelta(days=22), zero_ratio=0.0,
        )
        self._make_scored_task(
            '新主题三', now - timedelta(days=24), zero_ratio=0.0,
        )

        report = build_demand_radar_hit_trend(days=30, now=now)

        self.assertTrue(report['has_sample'])
        self.assertEqual(report['current']['hit_rate'], 100.0)
        self.assertEqual(report['previous']['hit_rate'], 0.0)
        row = next(r for r in report['metric_rows'] if r['key'] == 'hit_rate')
        self.assertEqual(row['direction'], 'rising')
        self.assertEqual(row['delta_display'], '+100.0')
        self.assertIn('闭环命中率', report['summary'])

    def test_falling_hit_rate_is_reported_when_the_radar_gets_worse(self):
        """命中率下降同样要报，不能只报好消息。"""
        now = timezone.now()
        self._make_scored_task(
            '旧主题一', now - timedelta(days=40), zero_ratio=0.0,
        )
        self._make_scored_task(
            '旧主题二', now - timedelta(days=45), zero_ratio=0.0,
        )
        self._make_scored_task(
            '旧主题三', now - timedelta(days=50), zero_ratio=0.0,
        )
        self._make_scored_task(
            '新主题一', now - timedelta(days=20), zero_ratio=1.0,
        )
        self._make_scored_task(
            '新主题二', now - timedelta(days=22), zero_ratio=1.0,
        )
        self._make_scored_task(
            '新主题三', now - timedelta(days=24), zero_ratio=1.0,
        )

        report = build_demand_radar_hit_trend(days=30, now=now)

        row = next(r for r in report['metric_rows'] if r['key'] == 'hit_rate')
        self.assertEqual(row['direction'], 'falling')

    def test_a_period_below_the_task_floor_is_not_compared(self):
        """任务数不够就不给方向，三个任务算出来的变化是噪声。"""
        now = timezone.now()
        # 上周期只有 2 个任务，低于 MIN_JUDGED_TASKS
        self._make_scored_task(
            '旧主题一', now - timedelta(days=40), zero_ratio=0.0,
        )
        self._make_scored_task(
            '旧主题二', now - timedelta(days=45), zero_ratio=1.0,
        )
        # 本周期有 3 个任务，够门槛
        self._make_scored_task(
            '新主题一', now - timedelta(days=20), zero_ratio=0.0,
        )
        self._make_scored_task(
            '新主题二', now - timedelta(days=22), zero_ratio=0.0,
        )
        self._make_scored_task(
            '新主题三', now - timedelta(days=24), zero_ratio=0.0,
        )

        report = build_demand_radar_hit_trend(days=30, now=now)

        self.assertFalse(report['has_sample'])
        row = next(r for r in report['metric_rows'] if r['key'] == 'hit_rate')
        self.assertEqual(row['direction'], 'insufficient')
        self.assertIn('任务数不足', report['summary'])

    def test_insufficient_sample_tasks_stay_out_of_the_denominator(self):
        """样本不足的任务既不算成功也不算失败，要单独数出来。"""
        now = timezone.now()
        # 本周期 3 个有证据的任务 + 1 个只有一次搜索的任务
        for index, ratio in enumerate((0.0, 0.0, 0.0)):
            self._make_scored_task(
                f'新主题{index}', now - timedelta(days=20 + index * 2), zero_ratio=ratio,
            )
        thin = self._make_task('冷门主题', now - timedelta(days=18))
        self._search('冷门主题', 0, now - timedelta(days=20))
        self._search('冷门主题', 0, now - timedelta(days=6))

        report = build_demand_radar_hit_trend(days=30, now=now)

        self.assertEqual(report['current']['task_count'], 4)
        self.assertEqual(report['current']['judged_count'], 3)
        self.assertEqual(report['current']['insufficient_count'], 1)
        self.assertEqual(report['current']['hit_rate'], 100.0)

    def test_flat_change_is_reported_as_flat_not_as_improvement(self):
        """命中率没动就是没动，几个百分点不能算成绩。"""
        now = timezone.now()
        for index, offset in enumerate((40, 45, 50)):
            self._make_scored_task(
                f'旧主题{index}', now - timedelta(days=offset), zero_ratio=0.0,
            )
        for index, offset in enumerate((10, 12, 14)):
            self._make_scored_task(
                f'新主题{index}', now - timedelta(days=offset), zero_ratio=0.0,
            )

        report = build_demand_radar_hit_trend(days=30, now=now)

        row = next(r for r in report['metric_rows'] if r['key'] == 'hit_rate')
        self.assertEqual(row['direction'], 'flat')
        self.assertEqual(row['delta_display'], '+0.0')

    def test_the_compared_metrics_cover_rate_and_magnitude(self):
        """对比项要同时覆盖命中率和收敛幅度，两个才能说清雷达好不好。"""
        now = timezone.now()
        report = build_demand_radar_hit_trend(days=30, now=now)

        keys = [row['key'] for row in report['metric_rows']]
        self.assertEqual(keys, [key for key, _ in COMPARED_METRICS])
        self.assertIn('hit_rate', keys)
        self.assertIn('median_delta_points', keys)
        rate_rows = [row for row in report['metric_rows'] if row['is_rate_metric']]
        self.assertEqual(
            [row['key'] for row in rate_rows],
            ['hit_rate', 'median_delta_points'],
        )

    def test_direction_vocabulary_matches_the_other_trend_panels(self):
        """方向词必须和搜索趋势等面板一致，否则读者要学两套语言。"""
        # 空报告里每个指标都落在样本不足，所以这里直接检查标签表本身。
        from .demand_radar_hit_trend import TREND_DIRECTIONS, TREND_LABELS
        self.assertEqual(
            TREND_DIRECTIONS,
            ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient'),
        )
        self.assertEqual(TREND_LABELS['insufficient'], '样本不足')
        self.assertEqual(TREND_LABELS['new'], '本期新出现')
        self.assertEqual(TREND_LABELS['gone'], '本期已消失')

        now = timezone.now()
        report = build_demand_radar_hit_trend(days=30, now=now)
        labels = {row['direction']: row['direction_label'] for row in report['metric_rows']}
        self.assertEqual(labels['insufficient'], '样本不足')

    def test_new_and_gone_are_used_when_one_period_has_no_tasks(self):
        """一边有任务、一边没有时，要给"本期新出现"而不是凭空算差值。"""
        now = timezone.now()
        # 只有本周期有任务，上一周期为空
        for index, offset in enumerate((10, 12, 14)):
            self._make_scored_task(
                f'新主题{index}', now - timedelta(days=offset), zero_ratio=0.0,
            )

        report = build_demand_radar_hit_trend(days=30, now=now)

        self.assertFalse(report['has_sample'])
        row = next(r for r in report['metric_rows'] if r['key'] == 'hit_rate')
        self.assertEqual(row['direction'], 'new')
        self.assertEqual(row['previous'], None)
        self.assertIsNone(row['delta'])

    def test_the_module_never_feeds_the_hit_rate_back_into_the_score(self):
        """命中率绝不回流到机会分，否则雷达会学会掩盖自己的误报。"""
        now = timezone.now()
        self._make_scored_task(
            '主题一', now - timedelta(days=20), zero_ratio=1.0,
        )

        build_demand_radar_hit_trend(days=30, now=now)

        # 机会分是创建任务时写定的快照，本模块只读不写
        task = DemandOpportunityTask.objects.get(title='主题一')
        self.assertEqual(task.opportunity_score, 0)

    def test_the_empty_history_reports_rather_than_raising(self):
        """没有任务时给出说明，不是抛异常。"""
        now = timezone.now()
        report = build_demand_radar_hit_trend(days=30, now=now)

        self.assertFalse(report['has_data'])
        self.assertFalse(report['has_sample'])
        self.assertIsNone(report['current']['hit_rate'])
        self.assertIsNone(report['previous']['hit_rate'])
        self.assertIn('没有创建跟进任务', report['summary'])

    def test_the_two_windows_are_bounded_by_local_midnight(self):
        """两个窗口按本地午夜切分，和搜索趋势面板同一把尺子。"""
        now = timezone.now()
        report = build_demand_radar_hit_trend(days=30, now=now)

        self.assertEqual(
            report['current_period_start'],
            report['previous_period_start'] + timedelta(days=30),
        )
        self.assertEqual(report['current_period_end'], now)

    def test_a_task_at_the_edge_of_the_window_is_not_dropped(self):
        """刚跨进本周期的任务不能被上一周期抢走，也不能被漏掉。"""
        now = timezone.now()
        start, _ = _period_bounds(now, 30)
        self._make_scored_task('边界任务', start + timedelta(hours=1), zero_ratio=0.0)

        report = build_demand_radar_hit_trend(days=30, now=now)

        titles = [row['title'] for row in report['current']['rows']]
        self.assertIn('边界任务', titles)
        previous_titles = [row['title'] for row in report['previous']['rows']]
        self.assertNotIn('边界任务', previous_titles)

    def test_dashboard_renders_the_hit_trend_panel(self):
        """面板要出现在看板上，否则运营看不到命中率趋势。"""
        staff = User.objects.create_superuser(
            username='trend-staff', email='trend@example.com', password='safe-password-123',
        )
        now = timezone.now()
        for index, offset in enumerate((40, 45, 50)):
            self._make_scored_task(
                f'旧主题{index}', now - timedelta(days=offset), zero_ratio=0.0,
            )
        for index, offset in enumerate((10, 12, 14)):
            self._make_scored_task(
                f'新主题{index}', now - timedelta(days=offset), zero_ratio=0.0,
            )
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '需求雷达命中率的跨周期趋势')
        self.assertContains(response, '闭环命中率')

    def test_hit_trend_rows_are_exported_for_weekly_reporting(self):
        """趋势要能导出，运营周报才有据可依。"""
        staff = User.objects.create_superuser(
            username='trend-export-staff', email='trend-export@example.com',
            password='safe-password-123',
        )
        now = timezone.now()
        self._make_scored_task('导出主题', now - timedelta(days=20), zero_ratio=0.0)
        self.client.force_login(staff)

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '需求雷达命中率的跨周期趋势')
        self.assertContains(export, '闭环命中率')
