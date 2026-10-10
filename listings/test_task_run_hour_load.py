# -*- coding: utf-8 -*-
"""守住运行时长的按小时分布的边界：小时级排队要能看见，而巧合不许被读成队列。

看板上关于调度的每一个数字都按命令切：健康、跨周期趋势、失败原因、耗时分位。
那是最先会问的切法，因为第一个问题是"哪个命令坏了"，但它有一处漏洞——七个
命令共用同一块磁盘、同一个连接池和校园接口那点配额，于是两条各自四秒的命令配
在同一分钟启动时会跑成十一秒。两个命令都没坏，而按命令切的面板谁都看不见这件
事：每个都像是莫名变慢了，唯一共通之处只有它们被安排在同一个小时。

这一组用例钉住的就是这件事。有四条边界比其余更要紧。小时按本地时钟切，不按
UTC——CI 跑 UTC 而项目时区是东八区，本地凌晨两点的那次运行在 UTC 里是前一天
傍晚六点，按 UTC 分桶会把拥塞报在一个没人配过的小时刻上。样本少的小时不判拥
塞——三次运行是巧合，不是队列。"这一小时比平时慢"用命令自己的全周期中位数
当基线，不是小时之间互比——否则一条匀速慢的命令会让它跑过的每个小时都显得
拥塞。以及这个模块只读台账、从不写回，一个能改写台账的统计会让台账变成记录
被它自己抠出来的形状的地方。
"""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import TaskRun
from .task_run_hour_load import (
    HOUR_SLOWDOWN_FACTOR,
    MAX_HOUR_ROWS,
    MIN_HOUR_RUNS,
    build_task_run_hour_load,
)


class TaskRunHourLoadTests(TestCase):
    """Which runs land in which local hour, and when an hour counts as crowded."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='hour-load-user', password='safe-password-123',
        )
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)

    def local_moment(self, day_offset, hour, minute=0):
        """一个被钉住的本地时刻：先算本地日期，再取该日的指定小时。

        时区钉死很重要：CI 跑 UTC、项目时区是东八区，如果直接用
        `timezone.now() - timedelta(days=...)`，本地零点前后那几个小时会跨日，
        同一个用例在这里通过、在 UTC 下失败。先把本地日期算出来再组合时刻，
        小时就一定是测试写下的那个小时。
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
        return build_task_run_hour_load(
            days=days, now=self.now + timedelta(minutes=5),
        )

    def row(self, report, hour):
        return next((row for row in report['hours'] if row['hour'] == hour), None)

    def test_runs_are_bucketed_by_the_local_hour_they_started_in(self):
        # 小时按本地时钟切，不按 UTC：CI 跑 UTC 而项目时区是东八区，
        # 按 UTC 分桶会把拥塞报在一个没人配过的小时刻上。
        for index in range(3):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=9000)
        report = self.report()
        row = self.row(report, 2)
        self.assertIsNotNone(row)
        self.assertEqual(row['run_count'], 3)
        self.assertEqual(row['label'], '02:00')
        # 同一天 18:00 UTC 的那次运行不该被算进凌晨两点这一小时。
        self.assertIsNone(self.row(report, 18))

    def test_an_hour_with_too_few_runs_is_not_crowded(self):
        # 三次运行是巧合，不是队列：样本不够的小时不许标成拥塞，
        # 门槛要摆在面板上，让一个安静的面板说的是"门槛是三次"而不是别的。
        for index in range(MIN_HOUR_RUNS - 1):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=9000)
        report = self.report()
        row = self.row(report, 2)
        self.assertEqual(row['run_count'], MIN_HOUR_RUNS - 1)
        self.assertFalse(row['is_crowded'])
        self.assertEqual(report['crowded_hour_count'], 0)
        self.assertEqual(report['min_hour_runs'], MIN_HOUR_RUNS)

    def test_a_command_is_slow_in_an_hour_against_its_own_usual_run(self):
        # "这一小时比平时慢"用命令自己的全周期中位数当基线，不是小时之间互比：
        # 否则一条匀速慢的命令会让它跑过的每个小时都显得拥塞。
        # 平时 1000 毫秒，凌晨两点那一小时 20000 毫秒，是该慢。
        for index in range(6):
            self.make_run('expire_items', started_at=self.local_moment(2, 10), duration_ms=1000)
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=20000)
        report = self.report()
        row = self.row(report, 2)
        self.assertTrue(row['is_crowded'])
        self.assertIn('expire_items', row['slow_commands'])

    def test_a_uniformly_slow_command_does_not_make_its_hour_crowded(self):
        # 一条每次都慢的命令不该把每个跑过的小时都标成拥塞：
        # 那是"按速度给命令排名"戴上了"拥塞"两个字。
        for index in range(4):
            self.make_run('expire_items', started_at=self.local_moment(2, 10), duration_ms=8000)
        for index in range(4):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=8000)
        report = self.report()
        row = self.row(report, 2)
        self.assertEqual(row['run_count'], 4)
        self.assertFalse(row['is_crowded'])
        self.assertEqual(row['slow_commands'], [])

    def test_a_dry_run_is_excluded_the_way_the_health_panel_excludes_it(self):
        # 预演照健康面板的口径排除：一次没发出东西的运行不能替调度作证，
        # 它的耗时也对调度一无所用。
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=1000)
        self.make_run(
            'expire_items', started_at=self.local_moment(1, 2), duration_ms=9000, note='dry-run',
        )
        report = self.report()
        row = self.row(report, 2)
        self.assertEqual(row['run_count'], MIN_HOUR_RUNS)
        self.assertFalse(row['is_crowded'])

    def test_a_run_belongs_to_the_hour_it_started_in(self):
        # 运行按开始时刻归期：卡在小时边界上的那次归它开始的那个小时，
        # 不归它结束的那个小时，否则"下一个小时变慢了"会掩盖真正卡住的那个小时。
        self.make_run('expire_items', started_at=self.local_moment(1, 1, 59), duration_ms=1000)
        self.make_run('expire_items', started_at=self.local_moment(1, 1, 30), duration_ms=1000)
        report = self.report()
        row = self.row(report, 1)
        self.assertEqual(row['run_count'], 2)
        self.assertIsNone(self.row(report, 2))

    def test_only_the_busiest_hours_are_reported(self):
        # 只报最忙的若干个：二十四个小时里二十个基本是空的，
        # 那不是发现而是日历，要滚动过空行的人会停止阅读。
        for hour in range(20):
            for index in range(hour % 3 + 1):
                self.make_run('expire_items', started_at=self.local_moment(1, hour), duration_ms=1000)
        report = self.report()
        self.assertEqual(len(report['hours']), MAX_HOUR_ROWS)
        self.assertEqual(report['busiest_hour'], report['hours'][0]['hour'])
        counts = [row['run_count'] for row in report['hours']]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_the_previous_period_is_reported_for_the_same_local_hour(self):
        # 上一周期同一小时并排报出来：这一小时变忙了还是变空了，
        # 单看本周期回答不了，而两个周期用同一套本地午夜边界切开。
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(10, 2), duration_ms=1000)
        for index in range(2):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=2000)
        report = self.report(days=7)
        row = self.row(report, 2)
        self.assertEqual(row['previous_run_count'], MIN_HOUR_RUNS)
        self.assertEqual(row['run_count'], 2)
        self.assertEqual(row['previous_median_duration_ms'], 1000)
        self.assertEqual(row['median_duration_ms'], 2000)

    def test_a_hour_with_no_claimed_slow_command_is_not_crowded(self):
        # 门槛是同时满足两个条件：够多次运行，且这一小时里有命令比它自身平时慢。
        # 只满足其中一个都不算，否则"忙"会被读成"堵"。
        for index in range(MIN_HOUR_RUNS + 2):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=1000)
        report = self.report()
        row = self.row(report, 2)
        self.assertFalse(row['is_crowded'])

    def test_an_empty_history_reports_rather_than_raising(self):
        # 空历史报"没有数据"，不抛异常：面板要在台账刚建好那天也能打开。
        report = self.report()
        self.assertFalse(report['has_data'])
        self.assertEqual(report['hours'], [])
        self.assertEqual(report['crowded_hours'], [])
        self.assertEqual(report['crowded_hour_count'], 0)
        self.assertIn('运行台账', report['summary'])

    def test_a_quiet_window_says_so_rather_than_leaving_it_ambiguous(self):
        # 没有拥塞小时时说清楚，别让读者去猜一个安静的面板是"没堵"还是"没数据"。
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=1000)
        report = self.report()
        self.assertTrue(report['has_data'])
        self.assertEqual(report['crowded_hour_count'], 0)
        self.assertIn(str(MIN_HOUR_RUNS), report['summary'])

    def test_the_slowdown_threshold_is_reported_so_a_reader_can_read_the_rows(self):
        # 门槛和倍数都要报出来：一行被标成拥塞，读者得知道是拿什么比的。
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=1000)
        report = self.report()
        self.assertEqual(report['slowdown_factor'], HOUR_SLOWDOWN_FACTOR)
        self.assertEqual(report['max_hour_rows'], MAX_HOUR_ROWS)

    def test_a_crowded_hour_names_the_commands_that_got_slower(self):
        # 拥塞小时要说出是哪些命令变慢了：只说"这个小时堵了"
        # 而不说是谁，读者没法决定挪哪一条 crontab。
        for index in range(6):
            self.make_run('expire_items', started_at=self.local_moment(2, 10), duration_ms=1000)
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=4000)
            self.make_run('send_lifecycle_reminders', started_at=self.local_moment(1, 2), duration_ms=1000)
        report = self.report()
        row = self.row(report, 2)
        self.assertTrue(row['is_crowded'])
        self.assertIn('expire_items', row['slow_commands'])
        self.assertNotIn('send_lifecycle_reminders', row['slow_commands'])
        self.assertEqual(row['command_count'], 2)
        self.assertIn('expire_items', report['summary'])

    def test_dashboard_renders_the_hour_load_panel(self):
        # 面板要能渲染出来：模块接上去而模板没接，看板上就少一整块。
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=4000)
        for index in range(4):
            self.make_run('expire_items', started_at=self.local_moment(2, 10), duration_ms=1000)
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '运行时长的按小时分布')

    def test_the_export_carries_the_hour_load_section(self):
        # 导出的口径要和面板一致：上一周期留空而不是填 0，
        # 否则下载回去的那张表会被读成"上一周期这个小时一次都没跑"。
        for index in range(6):
            self.make_run('expire_items', started_at=self.local_moment(2, 10), duration_ms=1000)
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=4000)
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('运行时长的按小时分布', text)
        self.assertIn('本地小时', text)
        self.assertIn('倍数', text)

    def test_the_export_leaves_a_missing_previous_hour_blank(self):
        # 上一周期同一个小时没有运行就留空：填 0 会被读成"确实一次都没跑"，
        # 而"没跑"和"没记录"在这张表上要能分开。
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=4000)
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('上一周期同一小时', text)

    def test_the_module_never_writes_back_into_the_ledger_it_reads(self):
        # 只读台账。一个能改写台账的统计会让台账变成记录被它自己抠出来的形状
        # 的地方，后面再读的人就分不清哪些是运行、哪些是统计的痕迹。
        before = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'status', 'duration_ms', 'note')
        )
        for index in range(MIN_HOUR_RUNS):
            self.make_run('expire_items', started_at=self.local_moment(1, 2), duration_ms=4000)
        for index in range(4):
            self.make_run('expire_items', started_at=self.local_moment(2, 10), duration_ms=1000)
        self.report()
        after = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'status', 'duration_ms', 'note')
        )
        self.assertEqual(before, after[:len(before)])
        self.assertEqual(len(after) - len(before), MIN_HOUR_RUNS + 4)