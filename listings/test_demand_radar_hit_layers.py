# -*- coding: utf-8 -*-
"""这些用例守住四件事：任务按自己的分类
与地点外键归层、分层命中率加起来等于整体
命中率、薄分层上的差值不算结论、只看
报告不改机会分。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .demand_radar_hit_layers import (
    LAYER_GAP_POINTS,
    UNASSIGNED_LABEL,
    build_demand_radar_hit_layers,
)
from .models import Category, CampusLocation, DemandOpportunityTask, SearchQuery


class DemandRadarHitLayersTests(TestCase):
    """分层要能回答“命中率的变化落在哪
    里”，而不是把一个全局平均数再录一
    遍。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='layers-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='数码设备')
        self.category_b = Category.objects.create(name='图书教资')
        self.location = CampusLocation.objects.create(name='图书馆东门')
        self.location_b = CampusLocation.objects.create(name='风雨跳蚤市场')

    def _make_task(self, term, created_at, **kwargs):
        """created_at 是 auto_now_add，必须先落库再 update 写回时间。

        否则所有任务都落在“现在”，分层
        和整体就分不开了。
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

    def _search(self, query, result_count, created_at, **kwargs):
        record = SearchQuery.objects.create(
            user=self.user, query=query, category=self.category,
            location=self.location, result_count=result_count, **kwargs,
        )
        SearchQuery.objects.filter(pk=record.pk).update(created_at=created_at)
        return record

    def _fill_window(self, term, *, start, days, zero_ratio):
        """在以 start 起算、长度为 days 天的窗口里铺搜索记录。

        zero_ratio 控制其中无结果搜索的比例，用来
        构造不同的命中率。窗口的第一天
        和最后一天都写满，保证刚好落在需
        求闭环模块的基线期与观察期内。
        """
        total = 6
        zero_count = int(total * zero_ratio)
        for index in range(total):
            is_zero = index < zero_count
            self._search(
                term, 0 if is_zero else 3,
                start + timedelta(days=index * (days - 1) / (total - 1)),
            )

    def _make_scored_task(self, term, created_at, *, zero_ratio, **kwargs):
        """造一个证据充分、可被判定的任务。

        基线期固定全部无结果（代表缺口
        存在），观察期用 zero_ratio 控制效果：
        0.0 表示缺口收敛，1.0 表示缺口扩大。
        """
        task = self._make_task(term, created_at, **kwargs)
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

    def test_layers_follow_the_task_own_foreign_keys(self):
        """分层必须按任务自己的外键归层，
        不按文本推断。同一个词写在两个
        分类下，就是两个分层。"""
        now = timezone.now()
        # radar_key 唯一，所以同词任务的 key 必须区分开；
        # 分层取决于外键而不是 key，这就是本用例
        # 要坐实的事。
        self._make_scored_task(
            '共用词', now - timedelta(days=20), zero_ratio=0.0,
            radar_key='共用词|category:1|location:0', category=self.category,
        )
        self._make_scored_task(
            '共用词', now - timedelta(days=22), zero_ratio=0.0,
            radar_key='共用词|category:2|location:0', category=self.category_b,
        )

        report = build_demand_radar_hit_layers(days=30, now=now)

        labels = [layer['label'] for layer in report['layers']['category']]
        self.assertIn('数码设备', labels)
        self.assertIn('图书教资', labels)
        self.assertEqual(len(labels), 2)

    def test_unassigned_tasks_are_kept_so_layers_add_up(self):
        """没有分类的任务要落在未分织层，
        不能丢。丢了分层就加不回整体。"""
        now = timezone.now()
        for index in range(3):
            self._make_scored_task(
                f'无分类主题{index}', now - timedelta(days=20 + index * 2),
                zero_ratio=0.0, category=None,
            )
        for index in range(3):
            self._make_scored_task(
                f'有分类主题{index}', now - timedelta(days=24 + index * 2),
                zero_ratio=0.0, category=self.category,
            )

        report = build_demand_radar_hit_layers(days=30, now=now)

        unassigned = [
            layer for layer in report['layers']['category']
            if layer['label'] == UNASSIGNED_LABEL
        ]
        self.assertEqual(len(unassigned), 1)
        self.assertEqual(unassigned[0]['judged_count'], 3)
        layer_total = sum(
            layer['judged_count'] for layer in report['layers']['category']
        )
        self.assertEqual(layer_total, report['overall']['judged_count'])

    def test_a_thin_layer_gets_counts_but_no_direction(self):
        """分层内任务数不足时只给数量，
        不给方向，也不因此放开门槛。"""
        now = timezone.now()
        # 一个分层只有两个任务，低于 MIN_JUDGED_TASKS
        self._make_scored_task(
            '薄层主题一', now - timedelta(days=20), zero_ratio=1.0,
            category=self.category_b,
        )
        self._make_scored_task(
            '薄层主题二', now - timedelta(days=22), zero_ratio=1.0,
            category=self.category_b,
        )

        report = build_demand_radar_hit_layers(days=30, now=now)

        layer = next(
            layer for layer in report['layers']['category']
            if layer['label'] == '图书教资'
        )
        self.assertEqual(layer['judged_count'], 2)
        self.assertFalse(layer['has_sample'])
        self.assertIsNotNone(layer['hit_rate'])
        self.assertEqual(layer['hit_rate'], 0.0)
        self.assertFalse(layer['is_flagged'])
        self.assertEqual(report['min_judged_tasks'], 3)

    def test_a_gap_at_the_threshold_is_flagged_and_below_it_is_not(self):
        """超过门槛的差值才标记，差一点就
        不标记。两者都要求该层先有足够证
        据。"""
        now = timezone.now()
        # 两个分类各 3 个任务，整体命中率 33.3。
        # 数码设备 100（高 66.7 点）、图书教资 0
        # （低 33.3 点），两个都超过 15 点门槛，
        # 且两个分层都有 3 个有证据任务，足够被标记。
        for index in range(3):
            self._make_scored_task(
                f'优势主题{index}', now - timedelta(days=20 + index * 2),
                zero_ratio=0.0, category=self.category,
            )
        for index in range(3):
            self._make_scored_task(
                f'落后主题{index}', now - timedelta(days=26 + index * 2),
                zero_ratio=1.0, category=self.category_b,
            )

        report = build_demand_radar_hit_layers(days=30, now=now)

        overall = report['overall']
        self.assertEqual(overall['judged_count'], 6)
        self.assertEqual(overall['converged_count'], 3)
        self.assertAlmostEqual(overall['hit_rate'], 50.0, places=1)
        layers = {
            layer['label']: layer for layer in report['layers']['category']
        }
        self.assertTrue(layers['数码设备']['is_flagged'])
        self.assertTrue(layers['图书教资']['is_flagged'])
        self.assertEqual(layers['数码设备']['gap_points'], 50.0)
        self.assertEqual(layers['图书教资']['gap_points'], -50.0)
        self.assertTrue(all(layer['has_sample'] for layer in layers.values()))

    def test_a_gap_below_the_threshold_is_not_flagged(self):
        """差值少于门槛就不标记：15 点差一点，
        就是一个任务改了主意的边际，不该
        把运营叫过去。
        """
        now = timezone.now()
        # 数码设备 4 个全收敛（100），图书教资 4 个里
        # 2 个收敛（66.7）。整体 85.7，图书教资差
        # -19 点（超门槛），数码设备差 +14.3 点（不超）。
        # 同一组数据上，一个层该标记、一个不该，
        # 这才能坐实门槛是 15 而不是 10 或 20。
        for index in range(4):
            self._make_scored_task(
                f'优势主题{index}', now - timedelta(days=20 + index * 2),
                zero_ratio=0.0, category=self.category,
            )
        for index, ratio in enumerate((0.0, 0.0, 1.0, 1.0)):
            self._make_scored_task(
                f'接近主题{index}', now - timedelta(days=26 + index * 2),
                zero_ratio=ratio, category=self.category_b,
            )

        report = build_demand_radar_hit_layers(days=30, now=now)

        layers = {
            layer['label']: layer for layer in report['layers']['category']
        }
        self.assertAlmostEqual(report['overall']['hit_rate'], 85.7, places=1)
        self.assertFalse(layers['数码设备']['is_flagged'])
        self.assertEqual(layers['数码设备']['gap_points'], 14.3)
        self.assertTrue(layers['图书教资']['is_flagged'])
        self.assertEqual(layers['图书教资']['gap_points'], -19.0)

    def test_a_thin_layer_with_a_big_gap_is_not_flagged(self):
        """薄层上的大差值不算结论：两个任
        务差 50 点，只是其中一个改了主意。"""
        now = timezone.now()
        for index in range(3):
            self._make_scored_task(
                f'原则主题{index}', now - timedelta(days=20 + index * 2),
                zero_ratio=0.0, category=self.category,
            )
        self._make_scored_task(
            '薄层主题一', now - timedelta(days=26), zero_ratio=1.0,
            category=self.category_b,
        )
        self._make_scored_task(
            '薄层主题二', now - timedelta(days=28), zero_ratio=1.0,
            category=self.category_b,
        )

        report = build_demand_radar_hit_layers(days=30, now=now)

        thin = [
            layer for layer in report['layers']['category']
            if layer['label'] == '图书教资'
        ]
        self.assertEqual(len(thin), 1)
        self.assertFalse(thin[0]['has_sample'])
        self.assertFalse(thin[0]['is_flagged'])
        # 整体命中率 60，薄层 0，差 60 点，依然不报
        self.assertEqual(thin[0]['hit_rate'], 0.0)
        self.assertEqual(thin[0]['gap_points'], -60.0)

    def test_the_module_never_feeds_the_layers_back_into_the_score(self):
        """分层命中率绝不回流到机会分，
        否则雷达会学会放弃报不出来的缺口。"""
        now = timezone.now()
        task = self._make_scored_task(
            '主题一', now - timedelta(days=20), zero_ratio=1.0,
        )

        build_demand_radar_hit_layers(days=30, now=now)

        task.refresh_from_db()
        self.assertEqual(task.opportunity_score, 0)
        self.assertEqual(task.level, 'critical')
        self.assertEqual(task.status, 'todo')

    def test_the_empty_history_reports_rather_than_raising(self):
        """没有任务时给出说明，不是抛异常。"""
        now = timezone.now()
        report = build_demand_radar_hit_layers(days=30, now=now)

        self.assertFalse(report['has_data'])
        self.assertFalse(report['has_sample'])
        self.assertEqual(report['layer_counts']['category'], 0)
        self.assertEqual(report['flagged_count'], 0)
        self.assertIn('还没有创建跟进任务', report['summary'])

    def test_the_summary_names_what_the_breakdown_shows(self):
        """结语要说清分层差值只说明走向，
        且不排序、不改机会分。"""
        now = timezone.now()
        for index in range(3):
            self._make_scored_task(
                f'主题{index}', now - timedelta(days=20 + index * 2),
                zero_ratio=0.0, category=self.category,
            )
        for index in range(2):
            self._make_scored_task(
                f'落后主题{index}', now - timedelta(days=26 + index * 2),
                zero_ratio=1.0, category=self.category_b,
            )

        report = build_demand_radar_hit_layers(days=30, now=now)

        self.assertGreaterEqual(report['flagged_count'], 1)
        self.assertIn('这里也不排序', report['summary'])
        self.assertIn('任务数少的分层', report['summary'])

    def test_dashboard_renders_the_hit_layers_panel(self):
        """面板要出现在看板上，否则运营
        看不到命中率落在哪里。"""
        staff = User.objects.create_superuser(
            username='layers-staff', email='layers@example.com',
            password='safe-password-123',
        )
        now = timezone.now()
        for index in range(3):
            self._make_scored_task(
                f'面板主题{index}', now - timedelta(days=20 + index * 2),
                zero_ratio=0.0,
            )
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '需求雷达闭环的分类与地点分层')

    def test_hit_layers_rows_are_exported_for_weekly_reporting(self):
        """分层要能导出，运营周报才有据可依。"""
        staff = User.objects.create_superuser(
            username='layers-export-staff', email='layers-export@example.com',
            password='safe-password-123',
        )
        now = timezone.now()
        for index in range(3):
            self._make_scored_task(
                f'导出主题{index}', now - timedelta(days=20 + index * 2),
                zero_ratio=0.0, category=self.category_b,
            )
        self.client.force_login(staff)

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '需求雷达闭环的分类与地点分层')
        self.assertContains(export, '图书教资')
