# -*- coding: utf-8 -*-
"""跟进任务的接手等待与当前停留，第一次被读。

这一轮量的不是解析函数，是三件更根本的事。

第一件是 updated_at 不能当状态变更时刻。一个 3 月 1 日建好、3 月 5 日被接手、
4 月 2 日结案的任务，updated_at 是 4 月 2 日，读出来是 32 天，而真实情况是
「等了 4 天才有人接手」。测试必须钉住这条边界：已经接手的任务，等待报的是上界，
并且从没接手过的任务被单独拆出来——那一组中间没有任何一次写入，它的等待是可读的。

第二件是两个口径不能混。口径一报等待，口径二报当前快照，它们之间没有算术关系，
不能被相加或相减。测试同时覆盖两个口径，并确认已闭环的任务照常参与停留统计。

第三件是空平台不报 0 天。一条任务都没有的时候，所有时长都不存在，不是零——0 天会被
读成「建了当天就有人接手」。未接手的任务清单也不按统计周期过滤：周期开始之前就
没人管的任务，恰恰是最不该被藏起来的一件。

本模块只报告：不改任务状态、不自动派单、不改截止时间、不新增任何提醒，也不按负责人
排名——负责人只在待处理清单里作为一列出现，用来找到人，不用来比较人。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .demand_task_aging import (
    HANDOVER_BUCKETS,
    MIN_BUCKET_SAMPLE,
    NOISE_DAYS,
    STALE_TASK_LIMIT,
    _bucket_label,
    _handover_wait_days,
    _has_handover,
    _residency_days,
    build_demand_task_aging,
)
from .models import CampusLocation, Category, DemandOpportunityTask, Item


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username, is_staff=False):
    return User.objects.create_user(
        username=username, password='safe-password-123', is_staff=is_staff,
    )


def make_task_base(staff, category, location, **kwargs):
    """建一条跟进任务，再把两个自动时间列写回夹具指定的时刻。

    created_at 是 auto_now_add、updated_at 是 auto_now，创建时传值会被忽略，所以建完
    之后再用 update 写回。否则所有任务都落在「现在」，等待和停留全都读成 0 天。
    """
    task = DemandOpportunityTask.objects.create(
        radar_key='aging-%d' % DemandOpportunityTask.objects.count(),
        title=kwargs.pop('title', '跟进主题'),
        category=category, location=location,
        level='medium', opportunity_score=10, search_count=2,
        demand_count=1, available_supply=1,
        created_by=staff, **kwargs
    )
    fields = {}
    if 'created_at' in kwargs:
        fields['created_at'] = kwargs['created_at']
    if 'updated_at' in kwargs:
        fields['updated_at'] = kwargs['updated_at']
    if fields:
        DemandOpportunityTask.objects.filter(pk=task.pk).update(**fields)
        task.refresh_from_db()
    return task


class AgingSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('aging-staff', is_staff=True)
        self.category = Category.objects.create(name='等待分类')
        self.location = CampusLocation.objects.create(name='等待地点')

    def make_task(self, **kwargs):
        return make_task_base(
            self.staff, self.category, self.location, **kwargs
        )

    def test_an_empty_period_reports_no_data_rather_than_zero_days(self):
        report = build_demand_task_aging(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['handover']['total'], 0)
        self.assertIsNone(report['handover']['handed_median_days'])
        self.assertIsNone(report['handover']['waiting_median_days'])
        self.assertIsNone(report['handover']['gap_days'])
        self.assertEqual(report['residency']['rows'][0]['median_days'], None)
        self.assertEqual(report['stale_rows']['rows'], [])
        self.assertIn('还没有建立需求跟进任务', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_demand_task_aging(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_a_task_created_before_the_window_is_excluded(self):
        self.make_task(created_at=self.now - timedelta(days=45))

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_a_task_created_in_the_future_is_excluded(self):
        self.make_task(created_at=self.now + timedelta(days=1))

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertFalse(report['has_data'])


class HandoverWaitTests(TestCase):
    """接手等待：没人接手的任务读的是「建出来到现在」，已接手的只报上界。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('handover-staff', is_staff=True)
        self.category = Category.objects.create(name='接手分类')
        self.location = CampusLocation.objects.create(name='接手地点')

    def make_task(self, **kwargs):
        return make_task_base(
            self.staff, self.category, self.location, **kwargs
        )

    def test_an_untouched_task_has_a_readable_wait(self):
        """没人碰过的任务，等待就是「建出来到现在」——这一段没有人推进过它。

        这一条钉的是本模块的口径一：没人接手过的任务走 now - created_at，不是
        updated_at - created_at。后者对没人碰过的任务恒等于 0，会把「等了 9 天」
        读成「当天就有人接手」——方向和真实情况正好相反。
        """
        task = self.make_task(
            created_at=self.now - timedelta(days=9),
            updated_at=self.now - timedelta(days=9),
        )

        self.assertTrue(_has_handover(task) is False)
        self.assertEqual(_handover_wait_days(task, self.now), 9)

    def test_a_touched_task_wait_is_an_upper_bound_not_the_real_wait(self):
        """3 月 1 日建好、3 月 5 日被接手、4 月 2 日又改了备注：updated_at 是 4 月 2 日。

        真实等待是 4 天，而库里读出来的是 32 天。这个数只能当上界用——测试钉住它报的
        是上界，并且真的接过手的那一组被单独拆出来。
        """
        task = self.make_task(
            status='in_progress', assigned_to=self.staff,
            created_at=self.now - timedelta(days=32),
            updated_at=self.now,
        )

        self.assertTrue(_has_handover(task))
        self.assertEqual(_handover_wait_days(task, self.now), 32)

    def test_a_note_edit_on_an_unclaimed_task_still_reads_as_a_clean_wait(self):
        """建了 5 天、昨天改了一句备注、状态还是 todo 的任务，在库里和 5 天没人碰过的无法区分。

        updated_at 被任何写入推进，这是口径一的天花板。测试钉住这一组报的是可读等待
        （now - created_at），而上界只会更长。
        """
        task = self.make_task(
            status='todo', created_at=self.now - timedelta(days=5),
            updated_at=self.now - timedelta(days=1),
        )

        self.assertEqual(_handover_wait_days(task, self.now), 5)

    def test_handover_is_detected_from_the_assignee_too(self):
        """后台可以只派人不改状态，那一笔也算接手。"""
        task = self.make_task(status='todo', assigned_to=self.staff)

        self.assertTrue(_has_handover(task))

    def test_never_handed_tasks_are_counted_separately(self):
        self.make_task(created_at=self.now - timedelta(days=2),
                       updated_at=self.now - timedelta(days=2))
        self.make_task(status='in_progress', assigned_to=self.staff,
                       created_at=self.now - timedelta(days=2),
                       updated_at=self.now - timedelta(days=1))

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertEqual(report['handover']['handed_count'], 1)
        self.assertEqual(report['handover']['waiting_count'], 1)
        self.assertEqual(report['handover']['waiting_median_days'], 2.0)
        self.assertEqual(report['handover']['handed_median_days'], 1.0)

    def test_wait_buckets_are_filled_by_whole_days(self):
        self.make_task(created_at=self.now - timedelta(days=20),
                       updated_at=self.now - timedelta(days=20))

        report = build_demand_task_aging(days=30, now=self.now)
        labels = [row['label'] for row in report['handover']['waiting_bucket_rows']]
        self.assertEqual(labels, [label for _limit, label in HANDOVER_BUCKETS])
        filled = [row for row in report['handover']['waiting_bucket_rows'] if row['count']]
        self.assertEqual([row['label'] for row in filled], ['8-30 天'])

    def test_a_backwards_clock_reads_as_zero_not_negative(self):
        """updated_at 早于 created_at 是数据问题，不是「提前被人接手」。"""
        task = self.make_task(
            status='todo',
            created_at=self.now,
            updated_at=self.now - timedelta(days=1),
        )

        self.assertEqual(_handover_wait_days(task, self.now), 0)


class ResidencyTests(TestCase):
    """停留时长只报当前快照，而这一条上 updated_at 恰好就是那个正确的列。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('residency-staff', is_staff=True)
        self.category = Category.objects.create(name='停留分类')
        self.location = CampusLocation.objects.create(name='停留地点')

    def make_task(self, **kwargs):
        return make_task_base(
            self.staff, self.category, self.location, **kwargs
        )

    def test_residency_measures_time_since_the_last_write(self):
        task = self.make_task(status='in_progress', updated_at=self.now - timedelta(days=6))

        self.assertEqual(_residency_days(task, self.now), 6)

    def test_every_status_gets_its_own_row(self):
        self.make_task(status='todo', updated_at=self.now - timedelta(days=3))
        self.make_task(status='in_progress', updated_at=self.now - timedelta(days=10))
        self.make_task(status='completed', updated_at=self.now - timedelta(days=1))

        report = build_demand_task_aging(days=30, now=self.now)
        rows = {row['key']: row for row in report['residency']['rows']}
        self.assertEqual(sorted(rows), ['completed', 'ignored', 'in_progress', 'todo'])
        self.assertEqual(rows['todo']['median_days'], 3.0)
        self.assertEqual(rows['in_progress']['median_days'], 10.0)
        self.assertEqual(rows['completed']['median_days'], 1.0)

    def test_closed_tasks_still_count_in_residency(self):
        """已闭环的任务照常参与停留统计，它们的停留回答的是「结案之后还留了多久」。"""
        self.make_task(status='ignored', updated_at=self.now - timedelta(days=40))

        report = build_demand_task_aging(days=30, now=self.now)
        rows = {row['key']: row for row in report['residency']['rows']}
        self.assertEqual(rows['ignored']['task_count'], 1)
        self.assertEqual(rows['ignored']['median_days'], 40.0)

    def test_residency_is_not_filtered_by_the_period(self):
        """停留描述的是「现在」：周期开始之前就没人管的任务也要算进来。"""
        self.make_task(created_at=self.now - timedelta(days=200),
                       updated_at=self.now - timedelta(days=200))

        report = build_demand_task_aging(days=30, now=self.now)
        rows = {row['key']: row for row in report['residency']['rows']}
        self.assertEqual(rows['todo']['task_count'], 1)
        self.assertEqual(rows['todo']['max_days'], 200)

    def test_a_thin_status_row_is_flagged_as_small_sample(self):
        self.make_task(status='todo', updated_at=self.now)

        report = build_demand_task_aging(days=30, now=self.now)
        rows = {row['key']: row for row in report['residency']['rows']}
        self.assertTrue(rows['todo']['is_small_sample'])
        self.assertEqual(report['residency']['min_bucket_sample'], MIN_BUCKET_SAMPLE)


class StaleRowTests(TestCase):
    """最久没有活动的未闭环任务：不按周期过滤，也不按负责人排名。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('stale-staff', is_staff=True)
        self.category = Category.objects.create(name='清单分类')
        self.location = CampusLocation.objects.create(name='清单地点')

    def make_task(self, **kwargs):
        return make_task_base(
            self.staff, self.category, self.location, **kwargs
        )

    def test_only_open_tasks_are_listed(self):
        """已结案的任务不进清单：它们已经不是待办。接手过的仍进清单，见下一条。"""
        self.make_task(created_at=self.now - timedelta(days=30),
                       updated_at=self.now - timedelta(days=30), title='没人管')
        self.make_task(status='in_progress', assigned_to=self.staff,
                       created_at=self.now - timedelta(days=30),
                       updated_at=self.now - timedelta(days=30), title='有人管')
        self.make_task(status='completed', created_at=self.now - timedelta(days=30),
                       updated_at=self.now - timedelta(days=30), title='已结案')
        self.make_task(status='ignored', created_at=self.now - timedelta(days=30),
                       updated_at=self.now - timedelta(days=30), title='已忽略')

        report = build_demand_task_aging(days=30, now=self.now)
        titles = [row['title'] for row in report['stale_rows']['rows']]
        self.assertEqual(sorted(titles), ['有人管', '没人管'])

    def test_a_handed_task_that_stopped_moving_is_still_listed(self):
        """接手之后 40 天没有任何写入，正是最该被点开看看的一件。"""
        self.make_task(status='in_progress', assigned_to=self.staff,
                       created_at=self.now - timedelta(days=40),
                       updated_at=self.now - timedelta(days=40), title='接手后停住')

        report = build_demand_task_aging(days=30, now=self.now)
        row = report['stale_rows']['rows'][0]
        self.assertEqual(row['title'], '接手后停住')
        self.assertTrue(row['has_handover'])
        self.assertEqual(row['assigned_to'], 'stale-staff')

    def test_the_stalest_task_comes_first(self):
        self.make_task(created_at=self.now - timedelta(days=5),
                       updated_at=self.now - timedelta(days=5), title='较新')
        self.make_task(created_at=self.now - timedelta(days=40),
                       updated_at=self.now - timedelta(days=40), title='最久')

        report = build_demand_task_aging(days=30, now=self.now)
        rows = report['stale_rows']['rows']
        self.assertEqual(rows[0]['title'], '最久')
        self.assertEqual(rows[0]['residency_days'], 40)
        self.assertEqual(report['stale_rows']['max_residency_days'], 40)

    def test_a_stale_task_from_before_the_window_is_not_hidden(self):
        """周期开始之前就没人管的任务，恰恰是最不该被藏起来的一件。"""
        self.make_task(created_at=self.now - timedelta(days=120),
                       updated_at=self.now - timedelta(days=120))

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertEqual(report['stale_rows']['row_total'], 1)

    def test_the_list_carries_the_fields_needed_to_find_a_person(self):
        """负责人只作为一列出现，用来找到人，不用来比较人。"""
        self.make_task(status='todo', assigned_to=self.staff, note='已问过图书馆',
                       due_at=self.now + timedelta(days=5),
                       created_at=self.now - timedelta(days=12),
                       updated_at=self.now - timedelta(days=12))

        report = build_demand_task_aging(days=30, now=self.now)
        row = report['stale_rows']['rows'][0]
        self.assertEqual(row['assigned_to'], 'stale-staff')
        self.assertTrue(row['has_due'])
        self.assertEqual(row['note_length'], 6)

    def test_the_list_reports_the_full_total_while_capping_rows(self):
        """截断不等于「一共就这么多」——把 24 条报成 20 条会让人以为看全了。"""
        for index in range(STALE_TASK_LIMIT + 4):
            self.make_task(
                title='清单 %02d' % index,
                created_at=self.now - timedelta(days=30 - index),
                updated_at=self.now - timedelta(days=30 - index),
            )

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertEqual(report['stale_rows']['row_total'], STALE_TASK_LIMIT + 4)
        self.assertEqual(len(report['stale_rows']['rows']), STALE_TASK_LIMIT)
        self.assertEqual(report['stale_rows']['row_limit'], STALE_TASK_LIMIT)


class NoiseTests(TestCase):
    """样本薄的时候只报数字、不给方向。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('noise-staff', is_staff=True)
        self.category = Category.objects.create(name='噪声分类')
        self.location = CampusLocation.objects.create(name='噪声地点')

    def make_task(self, status, wait_days):
        """建一条等待 wait_days 天的任务，两组用同一个尺度。

        没接手的那一组：updated_at 停在 created_at，等待是 now - created_at。
        已经接手的那一组：等待取上界 updated_at - created_at，这里让 created_at 落在
        wait_days 天之前、updated_at 落在昨天，于是上界正好也是 wait_days。

        两组共用同一个 wait_days 参数，测试才能对「相差几天」下断言——否则一个参数在两
        组里是两个尺度，噪声线的位置就说不清了。
        """
        if status == 'todo':
            created_at = self.now - timedelta(days=wait_days)
            updated_at = created_at
        else:
            created_at = self.now - timedelta(days=wait_days + 1)
            updated_at = self.now - timedelta(days=1)
        return make_task_base(
            self.staff, self.category, self.location,
            status=status, assigned_to=self.staff if status != 'todo' else None,
            created_at=created_at, updated_at=updated_at,
        )
    def test_a_thin_gap_gets_no_direction(self):
        for _index in range(MIN_BUCKET_SAMPLE - 1):
            self.make_task('todo', 1)
        for _index in range(MIN_BUCKET_SAMPLE - 1):
            self.make_task('in_progress', 4)

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertFalse(report['handover']['is_measurable'])
        self.assertFalse(report['handover']['gap_is_meaningful'])
        self.assertIn('从建出来就没被人碰过', report['summary'])
        self.assertNotIn('读不出方向', report['summary'])

    def test_a_gap_inside_the_noise_band_gets_no_direction(self):
        for _index in range(MIN_BUCKET_SAMPLE):
            self.make_task('todo', 5)
        for _index in range(MIN_BUCKET_SAMPLE):
            self.make_task('in_progress', 5 + NOISE_DAYS - 1)

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertTrue(report['handover']['is_measurable'])
        self.assertFalse(report['handover']['gap_is_meaningful'])
        self.assertIn('读不出方向', report['summary'])

    def test_a_gap_outside_the_noise_band_gets_a_direction(self):
        for _index in range(MIN_BUCKET_SAMPLE):
            self.make_task('todo', 2)
        for _index in range(MIN_BUCKET_SAMPLE):
            self.make_task('in_progress', 12)

        report = build_demand_task_aging(days=30, now=self.now)
        self.assertTrue(report['handover']['is_measurable'])
        self.assertTrue(report['handover']['gap_is_meaningful'])
        self.assertIn('相差', report['summary'])


class PanelAndExportTests(TestCase):
    """看板要真的把这套读法画出来、导出去。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('aging-panel-staff', is_staff=True)
        self.category = Category.objects.create(name='看板等待分类')
        self.location = CampusLocation.objects.create(name='看板等待地点')
        self.client.force_login(self.staff)

    def make(self, **kwargs):
        return make_task_base(
            self.staff, self.category, self.location, **kwargs
        )

    def fetch(self):
        html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        text = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        ).content.decode('utf-8-sig')
        return html, text

    def test_the_panel_names_both_readings_and_their_caveat(self):
        # 一条没人碰过的、一条已接手的，两组都要在面板上有位置。
        self.make(
            status='todo', title='没人管的主题',
            created_at=self.now - timedelta(days=9),
        )
        self.make(
            status='in_progress', assigned_to=self.staff, title='有人管的主题',
            created_at=self.now - timedelta(days=10),
            updated_at=self.now - timedelta(days=2),
        )

        html, _ = self.fetch()
        self.assertIn('demand-task-aging-panel', html)
        self.assertIn('跟进任务的接手等待与停留', html)
        self.assertIn('接手等待怎么分布的', html)
        self.assertIn('每个状态现在停了多久', html)
        self.assertIn('最久没有活动的未闭环任务', html)
        # 两支两个式子必须写在面板上，不能只写在模块里。
        self.assertIn('没人接手过', html)
        self.assertIn('已经有人接手', html)
        self.assertIn('这一条上', html)

    def test_the_list_separates_never_handed_from_stopped_after_handover(self):
        # 两条都落在统计周期内，否则面板会走空态，清单根本不会被画出来。
        # 没人碰过的那条要把 updated_at 写回 created_at——它在库里的 updated_at
        # 本来就停在 created_at，不写回就成了「刚刚动过」，停留读成 0 天。
        self.make(
            status='todo', title='从建出来就没被人碰过',
            created_at=self.now - timedelta(days=20),
            updated_at=self.now - timedelta(days=20),
        )
        self.make(
            status='in_progress', assigned_to=self.staff, title='接手之后停住了',
            created_at=self.now - timedelta(days=21),
            updated_at=self.now - timedelta(days=3),
        )

        html, _ = self.fetch()
        # 已接手的那条有负责人，直接显示用户名；「接手后停住」只在没有负责人的
        # 已接手任务上出现（后台只改了状态、没派人）。所以这里断言的是用户名。
        self.assertIn('没人管', html)
        self.assertIn(self.staff.username, html)

    def test_the_export_names_the_section_and_its_caveats(self):
        self.make(
            status='todo',
            created_at=self.now - timedelta(days=9),
        )

        _, text = self.fetch()
        self.assertIn('跟进任务的接手等待与停留', text)
        self.assertIn('有人接手', text)
        self.assertIn('还没人接手', text)
        self.assertIn('接手等待分档', text)
        self.assertIn('当前状态', text)
        self.assertIn('最久没有活动的未闭环任务', text)
        self.assertIn('不按统计周期过滤', text)
        self.assertIn('不能相加或相减', text)

    def test_the_dashboard_context_carries_the_report(self):
        self.make(status='todo', created_at=self.now - timedelta(days=9))

        html, _ = self.fetch()
        self.assertIn('demand-task-aging-panel', html)
        self.assertIn('建任务的时刻', html)

    def test_an_empty_period_says_so_instead_of_drawing_zeroes(self):
        html, text = self.fetch()
        self.assertIn('demand-task-aging-panel', html)
        self.assertIn('还没有建立需求跟进任务', html)
        self.assertNotIn('接手等待怎么分布的', html)
        self.assertNotIn('当前状态停留', text)
