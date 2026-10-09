from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .analytics import build_operational_alerts
from .demand_radar_outcome import (
    CONVERGENCE_DELTA_POINTS,
    build_demand_radar_outcomes,
    build_task_outcome,
    parse_radar_key,
)
from .models import Category, CampusLocation, DemandOpportunityTask, Item, SearchQuery


class DemandRadarOutcomeTests(TestCase):
    """这些用例覆盖的是模块存在的理由：证明跟进任务是否真的补上了缺口。

    只看雷达会把每个任务都当成进展，而运营真正需要的是事后判定。
    """

    def setUp(self):
        self.user = User.objects.create_user(username='outcome-user', password='safe-password-123')
        self.category = Category.objects.create(name='数码设备')
        self.location = CampusLocation.objects.create(name='图书馆东门')

    def _make_task(self, term='充电宝', **kwargs):
        """按需构造任务。

        created_at 是 auto_now_add，直接传进去会被忽略，所以建完之后再用
        update 写回，否则所有任务都落在"现在"，观察期永远不结束。
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
        created_at = defaults.pop('created_at', timezone.now() - timedelta(days=20))
        task = DemandOpportunityTask.objects.create(**defaults)
        DemandOpportunityTask.objects.filter(pk=task.pk).update(created_at=created_at)
        task.refresh_from_db()
        return task

    def _search(self, query, result_count, created_at, **kwargs):
        """按指定时间写入搜索记录。

        SearchQuery.created_at 也是 auto_now_add，创建时传值会被静默丢弃，
        所以必须先落库再用 update 写回时间，否则数据全都堆在"现在"，
        基线与观察两个窗口都会是空的。
        """
        record = SearchQuery.objects.create(
            user=self.user, query=query, category=self.category,
            location=self.location, result_count=result_count, **kwargs,
        )
        SearchQuery.objects.filter(pk=record.pk).update(created_at=created_at)
        return record

    def test_parse_radar_key_handles_pipe_inside_term(self):
        """搜索词本身可能含竖线，必须从右侧解析才不会被切开。"""
        parsed = parse_radar_key('A|B 充电宝|category:3|location:5')
        self.assertEqual(parsed['term'], 'A|B 充电宝')
        self.assertEqual(parsed['category_id'], 3)
        self.assertEqual(parsed['location_id'], 5)

    def test_parse_radar_key_rejects_malformed_key(self):
        self.assertIsNone(parse_radar_key(''))
        self.assertIsNone(parse_radar_key('充电宝|category:x|location:1'))
        self.assertIsNone(parse_radar_key('充电宝'))

    def test_zero_result_rate_drop_after_supply_is_added_counts_as_converged(self):
        """基线期全是无结果搜索，观察期都搜到了，判定为缺口收敛。"""
        now = timezone.now()
        task = self._make_task(created_at=now - timedelta(days=20))
        for day in range(34, 20, -1):
            self._search('充电宝', 0, now - timedelta(days=day))
        for day in range(20, 6, -1):
            self._search('充电宝', 3, now - timedelta(days=day))
        Item.objects.create(
            title='充电宝', description='全新', price='50.00',
            category=self.category, location=self.location, condition='全新', seller=self.user,
        )

        outcome = build_task_outcome(task, now=now)

        self.assertEqual(outcome['baseline_zero_rate'], 100.0)
        self.assertEqual(outcome['observation_zero_rate'], 0.0)
        self.assertEqual(outcome['delta_points'], 100.0)
        self.assertEqual(outcome['outcome'], 'converged')
        self.assertEqual(outcome['supply_delta'], 1)
        self.assertIn('供给动作生效', outcome['verdict'])

    def test_rising_zero_result_rate_is_reported_as_diverged(self):
        """缺口扩大不能被粉饰成持平，运营需要知道任务没起作用。"""
        now = timezone.now()
        task = self._make_task(created_at=now - timedelta(days=20))
        for day in range(34, 20, -1):
            self._search('充电宝', 2, now - timedelta(days=day))
        for day in range(20, 6, -1):
            self._search('充电宝', 0, now - timedelta(days=day))

        outcome = build_task_outcome(task, now=now)

        self.assertEqual(outcome['outcome'], 'diverged')
        self.assertLessEqual(outcome['delta_points'], -CONVERGENCE_DELTA_POINTS)
        self.assertEqual(outcome['supply_delta'], 0)

    def test_small_change_is_flat_not_converged(self):
        """几个百分点的波动是噪声，不能算成绩。"""
        now = timezone.now()
        task = self._make_task(created_at=now - timedelta(days=20))
        for day in range(34, 20, -1):
            self._search('充电宝', 0, now - timedelta(days=day))
        for day in range(20, 6, -1):
            # 观察期 14 次里只有 1 次搜到结果，无结果率 92.9%，
            # 比基线的 100% 低 7.1 个百分点，落在噪声带内。
            self._search('充电宝', 0 if day != 7 else 5, now - timedelta(days=day))

        outcome = build_task_outcome(task, now=now)

        self.assertEqual(outcome['outcome'], 'flat')

    def test_thin_sample_is_never_scored_as_success_or_failure(self):
        """只有一次搜索就宣布成功是自欺欺人，必须单独归为样本不足。"""
        now = timezone.now()
        task = self._make_task(created_at=now - timedelta(days=20))
        self._search('充电宝', 0, now - timedelta(days=25))
        self._search('充电宝', 0, now - timedelta(days=10))

        outcome = build_task_outcome(task, now=now)

        self.assertEqual(outcome['outcome'], 'insufficient')
        self.assertIsNone(outcome['delta_points'])

    def test_young_task_is_pending_until_observation_window_closes(self):
        """刚建的任务还没有足够观察期，提前判定会冤枉人。"""
        now = timezone.now()
        task = self._make_task(created_at=now - timedelta(days=2))
        self._search('充电宝', 0, now - timedelta(days=1))

        outcome = build_task_outcome(task, now=now)

        self.assertEqual(outcome['outcome'], 'pending')
        self.assertFalse(outcome['is_mature'])

    def test_unparseable_task_is_excluded_from_judgement(self):
        """雷达标识损坏时不猜测，直接说明无法参与统计。"""
        now = timezone.now()
        task = self._make_task(radar_key='损坏的标识', created_at=now - timedelta(days=20))

        outcome = build_task_outcome(task, now=now)

        self.assertEqual(outcome['outcome'], 'pending')
        self.assertIsNotNone(outcome['verdict'])

    def test_summary_hit_rate_ignores_insufficient_tasks(self):
        """命中率只统计有证据的任务，避免用没人搜的话题惩罚指标。"""
        now = timezone.now()
        self._make_task('充电宝', created_at=now - timedelta(days=20))
        self._make_task('台灯', created_at=now - timedelta(days=20))
        for day in range(34, 20, -1):
            self._search('充电宝', 0, now - timedelta(days=day))
        for day in range(20, 6, -1):
            self._search('充电宝', 2, now - timedelta(days=day))
        self._search('台灯', 0, now - timedelta(days=25))

        report = build_demand_radar_outcomes(days=60, now=now)

        self.assertEqual(report['summary']['task_count'], 2)
        self.assertEqual(report['summary']['converged_count'], 1)
        self.assertEqual(report['summary']['insufficient_count'], 1)
        self.assertEqual(report['summary']['judged_count'], 1)
        self.assertEqual(report['summary']['hit_rate'], 100.0)

    def test_report_only_covers_tasks_created_within_period(self):
        """days 只筛选任务本身，和对比窗口长度互不影响。"""
        now = timezone.now()
        self._make_task('周期内任务', created_at=now - timedelta(days=5))
        self._make_task('更早的任务', created_at=now - timedelta(days=90))

        report = build_demand_radar_outcomes(days=30, now=now)

        titles = [row['title'] for row in report['rows']]
        self.assertIn('周期内任务', titles)
        self.assertNotIn('更早的任务', titles)

    def test_status_filter_limits_reported_tasks(self):
        """运营想只看已完成的任务时，todo 不应混进来。"""
        now = timezone.now()
        self._make_task('待跟进任务', status='todo', created_at=now - timedelta(days=5))
        self._make_task('已完成任务', status='completed', created_at=now - timedelta(days=5))

        report = build_demand_radar_outcomes(days=30, now=now, statuses=['completed'])

        self.assertEqual([row['title'] for row in report['rows']], ['已完成任务'])

    def _neutral_metrics(self):
        """一组不会触发其它运营提醒的基线指标，保证只测闭环告警本身。"""
        return {
            'searches': 0, 'zero_result_searches': 0, 'zero_result_rate': 0,
            'searches_with_results': 0, 'search_click_through_rate': 0,
            'pending_reports': 0, 'mutual_aid_feedbacks': 0,
            'mutual_aid_completion_rate': 0,
        }

    def test_dashboard_renders_outcome_panel_with_task_filter(self):
        """闭环面板必须出现在看板上，否则运营看不到判定结果。"""
        staff = User.objects.create_superuser(
            username='outcome-staff', email='outcome@example.com', password='safe-password-123',
        )
        self._make_task('充电宝', status='completed', created_at=timezone.now() - timedelta(days=20))
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 60})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '需求跟进闭环效果')
        self.assertContains(response, '充电宝')
        self.assertContains(response, '闭环命中率')

        filtered = self.client.get(
            reverse('operations_dashboard'), {'days': 60, 'task_status': 'completed'},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertContains(filtered, '需求跟进闭环效果')

    def test_outcome_rows_are_exported_for_weekly_reporting(self):
        """闭环结论要能导出，运营周报才有据可依。"""
        staff = User.objects.create_superuser(
            username='export-staff', email='export@example.com', password='safe-password-123',
        )
        self._make_task('台灯', created_at=timezone.now() - timedelta(days=20))
        self.client.force_login(staff)

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 60})

        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '需求跟进闭环效果')
        self.assertContains(export, '闭环命中率')
        self.assertContains(export, '台灯')

    def test_low_hit_rate_raises_operational_alert(self):
        """命中率过低说明雷达在报假警，这比单个缺口更值得运营关注。"""
        now = timezone.now()
        outcomes = build_demand_radar_outcomes(days=60, now=now)
        outcomes['summary'].update({
            'judged_count': 4,
            'hit_rate': 25.0,
            'converged_count': 1,
        })

        alerts = build_operational_alerts(
            self._neutral_metrics(), [], None, None, outcomes,
        )

        alert = next(item for item in alerts if item['key'] == 'demand_radar_hit_rate')
        self.assertEqual(alert['severity'], 'warning')
        self.assertIn('25.0%', alert['message'])
        self.assertEqual(alert['action_url_name'], 'operations_dashboard')

    def test_thin_evidence_does_not_trigger_hit_rate_alert(self):
        """只有一两个样本时不足以否定整套机制，不应打扰运营。"""
        outcomes = build_demand_radar_outcomes(days=60)
        outcomes['summary'].update({'judged_count': 2, 'hit_rate': 0.0})

        alerts = build_operational_alerts(self._neutral_metrics(), [], None, None, outcomes)

        self.assertFalse(any(item['key'] == 'demand_radar_hit_rate' for item in alerts))
