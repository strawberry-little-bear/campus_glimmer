# -*- coding: utf-8 -*-
"""学期阶段快照读路径的测试。

这一轮给 `AcademicTermSnapshot` 加了第一条读路径。判据不是 `refreshed_at`——
`update_or_create` 会把 `auto_now` 字段无条件补进 `update_fields`，每次刷新都推进
它，十个计数一个都没变也照推。真正的判据是这一行的计数是否已经封闭：阶段的结束
日期已经过去，且快照行的区间与当前 `AcademicPhase` 算出来的完全一致。

下面每一条测试都钉住一个判据或一条边界。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.test import TestCase
from django.utils import timezone

from .academic_calendar import (
    build_academic_calendar,
    ensure_default_phases,
    refresh_academic_term_snapshots,
)
from .models import AcademicPhase, AcademicTerm, AcademicTermSnapshot, Category, Item


def _phase_rows(calendar):
    """把看板返回的学期结构摊平成 (学期 slug, phase_key) -> row 的字典。

    键必须带学期。四个默认阶段的 phase_key 是同一套，只按 phase_key 摊平会让
    后一个学期把前一个的同一阶段顶掉：一个「已结束学期 + 进行中学期」的看板里，
    `rows[(self.term.slug, 'registration')]` 拿到的是进行中学期那一行，于是「进行中必须实时算」
    的断言读到的是已结束学期的快照行，反过来也一样。读路径测的恰恰是「哪一行
    读的是哪一份数据」，定位不到行就什么都断言不了。
    """
    return {
        (term['term'].slug, row['phase_key']): row
        for term in calendar['terms']
        for row in term['phases']
    }


class AcademicTermSnapshotReadPathTests(TestCase):
    """已结束阶段读快照、进行中阶段实时算，以及两者数值必须一致。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='snapshot-read-owner', password='safe-password-123',
        )
        self.category = Category.objects.create(name='快照读路径分类')
        self.today = timezone.localdate()
        # 学期从 200 天前开始，四个默认阶段全部落在今天之前：开学周在
        # 186-199 天前，常规教学周在 103-186 天前，考试周和毕业季更早。这样
        # 「已结束」和「进行中」两种情况能在同一个学期里同时出现。
        self.term = AcademicTerm.objects.create(
            name='已结束学期（读路径测试）',
            slug='ended-read-path',
            kind='autumn',
            starts_on=self.today - timedelta(days=200),
            ends_on=self.today - timedelta(days=75),
        )
        ensure_default_phases(self.term)

    def make_item(self, *, title, created_at):
        item = Item.objects.create(
            seller=self.user,
            title=title,
            description='用于快照读路径测试',
            category=self.category,
            condition='八成新',
            price='20.00',
        )
        Item.objects.filter(pk=item.pk).update(created_at=created_at)
        return item

    def seed_counters(self):
        """在开学周区间里造数据，让七个计数都不是零。"""
        opening = AcademicPhase.objects.get(term=self.term, phase='registration')
        start_at = timezone.make_aware(
            timezone.datetime.combine(opening.start_date, timezone.datetime.min.time()),
        )
        self.make_item(title='开学周教材', created_at=start_at + timedelta(hours=2))
        self.make_item(title='开学周台灯', created_at=start_at + timedelta(hours=5))

    def refresh(self):
        return refresh_academic_term_snapshots(term=self.term)

    # ---------- 已结束阶段读快照 ----------

    def test_finished_phases_are_read_from_the_snapshot_table(self):
        self.seed_counters()
        self.refresh()

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        # 四个阶段的结束日期都在今天之前，应当全部读到快照。
        for phase_key in ('registration', 'regular', 'exam', 'graduation'):
            row = rows[(self.term.slug, phase_key)]
            self.assertEqual(
                row['source'], 'snapshot',
                f'{phase_key} 已结束，应当读快照而不是实时算',
            )
        self.assertEqual(calendar['source']['snapshot_row_total'], 4)
        self.assertEqual(calendar['source']['live_row_total'], 0)

    def test_snapshot_rows_match_the_live_numbers(self):
        """读快照和实时算在数值上必须完全一致，否则看板会因为来源不同而变脸。"""
        self.seed_counters()
        self.refresh()

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        opening = rows[(self.term.slug, 'registration')]
        # 开学周里造了两件商品，其余计数为零。
        self.assertEqual(opening['new_items'], 2)
        self.assertEqual(opening['new_demands'], 0)
        self.assertEqual(opening['new_orders'], 0)
        self.assertEqual(opening['completed_orders'], 0)
        self.assertEqual(opening['new_searches'], 0)
        self.assertEqual(opening['zero_result_searches'], 0)
        # 快照里存的 day_count 要原样带出来，不能按实时区间重算。
        phase = AcademicPhase.objects.get(term=self.term, phase='registration')
        self.assertEqual(opening['day_count'], (phase.end_date - phase.start_date).days + 1)

    def test_finished_phase_reads_the_snapshot_without_touching_the_live_tables(self):
        """快照路径不能让实时计数泄漏进来：表被清空后，已结束阶段的数字不变。"""
        self.seed_counters()
        self.refresh()

        before = dict(_phase_rows(build_academic_calendar(day=self.today, limit_terms=3)))
        # 把所有业务表清空，实时路径此时只会算出零。
        Item.objects.all().delete()

        after = dict(_phase_rows(build_academic_calendar(day=self.today, limit_terms=3)))
        for phase_key in ('registration', 'regular', 'exam', 'graduation'):
            self.assertEqual(
                before[(self.term.slug, phase_key)]['new_items'],
                after[(self.term.slug, phase_key)]['new_items'],
                f'{phase_key} 已结束，删空商品表不该改变它读到的数字',
            )
            self.assertEqual(after[(self.term.slug, phase_key)]['source'], 'snapshot')

    # ---------- 进行中阶段实时算 ----------

    def test_running_phase_is_always_computed_live(self):
        """进行中的阶段每小时都在长数据，快照最多是上周的，必须实时算。"""
        # 另建一个今天正在进行的学期。
        running = AcademicTerm.objects.create(
            name='进行中学期（读路径测试）',
            slug='running-read-path',
            kind='autumn',
            starts_on=self.today - timedelta(days=3),
            ends_on=self.today + timedelta(days=90),
        )
        ensure_default_phases(running)
        self.refresh()

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        current = rows[(running.slug, 'registration')]
        self.assertEqual(current['source'], 'live')
        # `refresh()` 只刷已结束学期，running 一行快照都没有，所以它拿到的是
        # 「没有快照可读」这条注脚。要让进行中的阶段真的有一份上周的快照，得连它
        # 一起刷——那才是「快照最多是上周的」想说的那种情况。
        self.assertIn('实时算', current['source_note'])

        # 把 running 也刷一遍：现在它每个阶段都有快照行，而它进行中，仍然必须实时算。
        refresh_academic_term_snapshots(term=running)
        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))
        current = rows[(running.slug, 'registration')]
        self.assertEqual(current['source'], 'live')
        self.assertIn('快照最多是上周的', current['source_note'])
        # 进行中的那一个学期贡献 1 行 live，已结束学期贡献 4 行 snapshot。
        self.assertEqual(calendar['source']['snapshot_row_total'], 4)
        self.assertEqual(calendar['source']['live_row_total'], 4)

    def test_running_phase_sees_rows_created_after_the_last_refresh(self):
        """实时路径要能看到上次刷新之后新落库的数据。"""
        running = AcademicTerm.objects.create(
            name='进行中学期（增量测试）',
            slug='running-increment',
            kind='autumn',
            starts_on=self.today - timedelta(days=3),
            ends_on=self.today + timedelta(days=90),
        )
        ensure_default_phases(running)
        self.refresh()

        opening = AcademicPhase.objects.get(term=running, phase='registration')
        start_at = timezone.make_aware(
            timezone.datetime.combine(opening.start_date, timezone.datetime.min.time()),
        )
        self.make_item(title='刷新之后才发布的商品', created_at=start_at + timedelta(hours=1))

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))
        self.assertEqual(rows[(running.slug, 'registration')]['new_items'], 1)
        self.assertEqual(rows[(running.slug, 'registration')]['source'], 'live')

    # ---------- 区间不匹配时不读 ----------

    def test_orphan_snapshot_row_is_not_read(self):
        """改 starts_on 留下的孤儿行不能被读走：它的区间和当前阶段对不上。"""
        self.seed_counters()
        self.refresh()

        # 把学期开始日期往后推 10 天，四个阶段的 start_date 整体平移，原来的四行
        # 快照全部变成匹配键失效的孤儿行。
        self.term.starts_on = self.term.starts_on + timedelta(days=10)
        self.term.save()

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        for phase_key in ('registration', 'regular', 'exam', 'graduation'):
            self.assertEqual(
                rows[(self.term.slug, phase_key)]['source'], 'live',
                f'{phase_key} 的区间已经平移，旧快照是孤儿行，不能读',
            )
        # 孤儿行还在表里，一行都没被删。
        self.assertEqual(AcademicTermSnapshot.objects.filter(term=self.term).count(), 4)

    def test_snapshot_with_a_shifted_end_date_is_not_read(self):
        """只有 start_date 对得上不够：end_date 变了同样说明这不是同一个阶段。"""
        self.seed_counters()
        self.refresh()

        # 手动把开学周快照的 end_date 往前挪一天，模拟一次阶段边界调整。
        snapshot = AcademicTermSnapshot.objects.get(
            term=self.term, phase_key='registration',
        )
        snapshot.end_date = snapshot.end_date - timedelta(days=1)
        snapshot.day_count = max(snapshot.day_count - 1, 1)
        snapshot.save()

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        self.assertEqual(rows[(self.term.slug, 'registration')]['source'], 'live')

    # ---------- 缺行时不静默退回 ----------

    def test_missing_snapshot_row_falls_back_to_live_and_says_so(self):
        """命令从来没跑过时，已结束阶段也要算得出数字，但来源必须标成实时。"""
        self.seed_counters()
        # 故意不刷新。

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        for phase_key in ('registration', 'regular', 'exam', 'graduation'):
            self.assertEqual(rows[(self.term.slug, phase_key)]['source'], 'live')
        self.assertEqual(calendar['source']['snapshot_row_total'], 0)
        self.assertEqual(calendar['source']['live_row_total'], 4)
        self.assertEqual(rows[(self.term.slug, 'registration')]['new_items'], 2)

    def test_empty_snapshot_table_reports_no_share(self):
        """快照表全空时占比报 None，不报 0%——0% 会被读成「刷过了但一行都没读中」。"""
        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        self.assertEqual(calendar['source']['row_total'], 4)
        self.assertIsNone(calendar['source']['snapshot_share'])

    def test_snapshot_share_counts_only_the_rows_actually_read(self):
        self.seed_counters()
        # 只刷开学周一个阶段，另外三个阶段仍然缺行。
        opening = AcademicPhase.objects.get(term=self.term, phase='registration')
        AcademicTermSnapshot.objects.create(
            term=self.term,
            phase_key=opening.phase,
            start_date=opening.start_date,
            end_date=opening.end_date,
            day_count=(opening.end_date - opening.start_date).days + 1,
            phase_label=opening.get_phase_display(),
            new_items=99,
        )

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        # 快照里存的是 99，实时算是 2，读到的必须是快照那份。
        self.assertEqual(rows[(self.term.slug, 'registration')]['source'], 'snapshot')
        self.assertEqual(rows[(self.term.slug, 'registration')]['new_items'], 99)
        self.assertEqual(calendar['source']['snapshot_row_total'], 1)
        self.assertEqual(calendar['source']['live_row_total'], 3)
        self.assertEqual(calendar['source']['snapshot_share'], 25.0)

    # ---------- 形状 ----------

    def test_both_sources_produce_the_same_keys(self):
        """模板和 CSV 导出不分辨来源，两条路径必须给出同一套键。"""
        running = AcademicTerm.objects.create(
            name='形状对拍学期',
            slug='shape-check',
            kind='autumn',
            starts_on=self.today - timedelta(days=3),
            ends_on=self.today + timedelta(days=90),
        )
        ensure_default_phases(running)
        self.seed_counters()
        self.refresh()

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        # 已结束学期读快照，进行中学期实时算，两边必须给出同一套键。
        snapshot_keys = set(rows[(self.term.slug, 'registration')].keys())
        live_keys = set(rows[(running.slug, 'exam')].keys())
        self.assertEqual(rows[(self.term.slug, 'registration')]['source'], 'snapshot')
        self.assertEqual(rows[(running.slug, 'exam')]['source'], 'live')
        self.assertEqual(snapshot_keys, live_keys)
        # 趋势和占比这类派生量两条路径都要有，否则模板要分支处理。
        for key in ('supply_trend', 'demand_trend', 'zero_result_rate', 'is_current'):
            self.assertIn(key, rows[(self.term.slug, 'registration')])
            self.assertIn(key, rows[(running.slug, 'exam')])

    def test_no_terms_still_carries_the_source_block(self):
        AcademicTerm.objects.all().delete()
        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        self.assertFalse(calendar['has_terms'])
        self.assertEqual(calendar['source']['row_total'], 0)
        self.assertIsNone(calendar['source']['snapshot_share'])

    def test_phase_note_comes_from_the_live_phase_row(self):
        """快照表不存 note，note 必须仍从 AcademicPhase 上取。"""
        self.seed_counters()
        self.refresh()

        calendar = build_academic_calendar(day=self.today, limit_terms=3)
        rows = dict(_phase_rows(calendar))

        phase = AcademicPhase.objects.get(term=self.term, phase='registration')
        self.assertEqual(rows[(self.term.slug, 'registration')]['phase_note'], phase.note)
        self.assertEqual(rows[(self.term.slug, 'registration')]['phase_key'], 'registration')
        self.assertEqual(rows[(self.term.slug, 'registration')]['term_name'], self.term.name)


class AcademicSnapshotSourceSurfaceTests(TestCase):
    """source 块算出来了就必须被读：面板和 CSV 导出都要带上它。

    上一轮把 `AcademicTermSnapshot` 报成「只被写、从未被读」，这一轮给它加了读
    路径。如果这条读路径的结论只停在 `build_academic_calendar` 的返回值里，面
    板和 CSV 一个都不读，那就是把同一个错误原样再犯一遍：数据算出来了，没人看。
    下面几条把「被读」钉在渲染结果上。
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='snapshot-surface-owner', password='safe-password-123',
        )
        self.category = Category.objects.create(name='快照面板分类')
        self.today = timezone.localdate()
        self.term = AcademicTerm.objects.create(
            name='已结束学期（面板测试）',
            slug='ended-surface',
            kind='autumn',
            starts_on=self.today - timedelta(days=200),
            ends_on=self.today - timedelta(days=75),
        )
        ensure_default_phases(self.term)
        self.staff = User.objects.create_user(
            username='snapshot-surface-staff', password='safe-password-123',
        )
        self.staff.is_staff = True
        self.staff.save(update_fields=['is_staff'])

    def make_item(self, *, title, created_at):
        item = Item.objects.create(
            seller=self.user,
            title=title,
            description='用于快照面板测试',
            category=self.category,
            condition='八成新',
            price='20.00',
        )
        Item.objects.filter(pk=item.pk).update(created_at=created_at)
        return item

    def seed_counters(self):
        opening = AcademicPhase.objects.get(term=self.term, phase='registration')
        start_at = timezone.make_aware(
            timezone.datetime.combine(opening.start_date, timezone.datetime.min.time()),
        )
        self.make_item(title='开学周教材', created_at=start_at + timedelta(hours=2))

    def test_dashboard_renders_the_source_share(self):
        """面板要说出读了几行快照、几行实时，否则汇总只是返回值里的数字。"""
        self.seed_counters()
        refresh_academic_term_snapshots(term=self.term)
        self.client.login(
            username='snapshot-surface-staff', password='safe-password-123',
        )

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '读的是每周刷新的阶段快照')
        self.assertContains(response, '进行中的阶段一律实时算')
        # 已结束学期的四个阶段都该标成读快照。
        self.assertContains(response, '读快照')
        self.assertContains(response, '本页 4 个阶段行里')

    def test_csv_export_carries_the_source_columns(self):
        """CSV 每一行都要带来源，否则离线看这份导出无法分辨哪行是缓存。"""
        self.seed_counters()
        refresh_academic_term_snapshots(term=self.term)
        self.client.login(
            username='snapshot-surface-staff', password='safe-password-123',
        )

        response = self.client.get(reverse('operations_dashboard_export'))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('数据来源', content)
        self.assertIn('来源说明', content)
        self.assertIn('数据来源汇总', content)
        # 四个已结束阶段行都应带「读快照」。汇总行和说明段里也有这四个字，
        # 所以按行数，不按全文出现次数。
        source_cells = [
            line for line in content.splitlines()
            if '读快照' in line or '实时算' in line
        ]
        # 1 行汇总 + 4 行阶段 + 若干说明行
        phase_rows = [
            line for line in source_cells
            if line.startswith('已结束学期（面板测试）')
        ]
        self.assertEqual(len(phase_rows), 4)
        for line in phase_rows:
            self.assertIn('读快照', line)

    def test_csv_notes_explain_the_empty_share(self):
        """占比为空要说清它的含义，别让读者拿它当 0%。"""
        AcademicTermSnapshot.objects.all().delete()
        self.client.login(
            username='snapshot-surface-staff', password='safe-password-123',
        )

        response = self.client.get(reverse('operations_dashboard_export'))
        content = response.content.decode('utf-8-sig')

        self.assertIn('一行快照都没读到', content)
