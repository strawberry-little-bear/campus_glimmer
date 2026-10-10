# -*- coding: utf-8 -*-
"""Cover the cross-period comparison of the schedule's own health.

The health panel answers questions about one window, and the comparison answers
the one that comes next: is this period better or worse than the one before it.
These tests pin down the properties that decide whether that answer is real or
an artefact of how the windows were cut.

Three of them matter more than the rest. A run belongs to the window it
*started* in, because a command that hung across the boundary belongs to the
period that had to live with it. A duration is reported as median, maximum and
slow-run count rather than as a mean, because a mean is the one statistic a hung
run cannot move and a panel built on one would show thirty-five seconds for a
command that never took thirty-five seconds. And a window below the floor is
reported as figures with no direction, because four runs produce a rate that
moves in steps of twenty-five points.

The rest are boundaries: dry runs are excluded for the reason the health module
excludes them, run counts never carry a direction, a metric that exists in only
one window is reported without a delta, and nothing in this module reaches back
into the ledger it reads.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import TaskRun
from .task_run_trend import (
    DURATION_DRIFT,
    DIRECTION_METRICS,
    SLOW_RUN_FACTOR,
    TREND_DIRECTIONS,
    TREND_LABELS,
    build_task_run_trend,
)


class TaskRunTrendTests(TestCase):
    """Which runs land in which window, and what the comparison may say."""

    def setUp(self):
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)

    def make_run(self, name, *, status='succeeded', started_at=None, duration_ms=120, note=''):
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
        # The current window has to sit after the rows: auto_now_add writes the
        # real insert time, which is later than the self.now captured in setUp.
        return build_task_run_trend(days=days, now=self.now + timedelta(minutes=5))

    def command(self, report, name):
        return next((row for row in report['commands'] if row['name'] == name), None)

    def metric(self, report, key):
        for row in report['metric_rows']:
            if row['key'] == key:
                return row
        return None

    def fill_window(self, name, *, count, started_at, status='succeeded', duration_ms=100):
        """`count` runs for one command, all started inside one window.

        The runs are spread over several days rather than placed at one instant
        so that the median, the maximum and the slow-run count are each computed
        from a distribution rather than from a single repeated value.
        """
        for index in range(count):
            self.make_run(
                name,
                status=status,
                started_at=started_at - timedelta(hours=index),
                duration_ms=duration_ms,
            )

    # -- which window a run belongs to -------------------------------------

    def test_a_run_is_attributed_to_the_window_it_started_in(self):
        # 这一条决定了这个面板说的话可不可俥相信：一个在边界上各挂了两个小时的命令，属于那个得打量它的周期，而不是继承了缓慢的那一个。
        self.fill_window(
            'send_operations_digest', count=4,
            started_at=self.now - timedelta(days=2),
        )
        # 一次在上一周期开始、在本周期结束的运行：它得算进上一周期。
        self.make_run(
            'send_operations_digest',
            started_at=self.now - timedelta(days=35),
            duration_ms=900000,
        )

        report = self.report()

        digest = self.command(report, 'send_operations_digest')
        self.assertEqual(digest['current']['run_count'], 4)
        self.assertEqual(digest['previous']['run_count'], 1)
        # 挂染的那一次在上一周期里表现为异常慢，而不是把本周期的中位数抬起来。
        self.assertEqual(digest['previous']['max_duration_ms'], 900000)
        self.assertEqual(digest['current']['max_duration_ms'], 100)

    def test_the_two_windows_are_bounded_by_local_midnight(self):
        # 两个窗口都以本地半夜为界，和搜索趋势同一个函数：同一次运行在这里被计入，在那里也必须被计入。
        report = self.report(days=30)

        self.assertEqual(
            report['current_period_start'].time(), timezone.datetime.min.time(),
        )
        local_end = timezone.localtime(report['current_period_end'])
        self.assertEqual(
            timezone.localtime(report['current_period_start']),
            local_end.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=29),
        )
        self.assertEqual(
            report['previous_period_start'],
            report['current_period_start'] - timedelta(days=30),
        )

    def test_a_run_in_neither_window_is_not_counted_anywhere(self):
        self.make_run('expire_items', started_at=self.now - timedelta(days=200))

        report = self.report(days=30)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['current']['run_count'], 0)
        self.assertEqual(report['previous']['run_count'], 0)

    # -- what the duration figures must be --------------------------------

    def test_duration_is_reported_as_median_not_mean(self):
        # 一个命令十一次四秒、一次四分钟，均值是三十五秒，而这个数字描述了没有任何一次真正发生过的运行。
        durations = [4000] * 11 + [240000]
        for index, duration in enumerate(durations):
            self.make_run(
                'send_operations_digest',
                started_at=self.now - timedelta(days=2, hours=index),
                duration_ms=duration,
            )

        report = self.report()

        row = self.command(report, 'send_operations_digest')
        self.assertEqual(row['current']['median_duration_ms'], 4000)
        self.assertEqual(row['current']['max_duration_ms'], 240000)
        self.assertEqual(row['current']['last_duration_ms'], 4000)
        self.assertNotIn('avg_duration_ms', row['current'])
        # 那一次后半子得被独立数出来，而不是被平均掉。
        self.assertEqual(row['current']['slow_run_count'], 1)

    def test_a_slow_run_is_one_that_took_several_times_the_median(self):
        # “异常慢”按当期自己的中位数判定，不用固定毫秒数：七个命令相差几个数量级，固定值会随着机器变快而失灵。
        self.assertEqual(SLOW_RUN_FACTOR, 3)
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=2, hours=index),
                duration_ms=1000,
            )
        self.make_run(
            'expire_items',
            started_at=self.now - timedelta(days=2, hours=5),
            duration_ms=2000,
        )
        self.make_run(
            'expire_items',
            started_at=self.now - timedelta(days=2, hours=6),
            duration_ms=9000,
        )

        report = self.report()

        row = self.command(report, 'expire_items')
        # 2000 毫秒只是中位数的两倍，不算异常；9000 才算。
        self.assertEqual(row['current']['slow_run_count'], 1)

    def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two(self):
        for index, duration in enumerate((100, 200, 300, 400)):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=2, hours=index),
                duration_ms=duration,
            )

        report = self.report()

        row = self.command(report, 'expire_items')
        self.assertEqual(row['current']['median_duration_ms'], 250)

    def test_a_duration_drift_is_relative_to_the_previous_median(self):
        # 比率可以用百分点比较，因为两边本来就是百分比；耗时没有这样的刻度，因此它的门槛只能是相对的。
        self.assertEqual(int(DURATION_DRIFT * 100), 25)
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=40 + index),
                duration_ms=1000,
            )
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=2, hours=index),
                duration_ms=1400,
            )

        report = self.report()

        row = self.command(report, 'expire_items')
        duration_row = next(
            item for item in row['metric_rows'] if item['key'] == 'median_duration_ms'
        )
        self.assertEqual(duration_row['delta_display'], '+400.0')
        # 1400 相对 1000 是 +40%，超过 25% 的门槛，因此给方向。
        self.assertEqual(duration_row['direction'], 'rising')

    def test_a_duration_that_moved_enough_is_given_a_direction(self):
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=40 + index),
                duration_ms=1000,
            )
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=2, hours=index),
                duration_ms=2000,
            )

        report = self.report()

        row = self.command(report, 'expire_items')
        self.assertEqual(row['driver_key'], 'median_duration_ms')
        self.assertEqual(row['direction'], 'rising')
        # 逗留变慢是坏消息，面板得知道该给它什么颜色，而不需要再查一次表。
        self.assertEqual(row['tone'], 'bad')
        self.assertIn('expire_items', report['summary'])
        self.assertIn('耗时中位数', report['summary'])

    # -- which figures may carry a direction -------------------------------

    def test_only_the_rate_and_the_duration_carry_a_direction(self):
        # 运行次数变多只说明周期更忙，不说明更好；把它叫“上升”会教会读者把量的变化读成质的变化。
        self.assertEqual(DIRECTION_METRICS, ('success_rate', 'median_duration_ms'))
        self.fill_window(
            'expire_items', count=8,
            started_at=self.now - timedelta(days=2),
        )
        self.fill_window(
            'send_operations_digest', count=4,
            started_at=self.now - timedelta(days=40),
        )

        report = self.report()

        row = self.command(report, 'expire_items')
        self.assertFalse(row['has_sample'])
        keys = [item['key'] for item in row['metric_rows'] if item['has_direction']]
        self.assertEqual(keys, ['success_rate', 'median_duration_ms'])
        for item in row['metric_rows']:
            if item['key'] == 'run_count':
                self.assertIsNone(item['direction'])
                self.assertEqual(item['direction_label'], '')
                self.assertEqual(item['delta_display'], '+8.0')

    def test_a_window_below_the_floor_is_reported_but_not_compared(self):
        # 四次运行算出来的成功率一步一个五分之一，这样的差值是算术而不是证据。
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=40 + index),
            )
        for index in range(3):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=2, hours=index),
            )

        report = self.report()

        row = self.command(report, 'expire_items')
        self.assertFalse(row['has_sample'])
        self.assertFalse(report['has_sample'])
        # 数值仍然给出，只是不给方向：读者能看到发生了什么，但不被告诉这就是趋势。
        for item in row['metric_rows']:
            if item['has_direction']:
                self.assertEqual(item['direction'], 'insufficient')
                self.assertEqual(item['direction_label'], TREND_LABELS['insufficient'])
        self.assertIn('达不到门槛', report['summary'])

    def test_a_metric_that_exists_in_only_one_window_has_no_delta(self):
        # 只有一边有数值时没有可减的另一边，据伪造一个就是在没人测过的数字上做算术。
        self.fill_window(
            'expire_items', count=4, started_at=self.now - timedelta(days=2),
        )

        report = self.report()

        row = self.command(report, 'expire_items')
        self.assertEqual(row['current']['run_count'], 4)
        self.assertEqual(row['previous']['run_count'], 0)
        for item in row['metric_rows']:
            if item['key'] == 'median_duration_ms':
                self.assertIsNone(item['previous'])
                self.assertIsNone(item['delta'])
                self.assertEqual(item['delta_display'], '—')
                self.assertEqual(item['direction'], 'insufficient')
        # 本周期够、上一周期没有：两个窗口都必须过门槛才给方向，所以这里不给。
        self.assertFalse(row['has_sample'])

    def test_the_direction_vocabulary_matches_the_other_cross_period_panels(self):
        # 多个面板并排摆在同一张看板上，读者在一处学会“上升”是十个百分点，不该在另一处重新学一个数。
        self.assertEqual(
            TREND_DIRECTIONS,
            ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient'),
        )
        self.assertEqual(TREND_LABELS['rising'], '较上期上升')
        self.assertEqual(TREND_LABELS['insufficient'], '样本不足')

    # -- what the comparison must never do ---------------------------------

    def test_dry_runs_are_excluded_for_the_reason_the_health_module_excludes(self):
        # 一次什么都没发的预演，既不能为调度作保，它的耗时也测不出什么关于调度的东西。
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=40 + index),
            )
        for index in range(4):
            self.make_run(
                'expire_items',
                status='failed',
                started_at=self.now - timedelta(days=2, hours=index),
                duration_ms=10,
                note='dry-run',
            )

        report = self.report()

        row = self.command(report, 'expire_items')
        self.assertEqual(row['current']['run_count'], 0)
        self.assertEqual(row['previous']['run_count'], 4)
        self.assertFalse(report['has_data'] is False)

    def test_the_module_never_reaches_back_into_the_ledger_it_reads(self):
        # 这里只读：不改 STALE_AFTER、不重跑失败的命令、不改排程，也不把统计写回台账。
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=40 + index),
            )
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=2, hours=index),
            )
        before = TaskRun.objects.count()

        report = self.report()

        self.assertEqual(TaskRun.objects.count(), before)
        # 摘要可以谈成功率和耗时，因为那就是它在报告的东西；但它不能谈“重跑”或“改动”，那属于改写而不是报告。
        self.assertIn('成功率', report['summary'] + '耗时')
        for forbidden in ('自动重试', '重新运行', '调整调度', '改为'):
            self.assertNotIn(forbidden, report['summary'])

    def test_a_command_that_ran_in_neither_window_is_not_listed(self):
        # 从未运行过的命令是健康面板的问题，它已经把这个命令报成静默；在这里重复一次，只会把表格填满两边都空的行。
        for index in range(4):
            self.make_run(
                'expire_items',
                started_at=self.now - timedelta(days=40 + index),
            )
        self.fill_window(
            'sync_academic_calendar', count=4,
            started_at=self.now - timedelta(days=40),
        )
        self.fill_window(
            'send_saved_search_digest', count=4,
            started_at=self.now - timedelta(days=2),
        )

        report = self.report()

        names = [row['name'] for row in report['commands']]
        self.assertIn('expire_items', names)
        self.assertIn('send_saved_search_digest', names)
        # 只在上一周期跑过、本周期没跑的命令仍然在表里：它的“消失”本身就是要报告的事情。
        self.assertIn('sync_academic_calendar', names)

    def test_the_empty_history_reports_rather_than_raising(self):
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['current']['run_count'], 0)
        self.assertEqual(report['previous']['run_count'], 0)
        self.assertIn('运行台账', report['summary'])

    def test_the_success_rate_direction_names_the_command_that_moved(self):
        # 摘要以成功率开头，因为那才是回答“调度是否变得更可靠”的数字，也是读者真正会照着做决定的那一个。
        for index in range(4):
            self.make_run(
                'send_operations_digest',
                started_at=self.now - timedelta(days=40 + index),
            )
        for index in range(3):
            self.make_run(
                'send_operations_digest',
                status='failed',
                started_at=self.now - timedelta(days=2, hours=index),
            )
        self.make_run(
            'send_operations_digest',
            started_at=self.now - timedelta(days=2, hours=4),
        )

        report = self.report()

        row = self.command(report, 'send_operations_digest')
        self.assertEqual(row['current']['success_rate'], 25.0)
        self.assertEqual(row['previous']['success_rate'], 100.0)
        self.assertEqual(row['direction'], 'falling')
        # 一个命令停止运行、另一个反而变忙，总数上仍然看得见，而不会相互抵销。
        self.assertEqual(row['tone'], 'bad')


class DashboardIntegrationTests(TestCase):
    """The panel must actually render on the operations dashboard."""

    def make_run(self, name, *, status='succeeded', minutes_ago=0, note=''):
        started = timezone.now() - timedelta(minutes=minutes_ago)
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=started,
            finished_at=started,
            duration_ms=120,
            metrics={},
            note=note,
        )

    def test_dashboard_renders_the_trend_panel(self):
        staff = User.objects.create_user(
            username='trend-staff', password='safe-password-123', is_staff=True,
        )
        for index in range(6):
            self.make_run('expire_items', minutes_ago=60 * 24 * 40 + index)
        for index in range(6):
            self.make_run('expire_items', minutes_ago=60 * 24 * 2 + index)
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '定时任务健康度的跨周期趋势')
        self.assertContains(response, 'expire_items')
        trend = response.context['task_run_trend']
        self.assertEqual(trend['current']['run_count'], 6)
        self.assertEqual(trend['previous']['run_count'], 6)
        # 两个窗口同样的运行次数、同样的耗时，摘要就得说“没有明显变化”；为了报告一个趋势而去编一个出来，比说没变化更差。
        self.assertTrue(trend['has_sample'])
        self.assertEqual(trend['current']['success_rate'], 100.0)
        self.assertEqual(trend['previous']['success_rate'], 100.0)
        self.assertIn('没有明显变化', trend['summary'])
        # 面板上的方向徽标得能告诉读者这个数字往哪边算好，而不需要再查一次表。
        row = next(item for item in trend['metric_rows'] if item['key'] == 'success_rate')
        self.assertEqual(row['direction'], 'flat')

    def test_dashboard_renders_the_empty_panel(self):
        staff = User.objects.create_user(
            username='trend-staff-empty', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '运行台账')
        self.assertFalse(response.context['task_run_trend']['has_data'])