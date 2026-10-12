# -*- coding: utf-8 -*-
"""为「学期阶段统计快照」钉住口径。

`AcademicTermSnapshot` 存在了很久，`refresh_academic_term_snapshots` 每周往里写一次，
`sync_academic_calendar` 有台账、admin 也注册了，而它**没有任何一条读路径**：
`build_academic_calendar` 至今是拿 `AcademicPhase` 的日期区间实时算的。这个模块把它的真实状
态第一次读出来，只报告不改写。这些测试要钉住的比数字本身更要紧的那些取舍：

1. **只报「刷新陈旧度」，不报「内容陈旧度」。** Django 的 `update_or_create` 会把
   `auto_now` 字段显式补进 `update_fields`（`query.py` 第 1002-1014 行），所以每次刷新都会
   推进 `refreshed_at`，不管十个计数有没有变。拿它当内容变化时刻会把一条早就不会再变的阶
   段一直显示为刚刚更新。
2. **过期判据看 `end_date`，不看 `refreshed_at`。** 一条上周才结束的阶段和一条上学期就结束
   的阶段，`refreshed_at` 可能都是昨天，只有 `end_date` 能分开。
3. **孤儿行的判据是匹配键失效，不是学期停用。** `is_active=False` 的学期留下的行是 help_text
   明写的「历史数据保留」，算进孤儿会把设计如此报成异常。
4. **「刷新中断」和孤儿是两个角度。** 孤儿是「表里有当前不存在的阶段」，中断是「当前存在的
   阶段没被刷到」。后者只在周期内确实有成功运行时才判。
5. **覆盖率的分子分母都是应有阶段，不是快照行。** 用快照行当分母会把孤儿行算成覆盖的一部
   分，而孤儿恰恰是多余的。
6. **没有分母时不报 0%，报 None。**
7. **只报告**：不刷新、不删孤儿行、不改 `starts_on`、不删阶段、不加迁移。
"""

from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import AcademicPhase, AcademicTerm, AcademicTermSnapshot, TaskRun
from .academic_term_snapshot_quality import (
    COVERAGE_WARN_PERCENT, REFRESH_GAP_DAYS, STALE_PHASE_DAYS,
    build_academic_term_snapshot_quality,
)


class AcademicTermSnapshotQualityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='snapshot-op', password='safe-password-123', is_staff=True,
        )
        # 一个横跨「现在」的学期，阶段默认计划覆盖开学周到毕业季。
        self.today = timezone.localdate()
        self.term = AcademicTerm.objects.create(
            name='快照统计测试学期', slug='snapshot-quality-term',
            kind='autumn', starts_on=self.today - timedelta(days=20),
            ends_on=self.today + timedelta(days=90), is_active=True,
        )

    # --- 造数据的帮手 ---

    def make_phase(self, *, term=None, phase='regular', start_offset=0, end_offset=13):
        """建一个阶段。`start_date`/`end_date` 是模型属性，不落库。"""
        return AcademicPhase.objects.create(
            term=term or self.term, phase=phase,
            start_offset=start_offset, end_offset=end_offset, note='测试阶段',
        )

    def make_snapshot(self, *, term=None, phase_key='regular', start_date=None,
                      end_date=None, refreshed_at=None):
        """建一行快照，`refreshed_at` 可回填。

        `refreshed_at` 是 `auto_now`，建行时拿真实墙上时间；要造「这一行很久没被刷到」必须
        回填。注意它只在 `save()` 时被推进——`update_or_create` 和 `.update()` 都会写它，
        所以回填只能用 `.update()`，否则会被 `auto_now` 当场覆盖掉。
        """
        snapshot = AcademicTermSnapshot.objects.create(
            term=term or self.term, phase_key=phase_key,
            phase_label=phase_key,
            start_date=start_date or (self.today - timedelta(days=5)),
            end_date=end_date or (self.today + timedelta(days=5)),
            day_count=11, new_items=3, new_demands=2, new_orders=1,
            completed_orders=1, new_searches=5, zero_result_searches=1,
        )
        if refreshed_at is not None:
            AcademicTermSnapshot.objects.filter(pk=snapshot.pk).update(
                refreshed_at=refreshed_at,
            )
            snapshot.refresh_from_db()
        return snapshot

    def make_task_run(self, *, days_ago=0, status='succeeded'):
        """建一条 `sync_academic_calendar` 的台账行，`started_at` 可回填。"""
        finished = timezone.now() - timedelta(days=days_ago)
        started = finished - timedelta(seconds=2)
        run = TaskRun.objects.create(
            name='sync_academic_calendar', status=status,
            started_at=started, finished_at=finished, duration_ms=2000,
            metrics={'marked': 4}, note='',
        )
        TaskRun.objects.filter(pk=run.pk).update(started_at=started)
        run.refresh_from_db()
        return run

    # --- 口径一：刷新是无条件的，所以只报刷新陈旧度 ---

    def test_refreshed_at_advances_even_when_no_counter_changes(self):
        """钉住那条 Django 源码事实：`update_or_create` 无条件推进 `refreshed_at`。

        这不是在测 Django，是在钉住本模块**为什么**不拿 `refreshed_at` 当内容变化时刻。两
        次刷新之间十个计数一个都没变，`refreshed_at` 仍然前进了——如果哪天 Django 改成了
        跳过无变化的保存，这个测试会失败，而失败恰恰说明本模块的判据该重新标定了。
        """
        from listings.academic_calendar import refresh_academic_term_snapshots

        phase = self.make_phase()
        first = refresh_academic_term_snapshots(term=self.term)
        snapshot = AcademicTermSnapshot.objects.get(
            term=self.term, phase_key=phase.phase, start_date=phase.start_date,
        )
        first_stamp = snapshot.refreshed_at
        AcademicTermSnapshot.objects.filter(pk=snapshot.pk).update(
            refreshed_at=timezone.now() - timedelta(days=3),
        )
        snapshot.refresh_from_db()

        second = refresh_academic_term_snapshots(term=self.term)

        self.assertEqual((first, second), (1, 1))
        snapshot.refresh_from_db()
        self.assertGreater(snapshot.refreshed_at, first_stamp)
        # 十个计数一个都没动。
        self.assertEqual(snapshot.new_items, 0)
        self.assertEqual(snapshot.new_searches, 0)

    def test_a_stale_phase_is_judged_by_end_date_not_refreshed_at(self):
        """结束很久的阶段要报过期，哪怕 `refreshed_at` 是刚刚。

        这正是无条件刷新造成的盲区：一条阶段结束两个月了，命令上周还跑过，`refreshed_at`
        新得像才写过。只有 `end_date` 能把它认出来。
        """
        old_end = self.today - timedelta(days=80)
        self.make_snapshot(
            phase_key='regular',
            start_date=old_end - timedelta(days=10), end_date=old_end,
            refreshed_at=timezone.now() - timedelta(hours=2),
        )
        self.make_phase(phase='regular', start_offset=0, end_offset=10)

        report = build_academic_term_snapshot_quality(days=30)

        self.assertEqual(report['stale_rows']['stale_total'], 1)
        self.assertGreater(report['metrics']['stale_total'], 0)
        self.assertLessEqual(
            report['stale_rows']['stale_rows'][0]['days_since_end'], STALE_PHASE_DAYS + 50,
        )

    def test_a_recently_ended_phase_is_not_stale(self):
        """上周才结束的阶段不算过期——门槛是给「早就不会再变」的阶段用的。"""
        self.make_snapshot(
            phase_key='regular',
            start_date=self.today - timedelta(days=13),
            end_date=self.today - timedelta(days=3),
            refreshed_at=timezone.now() - timedelta(days=1),
        )
        self.make_phase(phase='regular', start_offset=0, end_offset=10)

        report = build_academic_term_snapshot_quality(days=30)

        self.assertEqual(report['stale_rows']['stale_total'], 0)

    # --- 口径二：孤儿行是匹配键失效，不是学期停用 ---

    def test_a_phase_whose_start_date_moved_is_an_orphan(self):
        """改一次 `starts_on`，旧快照的匹配键全部失配，变成孤儿。

        这是本模块最具体的一个发现：`AcademicPhase.start_date` 是算出来的属性，改学期开始
        日期会让它整体平移，唯一约束失配后旧行不会被更新、只会被新键旁置。台账里那次运行
        的「刷新阶段快照 4 条」不会有任何变化。
        """
        # 先按当前 starts_on 建一行快照，匹配键是 (term, phase, start_date)。
        phase = self.make_phase(phase='registration', start_offset=0, end_offset=13)
        self.make_snapshot(
            phase_key='registration', start_date=phase.start_date,
            end_date=phase.end_date,
        )

        # 运营把学期开始日期往后推一周，阶段整体平移。
        self.term.starts_on = self.term.starts_on + timedelta(days=7)
        self.term.save()

        report = build_academic_term_snapshot_quality(days=30)

        # 快照行还在（没有任何东西会删它），但它已经匹配不上任何当前阶段。
        self.assertEqual(report['metrics']['row_total'], 1)
        self.assertEqual(report['metrics']['orphan_total'], 1)
        self.assertEqual(len(report['orphan_rows']['orphan_rows']), 1)
        self.assertEqual(
            report['orphan_rows']['orphan_rows'][0]['phase_label'], 'registration',
        )
        # 应有阶段一个都没被覆盖——四个阶段的 start_date 全都平移了。
        self.assertEqual(report['coverage']['expected_phase_total'], 1)
        self.assertEqual(report['coverage']['covered_phase_total'], 0)

    def test_snapshots_of_a_disabled_term_are_not_orphans(self):
        """停用学期留下的行是 help_text 写明的「历史数据保留」，不是孤儿。

        `is_active` 的 help_text 写着「停用后该学期不再参与阶段计算，但历史数据保留」，把这
        些行算进孤儿等于把一条被写死的行为报成异常。
        """
        disabled = AcademicTerm.objects.create(
            name='快照统计测试旧学期', slug='snapshot-quality-old-term',
            kind='spring', starts_on=self.today - timedelta(days=300),
            ends_on=self.today - timedelta(days=200), is_active=False,
        )
        self.make_snapshot(
            term=disabled, phase_key='regular',
            start_date=disabled.starts_on + timedelta(days=14),
            end_date=disabled.starts_on + timedelta(days=40),
        )

        report = build_academic_term_snapshot_quality(days=30)

        self.assertEqual(report['metrics']['orphan_total'], 0)
        self.assertEqual(report['metrics']['inactive_term_row_total'], 1)
        self.assertEqual(
            report['orphan_rows']['inactive_terms'], ['快照统计测试旧学期'],
        )

    # --- 口径三：覆盖率的分子分母都是应有阶段 ---

    def test_coverage_denominator_is_expected_phases_not_snapshot_rows(self):
        """覆盖率的分母是应有阶段，不是快照行。

        用快照行当分母会把孤儿行算成「已覆盖」的一部分——孤儿恰恰是多余的那些。这里造 4 个
        应有阶段、3 行覆盖 + 2 行孤儿，覆盖率必须是 3/4，不是 3/5。
        """
        offsets = [(0, 13), (14, 97), (98, 111), (112, 125)]
        for index, (start_offset, end_offset) in enumerate(offsets):
            phase = self.make_phase(
                phase=['registration', 'regular', 'exam', 'graduation'][index],
                start_offset=start_offset, end_offset=end_offset,
            )
            if index < 3:
                self.make_snapshot(
                    phase_key=phase.phase, start_date=phase.start_date,
                    end_date=phase.end_date,
                )
        # 两行孤儿：匹配键不在当前阶段集合里。
        for extra in range(2):
            self.make_snapshot(
                phase_key='regular',
                start_date=self.today + timedelta(days=200 + extra),
                end_date=self.today + timedelta(days=210 + extra),
            )

        report = build_academic_term_snapshot_quality(days=30)

        self.assertEqual(report['coverage']['expected_phase_total'], 4)
        self.assertEqual(report['coverage']['covered_phase_total'], 3)
        self.assertEqual(report['coverage']['covered_share'], 75.0)
        self.assertEqual(report['metrics']['orphan_total'], 2)

    # --- 口径四：刷新中断是另一个角度，且只在有基准时才判 ---

    def test_refresh_gap_needs_a_successful_run_as_its_baseline(self):
        """周期内没有成功运行时，一行都不判「没被刷到」。

        没有基准就没有「落后于基准」这回事。此时该报的是命令没跑，由 `ran_in_period` 和
        `command_state` 负责，不要让每一行都看起来像被漏掉了。
        """
        self.make_snapshot(refreshed_at=timezone.now() - timedelta(days=40))

        report = build_academic_term_snapshot_quality(days=30)

        self.assertFalse(report['gap_rows']['has_baseline'])
        self.assertEqual(report['gap_rows']['gap_total'], 0)
        self.assertFalse(report['metrics']['command_ran_in_period'])

    def test_a_row_the_command_never_reached_is_a_refresh_gap(self):
        """有成功运行做基准时，远早于基准的行报为「没被刷到」。

        这和孤儿是两个角度：孤儿行对应的阶段已经不存在，中断行对应的阶段还在、只是没被写
        到。两者都报，互不替代。
        """
        phase = self.make_phase()
        self.make_snapshot(
            phase_key=phase.phase, start_date=phase.start_date,
            end_date=phase.end_date,
            refreshed_at=timezone.now() - timedelta(days=30),
        )
        self.make_task_run(days_ago=1)

        report = build_academic_term_snapshot_quality(days=30)

        self.assertTrue(report['gap_rows']['has_baseline'])
        self.assertEqual(report['gap_rows']['gap_total'], 1)
        self.assertEqual(report['metrics']['orphan_total'], 0)

    def test_a_recently_refreshed_row_is_not_a_gap(self):
        """刚被刷到的行不算中断。"""
        phase = self.make_phase()
        self.make_snapshot(
            phase_key=phase.phase, start_date=phase.start_date,
            end_date=phase.end_date,
            refreshed_at=timezone.now() - timedelta(hours=6),
        )
        self.make_task_run(days_ago=1)

        report = build_academic_term_snapshot_quality(days=30)

        self.assertEqual(report['gap_rows']['gap_total'], 0)

    # --- 口径五：没有分母时不报 0% ---

    def test_an_empty_table_reports_no_shares_at_all(self):
        """空表不报 0%，覆盖率是 None。"""
        report = build_academic_term_snapshot_quality(days=30)

        self.assertFalse(report['has_data'])
        self.assertIsNone(report['coverage']['covered_share'])
        self.assertIsNone(report['stale_rows']['stale_share'])
        self.assertEqual(report['metrics']['row_total'], 0)
        self.assertEqual(report['metrics']['command_ran_in_period'], False)
        # 空表提示要置顶，不能被「命令一次都没跑过」挤掉——表里连行都没有，比命令这周没跑
        # 更要紧。
        self.assertEqual(report['recommendations'][0].count('还是空的'), 1)
        self.assertIn('没有任何读路径', report['recommendations'][0])

    def test_a_table_without_expected_phases_still_reports_row_total(self):
        """没有任何启用学期时，快照行仍然可数，只是没有覆盖率的分母。"""
        self.term.is_active = False
        self.term.save()
        self.make_snapshot(refreshed_at=timezone.now())

        report = build_academic_term_snapshot_quality(days=30)

        self.assertTrue(report['has_data'])
        self.assertIsNone(report['coverage']['covered_share'])
        self.assertEqual(report['metrics']['inactive_term_row_total'], 1)

    # --- 口径六：只报告 ---

    def test_the_module_never_writes_anything(self):
        """跑一遍之后，快照表、阶段表和学期表一个字节都不许变。"""
        phase = self.make_phase()
        self.make_snapshot(
            phase_key=phase.phase, start_date=phase.start_date,
            end_date=phase.end_date, refreshed_at=timezone.now() - timedelta(days=9),
        )
        self.make_task_run(days_ago=2)
        before_rows = AcademicTermSnapshot.objects.count()
        before_snapshot = AcademicTermSnapshot.objects.get().refreshed_at
        before_phases = AcademicPhase.objects.count()
        before_starts_on = self.term.starts_on

        build_academic_term_snapshot_quality(days=30)

        self.assertEqual(AcademicTermSnapshot.objects.count(), before_rows)
        self.assertEqual(
            AcademicTermSnapshot.objects.get().refreshed_at, before_snapshot,
        )
        self.assertEqual(AcademicPhase.objects.count(), before_phases)
        self.term.refresh_from_db()
        self.assertEqual(self.term.starts_on, before_starts_on)

    def test_days_outside_the_allowed_set_falls_back_to_thirty(self):
        """非法的 `days` 回落到 30，和其他模块同一条规矩。"""
        report = build_academic_term_snapshot_quality(days=13)

        self.assertEqual(report['period_days'], 30)

    # --- 判据的门槛值与提示的指向 ---

    def test_low_coverage_points_at_missing_or_disabled_phases(self):
        """覆盖率低于门槛时，提示要点名缺口而不是只说「持续关注」。"""
        offsets = [(0, 13), (14, 97), (98, 111), (112, 125)]
        phases = []
        for index, (start_offset, end_offset) in enumerate(offsets):
            phases.append(self.make_phase(
                phase=['registration', 'regular', 'exam', 'graduation'][index],
                start_offset=start_offset, end_offset=end_offset,
            ))
        # 只给第一个阶段配一行快照。匹配键是 (term, phase_key, start_date)，而 start_date
        # 是阶段算出来的属性，所以必须拿 `phases[0].start_date` 去建这一行，不能自己编一个
        # 日期——编一个对不上的日期等于又造了一行孤儿。
        self.make_snapshot(
            phase_key=phases[0].phase, start_date=phases[0].start_date,
            end_date=phases[0].end_date, refreshed_at=timezone.now(),
        )

        report = build_academic_term_snapshot_quality(days=30)

        self.assertEqual(report['coverage']['covered_share'], 25.0)
        joined = ''.join(report['recommendations'])
        self.assertIn('25.0%', joined)
        self.assertIn('只报告', joined)

    def test_a_command_that_never_ran_is_called_out(self):
        """命令一次都没跑过要单独点名，因为这张表本来就没人读。"""
        phase = self.make_phase()
        self.make_snapshot(
            phase_key=phase.phase, start_date=phase.start_date,
            end_date=phase.end_date,
        )

        report = build_academic_term_snapshot_quality(days=30)

        joined = ''.join(report['recommendations'])
        self.assertIn('sync_academic_calendar', joined)
        self.assertIn('一次都没跑过', joined)

    def test_coverage_warn_percent_is_exported_for_the_panel(self):
        """门槛值要能取到，面板才能把「为什么没提示」写清楚。"""
        report = build_academic_term_snapshot_quality(days=30)

        self.assertEqual(
            report['metrics']['coverage_warn_percent'], COVERAGE_WARN_PERCENT,
        )
        self.assertEqual(REFRESH_GAP_DAYS, 7)
