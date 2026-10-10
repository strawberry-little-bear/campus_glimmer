# -*- coding: utf-8 -*-
"""守住命令自我重叠的边界：节奏从台账反推，重叠必须真的发生过，而且必须真的有代价。

看板上关于调度的每一个数字都在描述已经发生过的事：哪个命令失败了、哪个命令变慢了、
哪个小时拥塞了。这个模块是唯一往前看的那一个，而它靠的是一个没人配置过的事实——一次
运行花了多久，相对于这个命令被要求多久跑一次。

一次运行比自己的间隔更长时，下一次就开始在上一次还没结束的时候。台账里没有任何一行
会把这标记成异常：每一行照样记成功、照样记耗时，健康面板照样报一个健康的命令。真正发生
的事情是同一命令的两次执行开始抢同一个连接池和同一份外部配额，于是每次都更慢，下一次
开始得更晚，再下一次更慢。这个环就是本项目大多数调度故障背后的机制，而现有面板一个都
看不见它，因为能看见它的那个面板要比较的是「耗时」和「间隔」——两件从不放在一起的东西。

有四条边界比其余更要紧。间隔从台账反推，不读 crontab：本项目没有这个数据源，而且即使
有，crontab 记的是写下来的安排而不是真正跑出来的节奏，一次被繁忙机器拖慢的运行、或重
启后的补跑，都会让实际间隔长于配置。重叠按台账里真实发生过的成对运行数，不按比值推断：
P99 逼近间隔是推测，推测不是两次运行撞在了一起的证据。重叠必须真的有代价才叫问题——
两条命令各自等一个外部的东西，那个东西变慢了，于是重叠越来越多而什么都没有退化，这种
重叠只报数字。以及这个模块只读台账、从不写回，也从不为了记录一次重叠而往台账里写第三
行：重叠是两行已有记录之间的关系，物化它会让一张表里出现一行开始时间不属于任何一次运行
的记录。
"""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import TaskRun
from .task_run_overlap import (
    APPROACHING_INTERVAL,
    MIN_OVERLAP_PAIRS,
    MIN_RUNS_FOR_RHYTHM,
    OVERLAP_SLOWDOWN,
    build_task_run_overlap,
)


class TaskRunOverlapTests(TestCase):
    """The rhythm a command kept, and whether it ran into its own next start."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='overlap-user', password='safe-password-123',
        )
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)

    def moment_plus(self, day_offset, hour, minute, offset_minutes):
        """本地时刻往后推若干分钟，用来排跨小时的等间隔节奏。

        间隔是三十分钟、四十分钟时，index * interval 会越过整点，而
        `time()` 只接受 0..59 的分钟。等间隔是这个模块对夹具的要求，
        所以这里必须能跨小时，而不是让用例迁就分钟的取值上限。
        """
        return self.local_moment(day_offset, hour, minute) + timedelta(
            minutes=offset_minutes,
        )

    def local_moment(self, day_offset, hour, minute=0):
        """一个被钉住的本地时刻：先算本地日期，再取该日的指定小时。

        时区钉死很重要：CI 跑 UTC、项目时区是东八区，如果直接用
        `timezone.now() - timedelta(days=...)`，本地零点前后那几个小时会跨日，
        同一个用例在这里通过、在 UTC 下失败。先把本地日期算出来再组合时刻，
        运行就一定会落在测试写下的那一天里。
        """
        local_date = self.today - timedelta(days=day_offset)
        moment = timezone.make_aware(datetime.combine(local_date, time(hour=hour, minute=minute)))
        # 落在这个周期内的运行必须早于窗口上界：setUp 取到的 now 已经过去了几毫秒，
        # 而模块默认用 `timezone.now()` 作上界，正好把刚写进去的行卡在窗外。
        upper = timezone.make_aware(datetime.combine(local_date, time(hour=23, minute=59)))
        if upper > self.now:
            return self.now - timedelta(minutes=1)
        return moment

    def make_run(self, name, *, status='succeeded', started_at=None,
                 duration_ms=120, note=''):
        started = started_at or self.local_moment(1, 9)
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
        return build_task_run_overlap(days=days, now=self.now + timedelta(minutes=5))

    def row_of(self, report, name):
        return next((row for row in report['rows'] if row['name'] == name), None)

    def run_cadence(self, name, *, count, day_offset, hour=9, duration_ms=120,
                    interval_minutes=10, status='succeeded'):
        """`count` runs of one command, evenly spaced by `interval_minutes`.

        均匀间隔是节奏这一列的读法：间隔取相邻两次开始时刻的中位数，所以用例
        要么真的排成等间隔，要么别对 interval_ms 下断言。
        """
        for index in range(count):
            self.make_run(
                name,
                status=status,
                started_at=self.moment_plus(
                    day_offset, hour, 0, index * interval_minutes,
                ),
                duration_ms=duration_ms,
            )

    # -- the rhythm is read off the ledger ---------------------------------

    def test_the_interval_is_the_median_gap_between_consecutive_starts(self):
        # 间隔是相邻两次开始时刻的中位数，不是平均值：一次漏跑会让某一个间隔翻倍，
        # 平均值会报出一个从未发生过的节奏，而中位数不会。
        self.run_cadence('expire_items', count=6, day_offset=2, interval_minutes=10)

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['interval_ms'], 10 * 60 * 1000)
        self.assertTrue(row['has_rhythm'])

    def test_a_single_missed_run_does_not_move_the_reported_interval(self):
        # 漏跑一次让某一个间隔变成正常的两倍，中位数必须不动：否则面板会把一次
        # 重启报成调度变了。
        for index, minute in enumerate((0, 10, 20)):
            self.make_run(
                'expire_items',
                started_at=self.moment_plus(2, 9, 0, minute),
                duration_ms=120,
            )
        # 中间插一个间隔翻倍的运行，其余仍是十分钟。
        self.make_run(
            'expire_items',
            started_at=self.moment_plus(2, 9, 0, 60),
            duration_ms=120,
        )
        for minute in (70, 80, 90):
            self.make_run(
                'expire_items',
                started_at=self.moment_plus(2, 9, 0, minute),
                duration_ms=120,
            )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        # 六个间隔里五个是十分钟、一个是二十分钟，中位数仍是十分钟。
        self.assertEqual(row['interval_ms'], 10 * 60 * 1000)
        self.assertEqual(row['interval_runs'], 6)

    def test_a_command_with_too_few_runs_has_no_rhythm(self):
        # 两次运行只有一个间隔，够干什么呢？不够报节奏，但次数照样报出来——
        # 少报数字比报一个中位数只有一次观测的节奏诚实。
        self.run_cadence('expire_items', count=2, day_offset=2, interval_minutes=10)

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertFalse(row['has_rhythm'])
        self.assertIsNone(row['interval_ms'])
        self.assertEqual(row['run_count'], 2)

    # -- overlap is counted from the ledger --------------------------------

    def test_a_run_that_outlives_its_interval_counts_as_overlapping(self):
        # 一次运行比自己的间隔更长时，下一次开始在上一次还没结束的时候。
        # 这是本模块存在的理由，必须能数出来。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=10, duration_ms=15 * 60 * 1000,
        )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['overlap_pair_count'], 5)

    def test_a_run_shorter_than_its_interval_never_overlaps(self):
        # 耗时远短于间隔时，下一次开始前上一次早就结束了。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=30, duration_ms=120,
        )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['overlap_pair_count'], 0)
        self.assertFalse(row['is_self_overlapping'])

    def test_a_failed_run_still_counts_as_an_overlap(self):
        # 失败的运行也占着连接池。只数成功的运行会把「失败是因为上一次还没跑完」
        # 这一类证据整个丢掉，而那正是这个模块要抓的东西。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=10, duration_ms=15 * 60 * 1000,
            status='failed',
        )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['overlap_pair_count'], 5)

    def test_a_run_with_no_duration_cannot_have_overlapped(self):
        # 没有耗时的运行记不出结束时刻。零耗时会算成「瞬间结束」，那种运行撞不到
        # 任何东西，把它算成重叠是凭空造出来的对。
        for index in range(6):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=0,
            )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['overlap_pair_count'], 0)

    # -- overlap has to cost something before it is called a problem -------

    def test_an_overlap_that_costs_nothing_is_reported_but_not_flagged(self):
        # 一条命令整个间隔都在等外部的东西，那个东西变慢了，于是重叠越来越多，
        # 而什么都没有退化。这种重叠只报数字，不叫问题。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=10, duration_ms=20 * 60 * 1000,
        )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['overlap_pair_count'], 5)
        self.assertFalse(row['is_self_overlapping'])
        # 整条命令都叠在一起时没有「其余运行」可比，代价给不出数字——
        # 这正是不叫它问题的原因：没有任何证据说重叠让事情变慢了。

    def test_an_overlap_whose_runs_are_slower_is_flagged(self):
        # 重叠真的让运行变慢时才叫自我重叠。前三分钟一次（不重叠，120 毫秒），
        # 之后叠在一起而且一次 900 毫秒——代价是 7.5 倍。
        for index in range(3):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=120,
            )
        for index in range(3, 6):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=15 * 60 * 1000,
            )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertTrue(row['is_self_overlapping'])
        # 干净的运行 120 毫秒，撞在一起的运行一次十五分钟。
        self.assertEqual(row['overlap_cost'], 7500.0)
        self.assertEqual(report['overlapping_count'], 1)

    def test_an_overlap_below_the_pair_floor_is_not_a_pattern(self):
        # 一对撞在一起的运行是意外，不是形态。低于门槛只报数字。
        for index in range(2):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=15 * 60 * 1000,
            )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['overlap_pair_count'], 1)
        self.assertFalse(row['is_self_overlapping'])

    # -- the tail against the interval ------------------------------------

    def test_a_tail_close_to_the_interval_is_reported_as_approaching(self):
        # 尾部耗时逼近间隔时要说话：还没撞上，但已经没给下一次留地方了。
        # 这是这个面板唯一能提前说话的一次机会。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=10, duration_ms=100,
        )
        # 一次真正慢的运行，九分钟，还没超过十分钟的间隔。
        self.make_run(
            'expire_items',
            started_at=self.moment_plus(2, 9, 0, 55),
            duration_ms=9 * 60 * 1000,
        )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        # 最长的一次是九分钟、间隔是十分钟，比值 0.9：还没撞上，但下一次
        # 只剩一分钟。这个面板唯一能提前说话的机会就在这里。
        self.assertTrue(row['is_approaching'])
        self.assertEqual(row['longest_run_ms'], 9 * 60 * 1000)
        self.assertEqual(row['longest_run_ratio'], 0.9)

    def test_a_tail_far_below_the_interval_is_not_approaching(self):
        # 尾部远短于间隔时不说话。一个健康的调度不该被报成正在靠近自己。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=30, duration_ms=200,
        )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertFalse(row['is_approaching'])
        self.assertEqual(row['longest_run_ratio'], 0.0)

    # -- sorting and the sentence -----------------------------------------

    def test_the_rows_that_collide_sort_first(self):
        # 会撞的排前面：要读的就是那几行。其余按重叠对数排，名字只用来打破平局，
        # 这样两次刷新之间顺序不会换。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=10, duration_ms=15 * 60 * 1000,
        )
        self.run_cadence(
            'send_digest', count=6, day_offset=3,
            interval_minutes=30, duration_ms=200,
        )

        report = self.report()

        self.assertEqual(report['rows'][0]['name'], 'expire_items')

    def test_the_summary_names_the_command_that_is_running_into_itself(self):
        # 摘要要点名。一排数字不叫结论。
        for index in range(3):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=120,
            )
        for index in range(3, 6):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=15 * 60 * 1000,
            )

        report = self.report()

        self.assertIn('expire_items', report['summary'])
        self.assertIn('此处只报告', report['summary'])
        # 摘要可以谈重叠，因为那就是它在报告的东西；但它不能谈「重排」「挪任务」，
        # 那属于改写而不是报告。
        for forbidden in ('已重排', '已自动挪', '改为每', '已调整'):
            self.assertNotIn(forbidden, report['summary'])

    def test_an_empty_window_says_so_rather_than_reporting_no_overlap(self):
        # 没有台账和「没有重叠」是两件事。空窗口要把话说清楚。
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['rows'], [])
        self.assertIn('还没有已记录', report['summary'])

    def test_dry_runs_are_excluded(self):
        # dry-run 什么都没发，它的耗时说明不了调度：一次预演可以故意跑很久。
        for index in range(6):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=15 * 60 * 1000,
                note='dry-run',
            )

        report = self.report()

        self.assertFalse(report['has_data'])

    def test_the_module_never_writes_back_into_the_ledger_it_reads(self):
        # 只读台账：不重排调度、不改间隔、不把统计写回台账，也不为了记录一次重叠
        # 而往台账里写第三行。一个能改写台账的统计，会让台账变成记录被它自己抠出来的
        # 形状的地方，后面再读的人就分不清哪些是运行、哪些是统计的痕迹。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=10, duration_ms=15 * 60 * 1000,
        )
        before = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'name', 'status', 'duration_ms', 'note')
        )

        self.report()

        after = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'name', 'status', 'duration_ms', 'note')
        )
        self.assertEqual(before, after)
        self.assertEqual(TaskRun.objects.count(), 6)

    def test_the_previous_period_is_not_used_for_the_interval(self):
        # 间隔不看上一周期：跨窗口起点的那一个间隔说的是窗口在哪里被切开，
        # 不是命令的节奏。所以上一周期的运行不该影响本周期这一行。
        self.run_cadence(
            'expire_items', count=6, day_offset=2,
            interval_minutes=10, duration_ms=120,
        )
        for index in range(6):
            self.make_run(
                'expire_items',
                started_at=self.moment_plus(40, 9, 0, index * 600),
                duration_ms=120,
            )

        report = self.report()

        row = self.row_of(report, 'expire_items')
        self.assertEqual(row['run_count'], 6)
        self.assertEqual(row['interval_ms'], 10 * 60 * 1000)


class DashboardIntegrationTests(TestCase):
    """The panel must actually render, and the export must actually carry it."""

    def setUp(self):
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)
        self.user = User.objects.create_user(
            username='overlap-staff', password='safe-password-123', is_staff=True,
        )

    def moment_plus(self, day_offset, hour, minute, offset_minutes):
        """本地时刻往后推若干分钟，用来排跨小时的等间隔节奏。

        间隔是三十分钟、四十分钟时，index * interval 会越过整点，而
        `time()` 只接受 0..59 的分钟。等间隔是这个模块对夹具的要求，
        所以这里必须能跨小时，而不是让用例迁就分钟的取值上限。
        """
        return self.local_moment(day_offset, hour, minute) + timedelta(
            minutes=offset_minutes,
        )

    def local_moment(self, day_offset, hour, minute=0):
        local_date = self.today - timedelta(days=day_offset)
        moment = timezone.make_aware(datetime.combine(local_date, time(hour=hour, minute=minute)))
        upper = timezone.make_aware(datetime.combine(local_date, time(hour=23, minute=59)))
        if upper > self.now:
            return self.now - timedelta(minutes=1)
        return moment

    def make_run(self, name, *, status='succeeded', started_at=None,
                 duration_ms=120, note=''):
        started = started_at or self.local_moment(1, 9)
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=started,
            finished_at=started,
            duration_ms=duration_ms,
            metrics={},
            note=note,
        )

    def seed_overlapping_command(self):
        for index in range(3):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=120,
            )
        for index in range(3, 6):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 9, index * 10),
                duration_ms=15 * 60 * 1000,
            )

    def test_dashboard_renders_the_overlap_panel(self):
        # 面板要能渲染出来：模块接上去而模板没接，看板上就少一整块。
        self.seed_overlapping_command()
        self.client.force_login(self.user)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '命令的自我重叠')
        overlap = response.context['task_run_overlap']
        self.assertEqual(overlap['overlapping_count'], 1)
        row = next(item for item in overlap['rows'] if item['name'] == 'expire_items')
        self.assertTrue(row['is_self_overlapping'])

    def test_the_export_carries_the_overlap_section(self):
        # 导出的口径要和面板一致：导出的表要和面板对得上，否则下载回去的那张
        # 表会被读成另一个项目的数据。
        self.seed_overlapping_command()
        self.client.force_login(self.user)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('命令的自我重叠', text)
        self.assertIn('重叠对数', text)
        self.assertIn('重叠代价', text)

    def test_the_export_reports_the_thresholds_it_used(self):
        # 门槛要随表导出：下载回去的表要能自证它是在哪几条门槛下得出结论的。
        self.seed_overlapping_command()
        self.client.force_login(self.user)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        text = response.content.decode('utf-8-sig')
        self.assertIn(str(MIN_RUNS_FOR_RHYTHM), text)
        self.assertIn(str(MIN_OVERLAP_PAIRS), text)
        self.assertIn(str(OVERLAP_SLOWDOWN), text)
        self.assertIn(str(APPROACHING_INTERVAL), text)