# -*- coding: utf-8 -*-
"""这些用例守住四件事：任务按自己的创建日归入学
期阶段、同一阶段跨学期相比、薄阶段上的差值不算
结论、只看报告不改机会分。

学期纵向对比的危险在于它看起来和跨周期对比一样，
其实不是。跨周期对比比的是相邻两段等长的时间，
学期纵向对比比的是两个不同学期里的同一个阶段。
前者问「这个月是不是比上个月糟」，后者问「这一届
的考试周是不是比上一届的考试周糟」。用错一个，就
会把季节性当成雷达在漂移。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .academic_calendar import ensure_default_phases
from .demand_radar_phase_trend import (
    DEFAULT_LOOKBACK_DAYS,
    NO_TERM_KEY,
    PHASE_GAP_POINTS,
    UNASSIGNED_PHASE_KEY,
    build_demand_radar_phase_trend,
)
from .models import AcademicTerm, Category, CampusLocation, DemandOpportunityTask, SearchQuery


class DemandRadarPhaseTrendTests(TestCase):
    """同一阶段要在学期之间比，而不是在相邻月份之间比。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='phase-trend-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='数码设备')
        self.location = CampusLocation.objects.create(name='图书馆东门')
        self.today = timezone.localdate()

    def _make_term(self, *, starts_on, name):
        term = AcademicTerm.objects.create(
            name=name,
            slug=name,
            kind='autumn',
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=140),
        )
        ensure_default_phases(term)
        return term

    def _make_task(self, key, created_at, **kwargs):
        """created_at 是 auto_now_add，必须先落库再 update 写回时间。

        否则所有任务都落在"现在"，两个学期阶段就分不开了。
        """
        defaults = {
            'radar_key': f'{key}|category:0|location:0',
            'title': key,
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

        基线期固定全部无结果，观察期用 zero_ratio 控制效果：
        0.0 表示缺口收敛，1.0 表示缺口没动。
        """
        total = 6
        zero_count = int(total * zero_ratio)
        for index in range(total):
            self._search(
                term, 0 if index < zero_count else 3,
                start + timedelta(days=index * (days - 1) / (total - 1)),
            )

    def _make_scored_task(self, key, created_at, *, zero_ratio, **kwargs):
        """造一个证据充分、可被判定的任务。"""
        task = self._make_task(key, created_at, **kwargs)
        window = 14
        self._fill_window(
            key, start=created_at - timedelta(days=window), days=window,
            zero_ratio=1.0,
        )
        self._fill_window(
            key, start=created_at, days=window, zero_ratio=zero_ratio,
        )
        return task

    def _at_day_offset(self, term, offset):
        """学期开始后第 offset 天的本地时间，落在白天的中间。"""
        return timezone.make_aware(
            timezone.datetime.combine(
                term.starts_on + timedelta(days=offset), timezone.datetime.min.time(),
            ) + timedelta(hours=12)
        )

    def report(self, *, days=DEFAULT_LOOKBACK_DAYS, now=None):
        return build_demand_radar_phase_trend(
            days=days, now=now or timezone.now(),
        )


    def test_a_task_is_filed_under_the_phase_it_was_created_in(self):
        """归期按任务自己的创建日解析，注册周和考试周
        不能落进同一个格子。"""
        term = self._make_term(
            starts_on=self.today - timedelta(days=100), name='上学期',
        )
        self._make_scored_task(
            '注册主题', self._at_day_offset(term, 5), zero_ratio=0.0,
        )
        self._make_scored_task(
            '考试主题', self._at_day_offset(term, 100), zero_ratio=0.0,
        )

        report = self.report()

        phases = {cell['phase_key'] for cell in report['cells']}
        self.assertEqual(phases, {'registration', 'exam'})
        # 同一学期里的两个阶段各自成格，且都没有跨学期可比的对手
        self.assertEqual(report['term_count'], 1)
        self.assertEqual(report['flagged_count'], 0)
        self.assertEqual(report['has_data'], True)

    def test_the_same_phase_is_compared_across_terms(self):
        """同一阶段跨学期相比：这一届考试周的命中率要
        和上一届考试周比，而不是和它前三十天比。"""
        old_term = self._make_term(
            starts_on=self.today - timedelta(days=400), name='上学年',
        )
        new_term = self._make_term(
            starts_on=self.today - timedelta(days=110), name='本学年',
        )
        for index in range(4):
            self._make_scored_task(
                f'旧考试{index}', self._at_day_offset(old_term, 100 + index),
                zero_ratio=0.0,
            )
        for index in range(4):
            self._make_scored_task(
                f'新考试{index}', self._at_day_offset(new_term, 98 + index),
                zero_ratio=1.0,
            )

        report = self.report()

        exam = [
            item for item in report['comparisons'] if item['phase_key'] == 'exam'
        ]
        self.assertEqual(len(exam), 1)
        self.assertTrue(exam[0]['has_sample'])
        self.assertTrue(exam[0]['is_flagged'])
        self.assertEqual(exam[0]['gap_points'], -100.0)
        # newest term first：新学年排在旧学年前面
        self.assertEqual(exam[0]['current']['term_name'], '本学年')
        self.assertEqual(exam[0]['previous']['term_name'], '上学年')
        # 差值只说明走向，两边的机会分都没被动过
        self.assertEqual(exam[0]['current']['hit_rate'], 0.0)
        self.assertEqual(exam[0]['previous']['hit_rate'], 100.0)

    def test_a_phase_present_in_only_one_term_gets_no_direction(self):
        """只在一个学期出现过的阶段不给方向：没有同类的
        早期阶段可减，前三十天是另一个季节。"""
        term = self._make_term(
            starts_on=self.today - timedelta(days=100), name='唯一的学期',
        )
        for index in range(4):
            self._make_scored_task(
                f'注册{index}', self._at_day_offset(term, 5 + index),
                zero_ratio=0.0,
            )

        report = self.report()

        registration = [
            item for item in report['comparisons']
            if item['phase_key'] == 'registration'
        ]
        self.assertEqual(len(registration), 1)
        self.assertIsNone(registration[0]['previous'])
        self.assertIsNone(registration[0]['gap_points'])
        self.assertFalse(registration[0]['is_flagged'])
        self.assertEqual(report['flagged_count'], 0)

    def test_a_thin_phase_cell_gets_counts_but_no_direction(self):
        """薄阶段格子只报数字：两周里只建了两个任务，命中
        率一步就是二分之一个台阶。"""
        term = self._make_term(
            starts_on=self.today - timedelta(days=100), name='薄阶段学期',
        )
        old_term = self._make_term(
            starts_on=self.today - timedelta(days=400), name='旧学期',
        )
        for index in range(4):
            self._make_scored_task(
                f'厚{index}', self._at_day_offset(old_term, 100 + index),
                zero_ratio=0.0,
            )
        self._make_scored_task(
            '薄一', self._at_day_offset(term, 98), zero_ratio=1.0,
        )
        self._make_scored_task(
            '薄二', self._at_day_offset(term, 99), zero_ratio=1.0,
        )

        report = self.report()

        exam = next(
            item for item in report['comparisons'] if item['phase_key'] == 'exam'
        )
        # 数字照报，差值也照算，但不给方向
        self.assertEqual(exam['current']['task_count'], 2)
        self.assertEqual(exam['gap_points'], -100.0)
        self.assertFalse(exam['has_sample'])
        self.assertFalse(exam['is_flagged'])
        self.assertEqual(report['flagged_count'], 0)
        # 样本不足的格子被算进等待证据的格子数
        self.assertEqual(report['waiting_cell_count'], 1)

    def test_tasks_the_calendar_cannot_place_are_counted_not_dropped(self):
        """日历放不下的任务要单独计数：没有学日历时发生
        的任务、落在阶段空隙里的任务，都不能被丢掉。"""
        term = self._make_term(
            starts_on=self.today - timedelta(days=200), name='有日历学期',
        )
        # 落在阶段空隙里：默认计划的 graduation 到第 125 天，学期却到第 140 天
        gap_day = term.starts_on + timedelta(days=132)
        self._make_scored_task(
            '空隙任务',
            timezone.make_aware(
                timezone.datetime.combine(gap_day, timezone.datetime.min.time())
                + timedelta(hours=12),
            ),
            zero_ratio=0.0,
        )
        # 早于任何学期：学日历只覆盖最近 140 天
        self._make_scored_task(
            '无学期任务', timezone.now() - timedelta(days=300), zero_ratio=0.0,
        )

        report = self.report()

        keys = {cell['phase_key'] for cell in report['cells']}
        self.assertIn(UNASSIGNED_PHASE_KEY, keys)
        self.assertIn(NO_TERM_KEY, keys)
        # 合成桶只描述覆盖率，不参与跨学期对比
        self.assertNotIn(UNASSIGNED_PHASE_KEY, [c['phase_key'] for c in report['comparisons']])
        self.assertNotIn(NO_TERM_KEY, [c['phase_key'] for c in report['comparisons']])
        self.assertEqual(report['unplaced_cell_count'], 2)
        # 两个任务都还在，没有一个被丢掉
        self.assertEqual(
            sum(cell['task_count'] for cell in report['cells']), 2,
        )
        self.assertIn('已单独计数', report['summary'])

    def test_each_task_is_judged_at_its_own_maturity_moment(self):
        """任务按自己的观察期结束时刻判分，否则旧学期
        的任务会全部变成待定，而它们正是要比较的那批。"""
        term = self._make_term(
            starts_on=self.today - timedelta(days=400), name='旧学年',
        )
        for index in range(4):
            self._make_scored_task(
                f'旧考试{index}', self._at_day_offset(term, 100 + index),
                zero_ratio=0.0,
            )

        report = self.report()

        exam = next(
            item for item in report['comparisons'] if item['phase_key'] == 'exam'
        )
        self.assertEqual(exam['current']['pending_count'], 0)
        self.assertEqual(exam['current']['judged_count'], 4)
        self.assertEqual(exam['current']['hit_rate'], 100.0)
        self.assertTrue(exam['current']['has_sample'])

    def test_the_module_never_feeds_the_comparison_back_into_the_score(self):
        """阶段对比绝不回流到机会分，否则雷达会按自己
        擅长的季节调整权重。"""
        term = self._make_term(
            starts_on=self.today - timedelta(days=100), name='回流学期',
        )
        task = self._make_scored_task(
            '回流主题', self._at_day_offset(term, 100), zero_ratio=1.0,
        )

        self.report()

        task.refresh_from_db()
        self.assertEqual(task.opportunity_score, 0)
        self.assertEqual(task.level, 'critical')
        self.assertEqual(task.status, 'todo')

    def test_the_empty_history_reports_rather_than_raising(self):
        """没有任务时给出说明，不是抛异常。"""
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertFalse(report['has_sample'])
        self.assertEqual(report['term_count'], 0)
        self.assertEqual(report['flagged_count'], 0)
        self.assertEqual(report['cells'], [])
        self.assertIn('没有创建跟进任务', report['summary'])

    def test_the_summary_names_what_the_comparison_shows(self):
        """结语要说清阶段差值只说明走向，且不排序、不改
        机会分。"""
        old_term = self._make_term(
            starts_on=self.today - timedelta(days=400), name='上学年',
        )
        new_term = self._make_term(
            starts_on=self.today - timedelta(days=110), name='本学年',
        )
        for index in range(4):
            self._make_scored_task(
                f'旧考试{index}', self._at_day_offset(old_term, 100 + index),
                zero_ratio=0.0,
            )
        for index in range(4):
            self._make_scored_task(
                f'新考试{index}', self._at_day_offset(new_term, 98 + index),
                zero_ratio=1.0,
            )

        report = self.report()

        self.assertEqual(report['flagged_count'], 1)
        self.assertIn('考试周', report['summary'])
        self.assertIn('这里也不排序', report['summary'])
        self.assertIn('任务数少的阶段', report['summary'])

    def test_the_phase_order_follows_the_calendar(self):
        """阶段顺序按学期进程排，读者不该在两处各学一个
        顺序。"""
        # 学期要足够早，毕业季的任务才落在回看期里而不是未来
        term = self._make_term(
            starts_on=self.today - timedelta(days=200), name='顺序学期',
        )
        for key, offset in (('毕业', 118), ('考试', 104), ('注册', 6)):
            for index in range(4):
                self._make_scored_task(
                    f'{key}{index}', self._at_day_offset(term, offset + index),
                    zero_ratio=0.0,
                )

        report = self.report()

        self.assertEqual(
            [cell['phase_key'] for cell in report['cells']],
            ['registration', 'exam', 'graduation'],
        )
        # 面板按阶段分组，组内 newest term first
        self.assertEqual(
            [group['key'] for group in report['phase_groups']],
            ['registration', 'exam', 'graduation'],
        )

    def test_dashboard_renders_the_phase_trend_panel(self):
        """面板要出现在看板上，否则运营看不到阶段之间
        的走向。"""
        staff = User.objects.create_superuser(
            username='phase-staff', email='phase@example.com',
            password='safe-password-123',
        )
        term = self._make_term(
            starts_on=self.today - timedelta(days=100), name='面板学期',
        )
        for index in range(4):
            self._make_scored_task(
                f'面板主题{index}', self._at_day_offset(term, 100 + index),
                zero_ratio=0.0,
            )
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '需求雷达命中率的学期纵向对比')

    def test_phase_trend_rows_are_exported_for_weekly_reporting(self):
        """阶段对比要能导出，运营周报才有据可依。"""
        staff = User.objects.create_superuser(
            username='phase-export-staff', email='phase-export@example.com',
            password='safe-password-123',
        )
        term = self._make_term(
            starts_on=self.today - timedelta(days=100), name='导出学期',
        )
        for index in range(4):
            self._make_scored_task(
                f'导出主题{index}', self._at_day_offset(term, 100 + index),
                zero_ratio=0.0,
            )
        self.client.force_login(staff)

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '需求雷达命中率的学期纵向对比')
