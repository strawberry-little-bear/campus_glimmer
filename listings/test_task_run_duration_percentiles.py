# -*- coding: utf-8 -*-
"""守住命令耗时分位的边界：一条慢命令要能被看见，而样本不够时不许装睡。

中位数回答"典型的一次有多久"，但回答不了"最慢的那一档有多慢"：一个命令
五十次四秒、一次四十秒，中位数在每个窗口里都是四秒，纹丝不动，而调度窗口
正是在被那一档悄悄吃掉。这一组用例钉住的就是这件事。

三件比其余更要紧的事。分位数只在该有样本时才声称——十次运行里最近秩的
P99 就是最大值，给它贴上 P99 的标签是让读者以为尾巴被量过，其实只看见了
一次运行。失败运行不进分位——失败可能毫秒级退出，也可能挂到被调度器杀掉，
把它算进去会让同一个数字同时描述"多常坏"和"好的时候多慢"两件不相干的事。
分位数取真实发生过的值而不是插值出来的值——插值 P90 会落在第九和第十次
之间，那是一次从未发生过的运行，而这个模块存在的理由就是描述真正发生过的
运行。

其余是边界：运行按开始时刻归期，预演照健康面板的口径排除，没有尾巴的行
排在有尾巴的行后面，以及这个模块只读台账、从不写回。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import TaskRun
from .task_run_duration_percentiles import (
    MIN_RUNS_FOR_PERCENTILE,
    PERCENTILES,
    build_task_run_duration_percentiles,
)


class TaskRunDurationPercentileTests(TestCase):
    """Which runs land in the window, and what a percentile may claim."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='percentile-user', password='safe-password-123',
        )
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)

    def make_run(self, name, *, status='succeeded', started_at=None,
                 duration_ms=120, note=''):
        started = started_at or (self.now - timedelta(hours=1))
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=started,
            finished_at=started,
            duration_ms=duration_ms,
            metrics={},
            note=note,
        )

    def report(self, days=30):
        # 当前窗口要坐在这些行之后：入库时间晚于 setUp 取到的 now，
        # 不往后推一段，刚写进去的行会落在窗口上界之外。
        return build_task_run_duration_percentiles(
            days=days, now=self.now + timedelta(minutes=5),
        )

    def row(self, report, name):
        return next((row for row in report['rows'] if row['name'] == name), None)

    def test_a_percentile_is_a_value_some_run_actually_took(self):
        # 十次运行的最近秩 P90 是第九次：十分之九才是九成，十分之八不是。
        for index in range(10):
            self.make_run('send_lifecycle_reminders', duration_ms=1000 + index * 10)
        row = self.row(self.report(), 'send_lifecycle_reminders')
        self.assertEqual(row['percentiles']['P90'], 1080)
        self.assertEqual(row['percentiles']['P50'], 1040)

    def test_the_ninetieth_is_not_the_maximum_of_a_handful_of_runs(self):
        # 十次以内的运行不声称 P99：最近秩的 P99 就是最大值，
        # 贴上一个 P99 的标签会让读者以为尾巴被量过。
        for index in range(10):
            self.make_run('send_lifecycle_reminders', duration_ms=1000 + index)
        row = self.row(self.report(), 'send_lifecycle_reminders')
        self.assertIsNone(row['percentiles']['P99'])
        self.assertFalse(row['sample_enough']['P99'])
        # 十次支撑得起 P90（第九次），此时 P99 是 None 而 P90 有值：
        # 门槛是按每个分位分别报的，不是给整行一个"样本不足"。
        self.assertIsNotNone(row['percentiles']['P90'])
        self.assertTrue(row['sample_enough']['P90'])

    def test_a_tail_is_reported_as_a_share_of_the_typical_run(self):
        # 同样的尾部，相对 P50 表示成倍数才跨命令可比：
        # "P99 比 P50 多 900 毫秒" 在不知道 P50 是多少时什么都不是。
        for index in range(8):
            self.make_run('expire_items', duration_ms=4000)
        self.make_run('expire_items', duration_ms=40000)
        self.make_run('expire_items', duration_ms=40000)
        row = self.row(self.report(), 'expire_items')
        # 十次运行声称不出 P99，尾部倍数就取已声称的最高分位 P90，
        # 而不是因为 P99 缺席就报告"没有尾巴"。
        self.assertEqual(row['tail_share'], 9.0)

    def test_a_command_without_a_tail_reports_one(self):
        # 每次都一样快的命令，尾部倍数就是 0——说"没有尾巴"比让读者
        # 自己去比三列更有用。
        for index in range(10):
            self.make_run('sync_academic_calendar', duration_ms=2500)
        row = self.row(self.report(), 'sync_academic_calendar')
        self.assertEqual(row['tail_share'], 0.0)

    def test_failed_runs_do_not_enter_the_percentiles(self):
        # 失败可能毫秒级退出，也可能挂到被调度器杀掉。把它算进分位，
        # 同一个数字就会同时描述"多常坏"和"好的时候多慢"。
        for index in range(10):
            self.make_run('send_operations_digest', duration_ms=3000)
        self.make_run('send_operations_digest', status='failed', duration_ms=1)
        row = self.row(self.report(), 'send_operations_digest')
        self.assertEqual(row['percentiles']['P50'], 3000)
        # 失败次数仍然报在旁边，读者才知道分位只描述成功的那些运行。
        self.assertEqual(row['failed_count'], 1)
        self.assertEqual(row['succeeded_count'], 10)

    def test_a_run_belongs_to_the_window_it_started_in(self):
        # 四十天前的那次不算进三十天窗口：归期按开始时刻，
        # 和跨周期趋势面板同一套口径。
        self.make_run('expire_items', started_at=self.now - timedelta(days=40))
        for index in range(10):
            self.make_run('expire_items', duration_ms=2000)
        row = self.row(self.report(), 'expire_items')
        self.assertEqual(row['run_count'], 10)

    def test_a_run_that_started_in_the_future_is_not_counted(self):
        # 上界就是 now：还没开始跑的运行不能进统计。
        self.make_run('expire_items', started_at=self.now + timedelta(hours=2))
        report = self.report()
        self.assertIsNone(self.row(report, 'expire_items'))

    def test_dry_runs_are_excluded_for_the_reason_the_health_module_excludes(self):
        # 预演什么都不发，它的耗时描述的是空转，不是一次真实的运行。
        for index in range(10):
            self.make_run('send_saved_search_digest', duration_ms=500)
        self.make_run('send_saved_search_digest', duration_ms=99999, note='dry-run')
        row = self.row(self.report(), 'send_saved_search_digest')
        self.assertEqual(row['percentiles']['P50'], 500)
        self.assertEqual(row['run_count'], 10)

    def test_a_row_with_a_tail_sorts_before_a_row_without_one(self):
        # 有尾巴的行是值得先读的那些，没尾巴的行排到后面去。
        for index in range(10):
            self.make_run('sync_academic_calendar', duration_ms=2000)
        for index in range(10):
            self.make_run('expire_items', duration_ms=3000)
        self.make_run('expire_items', duration_ms=30000)
        report = self.report()
        names = [row['name'] for row in report['rows']]
        self.assertEqual(names[0], 'expire_items')

    def test_a_command_with_no_claimed_tail_sorts_last(self):
        # 声称不出尾巴的行（样本不够）排在能声称的行之后，
        # 而不是因为某个 None 被排到最前面去。
        for index in range(10):
            self.make_run('sync_academic_calendar', duration_ms=2000)
        self.make_run('expire_items', duration_ms=3000)
        report = self.report()
        names = [row['name'] for row in report['rows']]
        self.assertEqual(names[-1], 'expire_items')

    def test_an_empty_history_reports_rather_than_raising(self):
        report = self.report()
        self.assertFalse(report['has_data'])
        self.assertEqual(report['rows'], [])

    def test_the_sample_floor_is_reported_per_figure(self):
        # 门槛按每个分位分别报，而不是给整行一个数：九次运行
        # 撑得起 P90、撑不起 P99，说成"样本不足"会把两者一起埋掉。
        for index in range(10):
            self.make_run('expire_items', duration_ms=1000)
        row = self.row(self.report(), 'expire_items')
        self.assertTrue(row['sample_enough']['P50'])
        self.assertTrue(row['sample_enough']['P90'])
        self.assertFalse(row['sample_enough']['P99'])

    def test_the_reported_percentiles_are_the_three_the_module_promises(self):
        # 面板按这三个名字取数，多一个少一个都会让模板读到空值。
        for index in range(10):
            self.make_run('expire_items', duration_ms=1000)
        row = self.row(self.report(), 'expire_items')
        self.assertEqual(
            sorted(row['percentiles']), sorted(label for _, label in PERCENTILES),
        )
        self.assertEqual(MIN_RUNS_FOR_PERCENTILE[99], 100)

    def test_dashboard_renders_the_percentile_panel(self):
        for index in range(10):
            self.make_run('expire_items', duration_ms=1000)
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '命令耗时的分位分布')

    def test_the_export_carries_the_percentile_section(self):
        # 导出的口径要和面板一致：分位留空而不是填 0，样本不足要写出来，
        # 否则下载回去的那张表会被读成"这个命令确实这么快"。
        for index in range(10):
            self.make_run('expire_items', duration_ms=1000 + index)
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('命令耗时的分位分布', text)
        self.assertIn('P99', text)
        self.assertIn('最近秩', text)
        # 十次运行的 P99 声称不出来，导出里必须是空的而不是 0。
        lines = text.splitlines()
        row = next(line for line in lines if line.startswith('expire_items,'))
        cells = row.split(',')
        self.assertEqual(cells[6], '')

    def test_the_module_never_writes_back_into_the_ledger_it_reads(self):
        # 只读台账。一个能改写台账的统计会让台账变成记录被它自己抠出来的
        # 形状的地方，后面再读的人就分不清哪些是运行、哪些是统计的痕迹。
        before = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'status', 'duration_ms', 'note')
        )
        for index in range(10):
            self.make_run('expire_items', duration_ms=1000)
        self.report()
        after = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'status', 'duration_ms', 'note')
        )
        self.assertEqual(before, after[:len(before)])
        self.assertEqual(len(after) - len(before), 10)
