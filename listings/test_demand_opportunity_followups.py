# -*- coding: utf-8 -*-
"""需求跟进任务的截止时间与备注，从上线起第一次被写、被读、被统计。

这两个字段从需求雷达上线那天起就在模型里，而雷达旁边那三个按钮从来不碰它们。
这些测试要钉住的不是解析函数，而是三件更根本的事。

第一件是填写率必须排在最前面。一个没人填的字段不该被分析，该被撤掉；先报逾期
再报填写率，会把一个死字段讲成一份热闹的清单。第二件是"任务建了但没人给期限"
必须单独报出来，而不是混进逾期里——没有期限的任务无从逾期，把它算进逾期率的分母
会让逾期率凭空变小，而它恰恰是跟进流程有没有在跑的最硬信号。第三件是两组完成率
的对比必须守住噪声线：几十条任务上几个百分点的差距和抛硬币差不多，样本不足时
只报数字、不给方向，一个把噪声讲成结论的统计比没有统计更糟。

备注的边界同样要钉住：它是跟进人写给自己的内部备忘，不是用户声音。只数字数、
只报有多少人写，不切词、不聚类、不推断语义，也不公开任何一条原文。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .demand_radar import build_demand_radar
from .models import (
    CampusLocation, Category, DemandOpportunityTask, Item, SearchQuery,
)
from .demand_opportunity_followups import (
    DUE_SOON_DAYS,
    MAX_REASONABLE_DUE_DAYS,
    MIN_COMPARISON_TASKS,
    MIN_NOTE_LENGTH,
    NOISE_POINTS,
    _days_until_due,
    _overdue_bucket_label,
    _overdue_days,
    build_demand_opportunity_followups,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username, is_staff=False):
    return User.objects.create_user(
        username=username, password='safe-password-123', is_staff=is_staff,
    )


class FollowupsSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('followup-staff', is_staff=True)
        self.category = Category.objects.create(name='跟进分类')
        self.location = CampusLocation.objects.create(name='跟进地点')
        self.item = Item.objects.create(
            title='跟进商品', description='用于跟进统计', price=Decimal('30.00'),
            status='available', seller=self.staff, category=self.category,
            location=self.location,
        )

    def make_task(self, status='todo', due_at=None, note='', created_at=None, title='跟进主题'):
        task = DemandOpportunityTask.objects.create(
            radar_key='%s-%d' % (title, DemandOpportunityTask.objects.count()),
            title=title, category=self.category, location=self.location,
            level='medium', opportunity_score=10, search_count=2,
            demand_count=1, available_supply=1,
            status=status, created_by=self.staff, due_at=due_at, note=note,
        )
        if created_at is not None:
            DemandOpportunityTask.objects.filter(pk=task.pk).update(created_at=created_at)
        return task

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_demand_opportunity_followups(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['fill_stats']['total'], 0)
        self.assertEqual(report['fill_stats']['due_fill_rate'], 0)
        self.assertEqual(report['overdue_stats']['overdue_rows'], [])
        self.assertIsNone(report['due_contrast']['gap_points'])
        self.assertIn('还没有建立需求跟进任务', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_demand_opportunity_followups(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_a_task_created_before_the_window_is_excluded(self):
        self.make_task(created_at=self.now - timedelta(days=45))

        report = build_demand_opportunity_followups(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_a_task_created_in_the_future_is_excluded(self):
        self.make_task(created_at=self.now + timedelta(days=1))

        report = build_demand_opportunity_followups(days=30, now=self.now)
        self.assertFalse(report['has_data'])


class FillStatsTests(TestCase):
    """填写率优先：一个没人填的字段不该被分析，该被撤掉。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('fill-staff', is_staff=True)
        self.category = Category.objects.create(name='填写分类')
        self.location = CampusLocation.objects.create(name='填写地点')

    def make(self, due_at=None, note=''):
        task = DemandOpportunityTask.objects.create(
            radar_key='fill-%d' % DemandOpportunityTask.objects.count(),
            title='填写主题', category=self.category, location=self.location,
            level='medium', opportunity_score=5, created_by=self.staff,
            due_at=due_at, note=note,
        )
        DemandOpportunityTask.objects.filter(pk=task.pk).update(
            created_at=self.now - timedelta(days=1),
        )
        return task

    def test_due_and_note_fill_rates_are_reported_before_anything_else(self):
        self.make(due_at=self.now + timedelta(days=3), note='已联系两位卖家')
        self.make(due_at=None, note='')
        self.make(due_at=None, note='')
        self.make(due_at=None, note='')

        report = build_demand_opportunity_followups(days=30, now=self.now)
        stats = report['fill_stats']
        self.assertEqual(stats['total'], 4)
        self.assertEqual(stats['with_due_count'], 1)
        self.assertEqual(stats['due_fill_rate'], 25.0)
        self.assertEqual(stats['without_due_count'], 3)
        self.assertEqual(stats['with_note_count'], 1)
        self.assertEqual(stats['note_fill_rate'], 25.0)
        self.assertIn('设了截止时间', report['summary'])
        self.assertIn('25.0%', report['summary'])

    def test_a_placeholder_note_does_not_count_as_written(self):
        self.make(note='嗯')
        self.make(note='   ')
        self.make(note='')

        report = build_demand_opportunity_followups(days=30, now=self.now)
        self.assertEqual(report['fill_stats']['with_note_count'], 0)
        self.assertEqual(report['fill_stats']['note_fill_rate'], 0)
        self.assertEqual(report['fill_stats']['average_note_length'], 0)
        self.assertEqual(report['fill_stats']['max_note_length'], 0)

    def test_a_note_exactly_at_the_minimum_length_counts(self):
        self.make(note='一二三四')

        report = build_demand_opportunity_followups(days=30, now=self.now)
        self.assertEqual(report['fill_stats']['with_note_count'], 1)
        self.assertEqual(report['fill_stats']['max_note_length'], MIN_NOTE_LENGTH)

    def test_average_note_length_only_covers_notes_that_exist(self):
        self.make(note='一二三四五六')
        self.make(note='')

        report = build_demand_opportunity_followups(days=30, now=self.now)
        self.assertEqual(report['fill_stats']['average_note_length'], 6.0)
        self.assertEqual(report['fill_stats']['max_note_length'], 6)

    def test_a_distant_due_date_is_reported_separately_not_silently_accepted(self):
        self.make(due_at=self.now + timedelta(days=400))
        self.make(due_at=self.now + timedelta(days=10))

        report = build_demand_opportunity_followups(days=30, now=self.now)
        stats = report['fill_stats']
        self.assertEqual(stats['with_due_count'], 2)
        self.assertEqual(stats['distant_due_count'], 1)
        self.assertEqual(stats['distant_due_rate'], 50.0)
        self.assertIn(str(MAX_REASONABLE_DUE_DAYS), report['summary'])

    def test_a_due_date_before_creation_is_counted_but_not_called_distant(self):
        task = self.make()
        DemandOpportunityTask.objects.filter(pk=task.pk).update(
            due_at=self.now - timedelta(days=5),
            created_at=self.now - timedelta(days=1),
        )

        report = build_demand_opportunity_followups(days=30, now=self.now)
        self.assertEqual(report['fill_stats']['distant_due_count'], 0)


class OverdueStatsTests(TestCase):
    """逾期与临期：按天切档，无期限的任务不作分母。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('overdue-staff', is_staff=True)
        self.category = Category.objects.create(name='逾期分类')
        self.location = CampusLocation.objects.create(name='逾期地点')

    def make(self, status='todo', due_at=None, note='', title='逾期主题'):
        task = DemandOpportunityTask.objects.create(
            radar_key='overdue-%d' % DemandOpportunityTask.objects.count(),
            title=title, category=self.category, location=self.location,
            level='medium', opportunity_score=5, created_by=self.staff,
            status=status, due_at=due_at, note=note,
        )
        DemandOpportunityTask.objects.filter(pk=task.pk).update(
            created_at=self.now - timedelta(days=1),
        )
        return task

    def test_a_due_date_earlier_today_is_already_overdue_by_zero_days(self):
        task = self.make(due_at=self.now.replace(hour=8, minute=0, second=0))

        self.assertEqual(_overdue_days(task, self.now), 0)

    def test_a_due_date_later_today_is_not_overdue_yet(self):
        task = self.make(due_at=self.now.replace(hour=23, minute=0, second=0))

        self.assertIsNone(_overdue_days(task, self.now))

    def test_yesterday_evening_is_one_day_overdue_by_the_morning_after(self):
        task = self.make(due_at=self.now - timedelta(days=1) - timedelta(hours=9))

        self.assertEqual(_overdue_days(task, self.now), 1)

    def test_closed_tasks_are_left_out_of_the_overdue_list(self):
        self.make(status='completed', due_at=self.now - timedelta(days=5))
        self.make(status='ignored', due_at=self.now - timedelta(days=5))

        report = build_demand_opportunity_followups(days=30, now=self.now)
        stats = report['overdue_stats']
        self.assertEqual(stats['overdue_count'], 0)
        self.assertEqual(stats['overdue_rows'], [])
        self.assertEqual(stats['open_count'], 0)

    def test_a_task_without_a_due_date_is_never_in_the_denominator(self):
        self.make(due_at=None)
        self.make(due_at=None)
        self.make(due_at=self.now - timedelta(days=2))

        stats = build_demand_opportunity_followups(
            days=30, now=self.now,
        )['overdue_stats']
        self.assertEqual(stats['open_count'], 3)
        self.assertEqual(stats['with_due_open_count'], 1)
        # 逾期率的分母是"未闭环且设了期限"，不是全部未闭环：没有期限的任务
        # 无从逾期，把它算进分母会让逾期率凭空变小。
        self.assertEqual(stats['overdue_rate'], 100.0)

    def test_bucket_boundaries_split_by_day_not_by_hour(self):
        self.make(due_at=self.now - timedelta(hours=2), title='当天')
        self.make(due_at=self.now - timedelta(days=2), title='一至三天')
        self.make(due_at=self.now - timedelta(days=10), title='三天以上')

        rows = build_demand_opportunity_followups(
            days=30, now=self.now,
        )['overdue_stats']['bucket_rows']
        self.assertEqual([row['count'] for row in rows], [1, 1, 1])
        self.assertEqual([row['label'] for row in rows], ['当天', '1-3 天', '3 天以上'])

    def test_the_overdue_list_puts_the_most_overdue_task_first(self):
        late = self.make(due_at=self.now - timedelta(days=9), title='超了九天')
        early = self.make(due_at=self.now - timedelta(days=1), title='超了一天')

        rows = build_demand_opportunity_followups(
            days=30, now=self.now,
        )['overdue_stats']['overdue_rows']
        self.assertEqual([row['title'] for row in rows], [late.title, early.title])
        self.assertEqual([row['overdue_days'] for row in rows], [9, 1])
        self.assertEqual(rows[0]['assigned_to'], '')

    def test_an_unassigned_overdue_task_is_reported_rather_than_skipped(self):
        task = self.make(due_at=self.now - timedelta(days=3))

        row = build_demand_opportunity_followups(
            days=30, now=self.now,
        )['overdue_stats']['overdue_rows'][0]
        self.assertEqual(row['id'], task.id)
        self.assertEqual(row['status_label'], '待跟进')
        self.assertEqual(row['note_length'], 0)

    def test_a_task_due_inside_the_soon_window_is_listed_as_approaching(self):
        soon = self.make(due_at=self.now + timedelta(days=DUE_SOON_DAYS), title='快到期')
        self.make(due_at=self.now + timedelta(days=DUE_SOON_DAYS + 1), title='还早')

        stats = build_demand_opportunity_followups(days=30, now=self.now)['overdue_stats']
        self.assertEqual(stats['approaching_count'], 1)
        self.assertEqual(stats['approaching_rows'][0]['id'], soon.id)
        self.assertEqual(stats['approaching_rows'][0]['days_until_due'], DUE_SOON_DAYS)

    def test_a_task_without_a_due_date_has_no_days_until_due(self):
        self.make(due_at=None)

        stats = build_demand_opportunity_followups(days=30, now=self.now)['overdue_stats']
        self.assertIsNone(_days_until_due(DemandOpportunityTask.objects.get(), self.now))
        self.assertEqual(stats['approaching_rows'], [])

    def test_the_open_list_survives_a_period_that_ended_before_it_did(self):
        # 待处理清单不按统计周期过滤：周期开始之前就超期的那个任务，
        # 恰恰是最不该被藏起来的一件。
        task = self.make(due_at=self.now - timedelta(days=40), status='in_progress')
        DemandOpportunityTask.objects.filter(pk=task.pk).update(
            created_at=self.now - timedelta(days=45),
        )

        stats = build_demand_opportunity_followups(days=30, now=self.now)['overdue_stats']
        self.assertEqual([row['id'] for row in stats['overdue_rows']], [task.id])


class StatusRowsTests(TestCase):
    """状态分布要能被当作台账读：每个状态一行，空状态也是零而不是缺席。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('status-staff', is_staff=True)
        self.category = Category.objects.create(name='状态分类')
        self.location = CampusLocation.objects.create(name='状态地点')

    def make(self, status, due_at=None, note=''):
        return DemandOpportunityTask.objects.create(
            radar_key='status-%d' % DemandOpportunityTask.objects.count(),
            title='状态主题', category=self.category, location=self.location,
            level='medium', opportunity_score=5, created_by=self.staff,
            status=status, due_at=due_at, note=note,
        )

    def test_every_status_gets_a_row_even_when_nobody_used_it(self):
        self.make('todo')

        rows = build_demand_opportunity_followups(days=30, now=self.now)['status_rows']
        self.assertEqual([row['key'] for row in rows], ['todo', 'in_progress', 'completed', 'ignored'])
        self.assertEqual([row['task_count'] for row in rows], [1, 0, 0, 0])

    def test_a_completed_row_carries_its_own_fill_counts(self):
        self.make('completed', due_at=self.now + timedelta(days=2), note='已约好线下')
        self.make('completed', due_at=None, note='')

        row = build_demand_opportunity_followups(
            days=30, now=self.now,
        )['status_rows'][2]
        self.assertEqual(row['label'], '已完成')
        self.assertEqual(row['task_count'], 2)
        self.assertEqual(row['with_due_count'], 1)
        self.assertEqual(row['with_note_count'], 1)


class WritePathTests(TestCase):
    """截止时间与备注必须真的能被写进去：一个只读的字段等于没有。"""

    def setUp(self):
        self.staff = make_user('write-staff', is_staff=True)
        self.member = make_user('write-member')
        self.category = Category.objects.create(name='写入分类')
        self.location = CampusLocation.objects.create(name='写入地点')

    def seed_a_radar_row(self):
        SearchQuery.objects.create(
            user=self.member, query='写入测试词', category=self.category,
            location=self.location, result_count=0,
        )
        return build_demand_radar(30)['rows'][0]

    def post_task(self, user, **extra):
        self.client.force_login(user)
        payload = {'radar_key': self.seed_a_radar_row()['radar_key'], 'days': 30}
        payload.update(extra)
        return self.client.post(reverse('create_demand_opportunity_task'), payload)

    def test_staff_can_create_a_task_with_a_due_date_and_a_note(self):
        response = self.post_task(
            self.staff,
            due_at='%s' % (local_noon() + timedelta(days=5)).strftime('%Y-%m-%dT%H:%M'),
            note='已联系两位卖家',
        )

        self.assertEqual(response.status_code, 302)
        task = DemandOpportunityTask.objects.get()
        self.assertEqual(task.note, '已联系两位卖家')
        self.assertIsNotNone(task.due_at)
        self.assertEqual(timezone.localtime(task.due_at).date(), timezone.localdate() + timedelta(days=5))

    def test_a_non_staff_member_still_cannot_create_a_task(self):
        response = self.post_task(self.member, due_at='2026-11-01T10:00', note='想写')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(DemandOpportunityTask.objects.count(), 0)

    def test_an_unreadable_due_date_is_refused_rather_than_silently_dropped(self):
        response = self.post_task(self.staff, due_at='下周三之前', note='想写')

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(DemandOpportunityTask.objects.get().due_at)

    def test_an_overlong_note_is_kept_and_truncated_instead_of_rejected(self):
        response = self.post_task(self.staff, note='备注' * 400)

        self.assertEqual(response.status_code, 302)
        note = DemandOpportunityTask.objects.get().note
        self.assertEqual(len(note), 500)

    def test_updating_a_task_can_set_and_clear_the_due_date(self):
        task = DemandOpportunityTask.objects.create(
            radar_key='update-key', title='更新主题', category=self.category,
            location=self.location, level='medium', opportunity_score=5,
            created_by=self.staff,
        )
        self.client.force_login(self.staff)
        due = (local_noon() + timedelta(days=4)).strftime('%Y-%m-%dT%H:%M')

        self.client.post(
            reverse('update_demand_opportunity_task', args=[task.id]),
            {'status': 'in_progress', 'days': 30, 'due_at': due, 'note': '先问图书馆'},
        )
        task.refresh_from_db()
        self.assertEqual(task.note, '先问图书馆')
        self.assertEqual(timezone.localtime(task.due_at).date(), timezone.localdate() + timedelta(days=4))

        self.client.post(
            reverse('update_demand_opportunity_task', args=[task.id]),
            {'status': 'in_progress', 'days': 30, 'due_at': '', 'note': '先问图书馆'},
        )
        task.refresh_from_db()
        self.assertIsNone(task.due_at)

    def test_an_unreadable_due_date_leaves_the_stored_one_alone(self):
        task = DemandOpportunityTask.objects.create(
            radar_key='update-key-2', title='坏日期主题', category=self.category,
            location=self.location, level='medium', opportunity_score=5,
            created_by=self.staff, due_at=local_noon() + timedelta(days=2),
        )
        self.client.force_login(self.staff)

        self.client.post(
            reverse('update_demand_opportunity_task', args=[task.id]),
            {'status': 'in_progress', 'days': 30, 'due_at': '本周内'},
        )
        task.refresh_from_db()
        self.assertEqual(
            timezone.localtime(task.due_at).date(),
            timezone.localdate() + timedelta(days=2),
        )

    def test_a_non_staff_member_cannot_write_the_two_fields(self):
        task = DemandOpportunityTask.objects.create(
            radar_key='update-key-3', title='越权主题', category=self.category,
            location=self.location, level='medium', opportunity_score=5,
            created_by=self.staff,
        )
        self.client.force_login(self.member)

        response = self.client.post(
            reverse('update_demand_opportunity_task', args=[task.id]),
            {'status': 'completed', 'days': 30, 'due_at': '2026-12-01T10:00', 'note': '想写'},
        )
        self.assertEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.status, 'todo')
        self.assertIsNone(task.due_at)
        self.assertEqual(task.note, '')


class PanelAndExportTests(TestCase):
    """看板要真的把这两个字段画出来、导出去。"""

    def setUp(self):
        self.staff = make_user('panel-staff', is_staff=True)
        self.category = Category.objects.create(name='看板分类')
        self.location = CampusLocation.objects.create(name='看板地点')
        self.client.force_login(self.staff)

    def make(self, status='todo', due_at=None, note=''):
        task = DemandOpportunityTask.objects.create(
            radar_key='panel-%d' % DemandOpportunityTask.objects.count(),
            title='看板主题', category=self.category, location=self.location,
            level='medium', opportunity_score=5, created_by=self.staff,
            status=status, due_at=due_at, note=note,
        )
        DemandOpportunityTask.objects.filter(pk=task.pk).update(
            created_at=local_noon() - timedelta(days=1),
        )
        return task

    def fetch(self):
        html = self.client.get(reverse('operations_dashboard'), {'days': 30}).content.decode('utf-8')
        text = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        ).content.decode('utf-8-sig')
        return html, text

    def test_the_panel_names_the_two_fields_it_reads(self):
        self.make(due_at=local_noon() + timedelta(days=3), note='已联系两位卖家')

        html, _ = self.fetch()
        self.assertIn('跟进任务的截止时间与备注', html)
        self.assertIn('设了截止时间', html)
        self.assertIn('设了期限的，真的更容易做完吗', html)
        self.assertIn('待处理的逾期任务', html)
        self.assertIn('待处理清单不按统计周期过滤', html)

    def test_the_export_names_the_section_and_its_caveats(self):
        self.make(due_at=local_noon() - timedelta(days=2), note='已联系两位卖家')

        _, text = self.fetch()
        self.assertIn('跟进任务的截止时间与备注', text)
        self.assertIn('待处理清单不按统计周期过滤', text)
        self.assertIn('有期限与无期限的完成率对比', text)
        self.assertIn('逾期分档', text)
        self.assertIn('任务状态分布', text)

    def test_the_dashboard_context_carries_the_report(self):
        self.make(due_at=local_noon() + timedelta(days=3))

        html, _ = self.fetch()
        self.assertIn('demand-followup-panel', html)
        self.assertIn('有期限减无期限', html)

    def test_an_empty_period_says_so_instead_of_drawing_zeroes(self):
        html, _ = self.fetch()
        self.assertIn('还没有建立需求跟进任务', html)
        self.assertNotIn('有期限减无期限', html)
