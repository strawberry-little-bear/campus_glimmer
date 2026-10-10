# -*- coding: utf-8 -*-
"""守住失败原因的跨周期变化的边界：一个正在恶化的尖峰和一个一直很高的平台不是同一件事。

失败原因分类面板回答的是「这一周期的失败长什么样」，它给出的每一个数字都是
一个窗口内的计数。而真正决定要不要动手的是下一个问题：这种失败是在变好还是
在变坏。十五次外部依赖失败，既可能是一直平平稳稳的十五次，也可能是上期只有
两次、这期涨上来的十五次——前者该做的是把长期问题排上日程，后者该做的是现在
就去查依赖。窗口总数答不了这件事，因为两种都是十五。

这一组用例钉住的就是「按原因切、再按周期切」这件事。有六条边界比其余更要紧。
一次运行按它开始的时刻归期，且两个周期用同一套本地午夜边界，否则这里说上升
的东西在别处会被算进一个健康的周期。低于门槛的类别只报数字、不给方向——两次
对一次是差一次，把它叫上升是教读者把算术当成证据。只给失败次数方向，且上升
是坏消息。只在一个周期出现的类别没有差值，没有上期可减。以及这个模块只读
台账、从不写回——一个能改写台账的统计，会让台账变成记录被它自己抠出来的形状
的地方。
"""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import TaskRun
from .task_run_failure_causes import (
    CAUSE_CONFIG,
    CAUSE_DATA,
    CAUSE_EXTERNAL,
    CAUSE_UNKNOWN,
)
from .task_run_failure_cause_trend import (
    CAUSE_SHARE_DRIFT,
    DIRECTION_TONES,
    MIN_CAUSE_RUNS,
    MIN_PERIOD_FAILURES,
    TREND_DIRECTIONS,
    TREND_LABELS,
    build_task_run_failure_cause_trend,
)


class TaskRunFailureCauseTrendTests(TestCase):
    """Which failures land in which period, and what the comparison may say."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='cause-trend-user', password='safe-password-123',
        )
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)

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

    def make_run(self, name, *, status='succeeded', started_at=None, duration_ms=120, note=''):
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
        return build_task_run_failure_cause_trend(
            days=days, now=self.now + timedelta(minutes=5),
        )

    def cause_of(self, report, cause):
        return next((row for row in report['causes'] if row['cause'] == cause), None)

    def fail_in_period(self, cause_note, *, count, day_offset, hour=9):
        """`count` failures of one cause, all inside one period."""
        for index in range(count):
            self.make_run(
                'expire_items',
                status='failed',
                started_at=self.local_moment(day_offset, hour, index),
                note=cause_note,
            )

    # -- which period a failure belongs to --------------------------------

    def test_a_failure_is_attributed_to_the_period_it_started_in(self):
        # 这一条决定了这个面板说的话可不可信：一次在上一周期最后一天开始、本周期第一天
        # 才结束的失败，属于那个得料理它的周期，而不是继承了失败的那一个。两次都在窗口内，
        # 只有归期能把它们分开。
        self.fail_in_period('OperationalError: db gone', count=4, day_offset=40, hour=2)
        self.fail_in_period('IntegrityError: null', count=6, day_offset=40, hour=3)
        # 上一周期的最后一天：本地日期上就是窗口起点前一日。
        boundary = self.local_moment(31, 2)
        self.make_run(
            'expire_items', status='failed',
            started_at=boundary, note='OperationalError: db gone',
        )
        self.fail_in_period('OperationalError: db gone', count=5, day_offset=2, hour=2)
        self.fail_in_period('IntegrityError: null', count=6, day_offset=2, hour=3)

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        self.assertEqual(external['current_count'], 5)
        self.assertEqual(external['previous_count'], 5)

    def test_a_failure_on_the_boundary_goes_to_the_period_that_lived_with_it(self):
        # 卡在边界上的那次归它开始的那个周期，不归它结束的那个周期：按结束时刻归期，
        # 会把一个坏周期的失败算成下一个周期的失败，于是这里说上升、别处说健康。
        boundary = self.local_moment(31, 2)
        self.make_run(
            'expire_items', status='failed',
            started_at=boundary, note='OperationalError: db gone',
        )
        self.fail_in_period('IntegrityError: null', count=6, day_offset=2, hour=3)

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        self.assertEqual(external['current_count'], None)
        self.assertEqual(external['previous_count'], 1)
        self.assertEqual(external['direction'], 'gone')

    def test_the_two_periods_are_bounded_by_local_midnight(self):
        # 两个周期都以本地半夜为界，和调度趋势面板同一个函数：同一次运行在这里被计入，
        # 在那里也必须被计入。两个面板对「哪次运行归哪个周期」各说各话，读者就无法
        # 判断哪一个在说谎。
        report = self.report(days=30)

        self.assertEqual(
            report['window_start'].time(), timezone.datetime.min.time(),
        )
        local_end = timezone.localtime(report['window_end'])
        self.assertEqual(
            timezone.localtime(report['window_start']),
            local_end.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=29),
        )
        self.assertEqual(
            report['previous_window_start'],
            report['window_start'] - timedelta(days=30),
        )

    def test_a_failure_in_neither_period_is_not_counted_anywhere(self):
        self.fail_in_period('OperationalError: db gone', count=3, day_offset=200)

        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['causes'], [])
        self.assertEqual(report['total_failed'], 0)
        self.assertEqual(report['previous_total_failed'], 0)

    # -- what the comparison is for --------------------------------------

    def test_a_rising_cause_is_a_spike_that_needs_looking_at(self):
        # 外部依赖失败从两次涨到十五次，而其他类别没动：这是依赖正在坏，
        # 要动的是那多出来的十三次，不是那个一直很高的平台。
        self.fail_in_period('OperationalError: db gone', count=2, day_offset=40, hour=2)
        self.fail_in_period('IntegrityError: null', count=18, day_offset=40, hour=3)
        self.fail_in_period('OperationalError: db gone', count=15, day_offset=2, hour=2)
        self.fail_in_period('IntegrityError: null', count=6, day_offset=2, hour=3)

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        self.assertEqual(external['current_count'], 15)
        self.assertEqual(external['previous_count'], 2)
        self.assertEqual(external['delta'], 13)
        self.assertEqual(external['delta_display'], '+13')
        self.assertEqual(external['direction'], 'rising')
        self.assertEqual(external['direction_label'], TREND_LABELS['rising'])
        # 失败次数上升是坏消息，面板得知道该给它什么颜色，而不需要再查一次表。
        self.assertEqual(external['tone'], 'bad')
        self.assertEqual(DIRECTION_TONES['rising'], 'bad')
        self.assertIn('外部依赖', report['summary'])
        self.assertTrue(report['has_direction'])
        # 份额口径让「占比从一成变成六成」这件事本身也可读，读者不用自己拿两个数相除。
        self.assertEqual(external['previous_share'], 10.0)
        self.assertEqual(external['current_share'], 71.4)

    def test_a_steady_cause_is_a_platform_not_a_change(self):
        # 同样是十五次，上期也是十五次，而且它在失败里的占比也没动：依赖一直在坏，
        # 要动的是长期问题，不是这一次的变化。把它读成上升，等于让运营每个周期
        # 都去开一次同一个事故。另一个类别一起摆着，否则份额恒为百分之百，读不出持平。
        self.fail_in_period('OperationalError: db gone', count=15, day_offset=40, hour=2)
        self.fail_in_period('IntegrityError: null', count=15, day_offset=40, hour=3)
        self.fail_in_period('OperationalError: db gone', count=15, day_offset=2, hour=2)
        self.fail_in_period('IntegrityError: null', count=15, day_offset=2, hour=3)

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        self.assertEqual(external['current_count'], 15)
        self.assertEqual(external['previous_count'], 15)
        self.assertEqual(external['delta'], 0)
        self.assertEqual(external['delta_display'], '+0')
        self.assertEqual(external['direction'], 'flat')
        self.assertEqual(external['tone'], 'flat')
        # 占比也没动：份额口径下「持平」说的是「它在失败里的分量没变」，不只是次数没变。
        self.assertEqual(external['current_share'], 50.0)
        self.assertEqual(external['previous_share'], 50.0)
        self.assertFalse(report['has_direction'])
        self.assertIn('都没有明显变化', report['summary'])

    def test_a_falling_cause_is_a_cause_recovering(self):
        self.fail_in_period('OperationalError: db gone', count=12, day_offset=40, hour=2)
        self.fail_in_period('IntegrityError: null', count=8, day_offset=40, hour=3)
        self.fail_in_period('OperationalError: db gone', count=2, day_offset=2, hour=2)
        self.fail_in_period('IntegrityError: null', count=18, day_offset=2, hour=3)

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        self.assertEqual(external['direction'], 'falling')
        self.assertEqual(external['delta_display'], '-10')
        # 失败次数下降是好消息，但也不能被读成"这个类别已经没问题了"。
        self.assertEqual(external['tone'], 'good')
        self.assertEqual(DIRECTION_TONES['falling'], 'good')

    def test_a_change_smaller_than_the_threshold_is_flat(self):
        # 阈值不按搜索趋势、调度趋势的百分点：那是个只对「本身就是百分比的数字」有意义的
        # 阈值，而失败次数是原始计数，没有那样的尺度。这里用份额，因为周期自己的失败
        # 总数是一个计数唯一有的尺度——两次对一次是翻倍，二十对十九是噪声，没有一个
        # 固定的失败次数能把这两件事分开。
        self.fail_in_period('OperationalError: db gone', count=9, day_offset=40, hour=2)
        self.fail_in_period('IntegrityError: null', count=11, day_offset=40, hour=3)
        self.fail_in_period('OperationalError: db gone', count=12, day_offset=2, hour=2)
        self.fail_in_period('IntegrityError: null', count=8, day_offset=2, hour=3)

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        self.assertEqual(external['delta'], 3)
        self.assertEqual(external['direction'], 'flat')

    def test_the_direction_vocabulary_matches_the_other_cross_period_panels(self):
        self.assertEqual(
            TREND_DIRECTIONS,
            ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient'),
        )
        self.assertEqual(TREND_LABELS['rising'], '较上期上升')
        self.assertEqual(TREND_LABELS['insufficient'], '样本不足')

    # -- what the comparison must never do ---------------------------------

    def test_a_cause_below_the_floor_is_reported_but_not_compared(self):
        # 失败比运行稀少，所以门槛比调度趋势低，但存在的理由一样：一次失败不是一种形态，
        # 把它叫上升是在教读者把算术当成证据。数字照报，方向撤回。
        self.fail_in_period('CommandError: bad args', count=MIN_CAUSE_RUNS, day_offset=40, hour=2)
        self.fail_in_period('CommandError: bad args', count=1, day_offset=2, hour=2)
        self.fail_in_period('IntegrityError: null', count=4, day_offset=40, hour=3)
        self.fail_in_period('IntegrityError: null', count=6, day_offset=2, hour=3)

        report = self.report()

        config = self.cause_of(report, CAUSE_CONFIG)
        self.assertEqual(config['current_count'], 1)
        self.assertEqual(config['previous_count'], MIN_CAUSE_RUNS)
        self.assertEqual(config['delta'], 1 - MIN_CAUSE_RUNS)
        self.assertEqual(config['direction'], 'insufficient')
        self.assertEqual(config['direction_label'], TREND_LABELS['insufficient'])
        # 方向撤回不代表行消失：读者仍然看得见这两个数字，只是不被告知这就是趋势。
        self.assertIsNotNone(config)
        self.assertNotIn('配置与参数', report['summary'])

    def test_each_period_has_its_own_floor(self):
        # 门槛是每个类别每个周期各自判的：一边够了、另一边不够，仍然没有可比性。
        self.fail_in_period('IntegrityError: null', count=MIN_CAUSE_RUNS, day_offset=40, hour=2)
        self.fail_in_period('IntegrityError: null', count=MIN_CAUSE_RUNS - 1, day_offset=2, hour=2)
        self.fail_in_period('OperationalError: db gone', count=5, day_offset=40, hour=3)
        self.fail_in_period('OperationalError: db gone', count=5, day_offset=2, hour=3)

        report = self.report()

        data = self.cause_of(report, CAUSE_DATA)
        self.assertEqual(data['current_count'], MIN_CAUSE_RUNS - 1)
        self.assertEqual(data['previous_count'], MIN_CAUSE_RUNS)
        self.assertEqual(data['direction'], 'insufficient')

    def test_a_period_with_too_few_failures_is_not_compared_at_all(self):
        # 方向是按份额算的，所以分母得够厚：整个周期只有一次失败时，某个类别就占着
        # 百分之百，这样的份额一步就能跳很远，读不出任何东西。数字照报，方向撤回。
        self.fail_in_period('IntegrityError: null', count=4, day_offset=40, hour=3)
        self.fail_in_period('IntegrityError: null', count=1, day_offset=2, hour=3)

        report = self.report()

        data = self.cause_of(report, CAUSE_DATA)
        self.assertEqual(data['current_count'], 1)
        self.assertEqual(data['previous_count'], 4)
        self.assertEqual(data['direction'], 'insufficient')
        self.assertLess(report['total_failed'], MIN_PERIOD_FAILURES)

    def test_a_cause_that_exists_in_only_one_period_has_no_delta(self):
        # 只有一边有数字时没有可减的另一边，伪造一个「+15」就是在没人测过的数字上做算术。
        self.fail_in_period('CommandError: no settings', count=MIN_CAUSE_RUNS, day_offset=2, hour=2)

        report = self.report()

        config = self.cause_of(report, CAUSE_CONFIG)
        self.assertEqual(config['current_count'], MIN_CAUSE_RUNS)
        self.assertIsNone(config['previous_count'])
        self.assertIsNone(config['delta'])
        self.assertEqual(config['delta_display'], '—')
        self.assertEqual(config['direction'], 'new')
        self.assertEqual(config['direction_label'], TREND_LABELS['new'])
        self.assertEqual(config['previous_display'], '—')

    def test_a_cause_that_disappeared_is_gone_not_zero(self):
        # 「本期已消失」不是「本期为零次」：一个没有再发生的类别和一次都没发生过的类别，
        # 在这张表上要说不同的话。
        self.fail_in_period('CommandError: no settings', count=5, day_offset=40, hour=2)

        report = self.report()

        config = self.cause_of(report, CAUSE_CONFIG)
        self.assertEqual(config['current_count'], None)
        self.assertEqual(config['previous_count'], 5)
        self.assertEqual(config['delta'], None)
        self.assertEqual(config['delta_display'], '—')
        self.assertEqual(config['direction'], 'gone')
        self.assertEqual(config['direction_label'], TREND_LABELS['gone'])

    def test_rows_are_ordered_with_the_worst_direction_first(self):
        # 方向是读者来看的东西，所以带方向的行排在前面，最坏的排最前：把上升的类别埋在
        # 一堆只是存在过的行下面，就遮住了这块面板唯一要报告的那件事。
        self.fail_in_period('OperationalError: db gone', count=2, day_offset=40, hour=2)
        self.fail_in_period('OperationalError: db gone', count=15, day_offset=2, hour=2)
        self.fail_in_period('IntegrityError: null', count=8, day_offset=40, hour=3)
        self.fail_in_period('IntegrityError: null', count=8, day_offset=2, hour=3)
        self.fail_in_period('CommandError: no settings', count=4, day_offset=2, hour=4)
        self.fail_in_period('今天没有到期订单', count=4, day_offset=2, hour=5)

        report = self.report()

        self.assertEqual(
            [row['cause'] for row in report['causes']],
            [CAUSE_EXTERNAL, CAUSE_DATA, CAUSE_CONFIG, CAUSE_UNKNOWN],
        )
        # 带方向的行排在前面，最坏的排最前；「本期新出现」和「样本不足」排在最后，
        # 它们不是发现，只是还没法比。
        self.assertEqual(report['causes'][0]['direction'], 'rising')
        self.assertEqual(report['causes'][3]['direction'], 'new')

    def test_successful_runs_are_never_counted_as_failures(self):
        # 一次成功的运行不是「零次失败」：把成功进行算进来，会让每个类别都凭空多出分母。
        self.fail_in_period('OperationalError: db gone', count=3, day_offset=40, hour=2)
        self.fail_in_period('IntegrityError: null', count=4, day_offset=40, hour=3)
        for index in range(10):
            self.make_run(
                'expire_items',
                started_at=self.local_moment(2, 2, index),
                duration_ms=1000,
            )

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        self.assertIsNotNone(external)
        self.assertEqual(external['current_count'], None)
        self.assertEqual(external['previous_count'], 3)
        self.assertEqual(report['total_failed'], 0)
        self.assertEqual(report['previous_total_failed'], 7)

    def test_a_cause_carries_the_commands_and_the_newest_note(self):
        # 类别要能回答「是哪个命令在驱动它」，否则读者还得再回台账里翻一遍。
        for index in range(4):
            self.make_run(
                'expire_items', status='failed',
                started_at=self.local_moment(40, 2, index),
                note='OperationalError: db gone',
            )
        for index in range(3):
            self.make_run(
                'send_operations_digest', status='failed',
                started_at=self.local_moment(40, 2, index + 5),
                note='OperationalError: db gone again',
            )
        for index in range(6):
            self.make_run(
                'send_operations_digest', status='failed',
                started_at=self.local_moment(2, 2, index),
                note='OperationalError: db gone third',
            )
        for index in range(4):
            self.make_run(
                'expire_items', status='failed',
                started_at=self.local_moment(2, 2, index + 10),
                note='OperationalError: db gone fourth',
            )

        report = self.report()

        external = self.cause_of(report, CAUSE_EXTERNAL)
        # 两个命令在两期里都失败过，所以两个都要数到：只数一边会让「哪个命令在驱动
        # 这个类别」这个问题的答案随周期漂移。
        self.assertEqual(external['command_count'], 2)
        self.assertEqual(external['commands'][0], {'name': 'send_operations_digest', 'count': 6})
        self.assertEqual(external['current_count'], 10)
        self.assertEqual(external['previous_count'], 7)
        # 最近一次备注取两期里最新的那一条，读者点进去看到的就是模块说的这一条。
        self.assertEqual(external['last_note'], 'OperationalError: db gone fourth')

    def test_dry_runs_are_excluded_for_the_reason_the_health_module_excludes(self):
        # 一次什么都没发的预演，既不能为调度作保，它的失败也测不出什么关于调度的东西。
        self.fail_in_period('OperationalError: db gone', count=4, day_offset=40, hour=2)
        self.fail_in_period('dry-run', count=4, day_offset=2, hour=2)

        report = self.report()

        self.assertIsNone(self.cause_of(report, CAUSE_UNKNOWN))
        self.assertEqual(report['total_failed'], 0)
        self.assertEqual(report['previous_total_failed'], 4)

    def test_the_empty_history_reports_rather_than_raising(self):
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['causes'], [])
        self.assertEqual(report['total_failed'], 0)
        self.assertIn('两个周期内都没有失败', report['summary'])

    def test_the_module_never_writes_back_into_the_ledger_it_reads(self):
        # 只读台账：不重新分类、不改类别表、不重跑失败的命令，也不把统计写回台账。
        # 一个能改写台账的统计，会让台账变成记录被它自己抠出来的形状的地方，
        # 后面再读的人就分不清哪些是运行、哪些是统计的痕迹。
        self.fail_in_period('OperationalError: db gone', count=5, day_offset=40, hour=2)
        self.fail_in_period('OperationalError: db gone', count=5, day_offset=2, hour=2)
        before = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'status', 'duration_ms', 'note')
        )

        self.report()

        after = list(
            TaskRun.objects.order_by('pk').values_list('pk', 'status', 'duration_ms', 'note')
        )
        self.assertEqual(before, after)
        # 摘要可以谈失败次数的变化，因为那就是它在报告的东西；但它不能谈「重试」或「重跑」，
        # 那属于改写而不是报告。
        self.assertIn('失败次数', self.report()['summary'])
        for forbidden in ('自动重试', '重新运行', '已重跑', '改为'):
            self.assertNotIn(forbidden, self.report()['summary'])

    def test_unclassified_failures_are_a_row_like_any_other(self):
        # 未归类的失败也占一行：把它藏起来，「本期为什么变差」就少了一块可读的证据。
        self.fail_in_period('今天没有到期订单', count=4, day_offset=40, hour=2)
        self.fail_in_period('连接被重置', count=4, day_offset=2, hour=2)
        self.fail_in_period('OperationalError: db gone', count=6, day_offset=40, hour=3)
        self.fail_in_period('OperationalError: db gone', count=6, day_offset=2, hour=3)

        report = self.report()

        unknown = self.cause_of(report, CAUSE_UNKNOWN)
        self.assertEqual(unknown['current_count'], 4)
        self.assertEqual(unknown['previous_count'], 4)
        self.assertEqual(unknown['direction'], 'flat')
        self.assertEqual(unknown['label'], '未能归类')


class DashboardIntegrationTests(TestCase):
    """The panel must actually render, and the export must actually carry it."""

    def setUp(self):
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)
        self.user = User.objects.create_user(
            username='cause-trend-staff', password='safe-password-123', is_staff=True,
        )

    def local_moment(self, day_offset, hour, minute=0):
        local_date = self.today - timedelta(days=day_offset)
        moment = timezone.make_aware(datetime.combine(local_date, time(hour=hour, minute=minute)))
        upper = timezone.make_aware(datetime.combine(local_date, time(hour=23, minute=59)))
        if upper > self.now:
            return self.now - timedelta(minutes=1)
        return moment

    def make_run(self, name, *, status='succeeded', started_at=None, duration_ms=120, note=''):
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

    def test_dashboard_renders_the_cause_trend_panel(self):
        # 面板要能渲染出来：模块接上去而模板没接，看板上就少一整块。
        # 上一周期里要有一个对照类别，否则上一周期只有一次失败、周期总数过不了门槛，
        # 面板会对一个真实的变化说「样本不足」——那是模块在回避自己该回答的问题。
        for index in range(2):
            self.make_run(
                'expire_items', status='failed',
                started_at=self.local_moment(40, 2, index), note='OperationalError: db gone',
            )
        for index in range(3):
            self.make_run(
                'notify_due', status='failed',
                started_at=self.local_moment(40, 3, index), note='ValueError: bad payload',
            )
        for index in range(6):
            self.make_run(
                'expire_items', status='failed',
                started_at=self.local_moment(2, 2, index), note='OperationalError: db gone',
            )
        self.client.force_login(self.user)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '失败原因的跨周期变化')
        trend = response.context['task_run_failure_cause_trend']
        self.assertEqual(trend['total_failed'], 6)
        self.assertEqual(trend['previous_total_failed'], 5)
        row = next(item for item in trend['causes'] if item['cause'] == CAUSE_EXTERNAL)
        self.assertEqual(row['direction'], 'rising')
        self.assertEqual(row['tone'], 'bad')

    def test_the_export_carries_the_cause_trend_section(self):
        # 导出的口径要和面板一致：导出的表要和面板对得上，否则下载回去的那张
        # 表会被读成另一个项目的数据。
        for index in range(2):
            self.make_run(
                'expire_items', status='failed',
                started_at=self.local_moment(40, 2, index), note='OperationalError: db gone',
            )
        for index in range(3):
            self.make_run(
                'notify_due', status='failed',
                started_at=self.local_moment(40, 3, index), note='ValueError: bad payload',
            )
        for index in range(6):
            self.make_run(
                'expire_items', status='failed',
                started_at=self.local_moment(2, 2, index), note='OperationalError: db gone',
            )
        self.client.force_login(self.user)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('失败原因的跨周期变化', text)
        self.assertIn('外部依赖', text)
        self.assertIn('较上期上升', text)

    def test_the_export_leaves_a_missing_previous_cause_blank(self):
        # 上一周期没有这个类别就留空，不填 0：填 0 会被读成「上一周期确实一次都没失败」，
        # 而「没失败」和「没有记录」在这张表上要能分开。
        for index in range(4):
            self.make_run(
                'expire_items', status='failed',
                started_at=self.local_moment(2, 2, index), note='CommandError: no settings',
            )
        self.client.force_login(self.user)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('上一周期失败次数', text)
        self.assertIn('本期新出现', text)