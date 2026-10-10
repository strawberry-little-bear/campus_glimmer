# -*- coding: utf-8 -*-
"""Cover the phase-by-category cross built on top of the borrow stratification.

The cross exists because a category's third-level rate means two different
things depending on when its borrows happened: concentrated in a fortnight of
exam phase, or spread across a fourteen-week term. These tests therefore pin
down the things that decide which of the two a reader is looking at.

Three of them matter more than the rest. A long phase must not win a volume
comparison just for being long, which is what the per-day figure is for and what
the first test checks. A reporting window that clips a phase must not turn that
phase into an apparent spike, which is why the divisor is the days the phase
actually produced borrows on. And a borrow the calendar cannot place must still
be counted, because dropping it would quietly inflate every phase's share.

The last two are boundaries rather than arithmetic: a cell too thin to conclude
from stays visible and marked instead of being hidden or promoted, and nothing
in this module may reach back into the escalation ladder.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .academic_calendar import ensure_default_phases
from .borrow_escalation import ESCALATION_SCHEDULE, escalate_overdue_borrows
from .borrow_rhythm import (
    DEFAULT_LOOKBACK_DAYS,
    MIN_SAMPLE_SIZE,
    NO_TERM_KEY,
    PHASE_ORDER,
    SYNTHETIC_PHASE_KEYS,
    UNASSIGNED_PHASE_KEY,
    build_borrow_rhythm,
)

from .models import AcademicTerm, Category, CampusLocation, Item, Order


class BorrowRhythmCrossTests(TestCase):
    """Which borrows land in which phase, and what the cross is allowed to say."""

    def setUp(self):
        self.seller = User.objects.create_user(username='rhythm-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='rhythm-borrower', password='safe-password-123')
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)
        self.location_serial = 0
        self.category_serial = 0

    def make_category(self, name):
        self.category_serial += 1
        return Category.objects.create(name=f'{name}{self.category_serial}')

    def make_term(self, *, starts_on=None, ends_on=None, with_phases=True):
        term = AcademicTerm.objects.create(
            name=f'节奏测试学期{AcademicTerm.objects.count() + 1}',
            slug=f'rhythm-term-{AcademicTerm.objects.count() + 1}',
            kind='autumn',
            starts_on=starts_on or (self.today - timedelta(days=30)),
            ends_on=ends_on or (self.today + timedelta(days=60)),
        )
        if with_phases:
            ensure_default_phases(term)
        return term

    def make_borrow(self, *, category, created_at=None, deposit_amount='100.00',
                    overdue_days=None, borrower=None):
        """One borrow order, placed on a given day and optionally overdue."""
        self.location_serial += 1
        item = Item.objects.create(
            title=f'{category.name}测试物',
            description='用于借用与学期节律交叉测试',
            trade_mode='borrow',
            price='60.00',
            deposit_amount=deposit_amount,
            borrow_days=14,
            category=category,
            location=CampusLocation.objects.create(name=f'节奏地点{self.location_serial}'),
            condition='9成新',
            seller=self.seller,
        )
        order = Order.objects.create(
            item=item,
            buyer=borrower or self.borrower,
            seller=self.seller,
            agreed_price='0.00',
            deposit_amount=deposit_amount,
            status='borrowed',
        )
        if overdue_days is not None:
            # 借用日和到期日分开处理：借用日决定它落在哪个阶段，到期日决定
            # 它逾期了多久。逾期门槛按到期日起算（1 天起步），所以到期日必须
            # 真的落在 now 之前 overdue_days 天，只把到期日设成 now 会让催收
            # 一级都建不起来，闭环时长也就无从量起。
            Order.objects.filter(pk=order.pk).update(
                created_at=created_at or (self.now - timedelta(days=14)),
                return_due_at=self.now - timedelta(days=overdue_days),
            )
            order.refresh_from_db()
        elif created_at is not None:
            Order.objects.filter(pk=order.pk).update(created_at=created_at)
            order.refresh_from_db()
        return order

    def at_midnight(self, day):
        """An aware datetime on the given local date."""
        return timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))

    def report(self, days=DEFAULT_LOOKBACK_DAYS):
        # The window has to sit after the rows: auto_now_add writes the real
        # insert time, which is later than the self.now captured in setUp.
        return build_borrow_rhythm(days=days, now=self.now + timedelta(minutes=5))

    def borrows_in_phase(self, report, phase_key):
        for row in report['rows']:
            if row['phase_key'] == phase_key:
                return row
        return None

    # -- the population ----------------------------------------------------

    def test_borrows_that_were_never_escalated_stay_in_the_phase_denominator(self):
        # 和风险分层同一个前提：没升级过的借用必须计入分母，否则每个阶段的
        # 升级率都会变成 100%，交叉表就白做了。
        self.make_term()
        category = self.make_category('计入分母')
        for index in range(3):
            self.make_borrow(category=category, created_at=self.at_midnight(self.today))

        row = self.borrows_in_phase(self.report(), 'regular')

        self.assertEqual(row['borrow_count'], 3)
        self.assertEqual(row['escalated_count'], 0)
        self.assertEqual(row['level_three_rate'], 0)

    def test_sales_are_not_counted_as_borrows(self):
        # 普通买卖混进来会把所有阶段的比率都稀释掉。
        self.make_term()
        category = self.make_category('普通买卖')
        item = Item.objects.create(
            title='普通买卖测试物', description='不是借用',
            trade_mode='sale', price='30.00', category=category,
            location=CampusLocation.objects.create(name=f'节奏买卖地点{self.location_serial + 1}'),
            condition='八成新', seller=self.seller,
        )
        Order.objects.create(
            item=item, buyer=self.borrower, seller=self.seller,
            agreed_price='30.00', status='completed',
            created_at=self.at_midnight(self.today),
        )
        borrow_category = self.make_category('正常借用')
        self.make_borrow(category=borrow_category, created_at=self.at_midnight(self.today))

        report = self.report()

        self.assertEqual(report['borrow_count'], 1)
        self.assertEqual(len(report['rows']), 1)

    # -- the phase split ---------------------------------------------------

    def test_a_borrow_is_resolved_into_the_phase_of_its_own_day(self):
        # 阶段归属按借用发生日解析，不按归还日：借用发生在考试周、拖到开学后
        # 才升级，仍然是考试周借出去的那一笔。
        term = self.make_term(
            starts_on=self.today - timedelta(days=200),
            ends_on=self.today - timedelta(days=60),
        )
        AcademicTerm.objects.update(is_active=False)
        term.is_active = True
        term.save(update_fields=['is_active'])

        category = self.make_category('阶段归属')
        self.make_borrow(category=category, created_at=self.at_midnight(term.starts_on))
        self.make_borrow(category=category, created_at=self.at_midnight(term.starts_on + timedelta(days=20)))
        self.make_borrow(category=category, created_at=self.at_midnight(term.starts_on + timedelta(days=100)))
        self.make_borrow(category=category, created_at=self.at_midnight(term.starts_on + timedelta(days=115)))

        rows = {row['phase_key']: row for row in self.report(days=400)['rows']}

        self.assertEqual(rows['registration']['borrow_count'], 1)
        self.assertEqual(rows['regular']['borrow_count'], 1)
        self.assertEqual(rows['exam']['borrow_count'], 1)
        self.assertEqual(rows['graduation']['borrow_count'], 1)

    def test_phases_are_listed_in_calendar_order_not_database_order(self):
        # 表格要按学期进程排，否则读者得自己把行拼回日历上。
        term = self.make_term(
            starts_on=self.today - timedelta(days=120),
            ends_on=self.today + timedelta(days=10),
        )
        category = self.make_category('排序')
        # 第 110 天落在考试周，第 40 天落在常规教学周，第 5 天落在开学周。
        for offset in (110, 40, 5):
            self.make_borrow(
                category=category,
                created_at=self.at_midnight(term.starts_on + timedelta(days=offset)),
            )

        report = self.report()

        self.assertEqual(
            [row['phase_key'] for row in report['rows']],
            ['registration', 'regular', 'exam'],
        )

    def test_the_same_phase_key_across_two_terms_is_reported_once(self):
        # 两个学期都有考试周时，同一阶段键合成一行：读者要比较的是「考试周
        # 是什么样」，不是「去年秋天那次考试周」。
        old_term = self.make_term(
            starts_on=self.today - timedelta(days=130),
            ends_on=self.today - timedelta(days=5),
        )
        new_term = self.make_term(
            starts_on=self.today - timedelta(days=2),
            ends_on=self.today + timedelta(days=120),
        )
        category = self.make_category('跨学期同阶段')
        # 老学期的考试周（第 98-111 天）落在窗口内，新学期的考试周还没到，
        # 所以这里补一笔落在新学期开学周的借用，验证同键合并不是靠运气。
        self.make_borrow(
            category=category,
            created_at=self.at_midnight(old_term.starts_on + timedelta(days=100)),
        )
        self.make_borrow(
            category=category,
            created_at=self.at_midnight(old_term.starts_on + timedelta(days=105)),
        )

        row = self.borrows_in_phase(self.report(days=400), 'exam')

        self.assertEqual(row['borrow_count'], 2)
        self.assertEqual(row['term_name'], old_term.name)

    # -- unequal phase lengths ---------------------------------------------

    def test_a_long_phase_does_not_win_the_comparison_by_being_long(self):
        # 常规教学周有 84 天，考试周只有 14 天。按总次数比，长阶段永远赢，
        # 那说明的是阶段长度而不是风险。日均口径下两者才可比。
        term = self.make_term(
            starts_on=self.today - timedelta(days=125),
            ends_on=self.today + timedelta(days=10),
        )
        category = self.make_category('阶段长度')
        # 常规周：84 天里均匀铺 20 笔，每天约 0.24 笔。
        for index in range(20):
            self.make_borrow(
                category=category,
                created_at=self.at_midnight(term.starts_on + timedelta(days=14 + index * 4)),
            )
        # 考试周：只铺其中 3 天、每天多笔，日均明显高于常规周，总量却小得多。
        for index in range(3):
            for _ in range(2):
                self.make_borrow(
                    category=category,
                    created_at=self.at_midnight(term.starts_on + timedelta(days=98 + index)),
                )

        rows = {row['phase_key']: row for row in self.report(days=400)['rows']}

        self.assertGreater(rows['regular']['borrow_count'], rows['exam']['borrow_count'])
        self.assertLess(rows['regular']['borrow_per_day'], rows['exam']['borrow_per_day'])

    def test_a_clipped_phase_is_not_reported_as_a_spike(self):
        # 报告窗口只覆盖一个阶段的后几天时，用阶段名义长度做除数会把日均放
        # 大好几倍——那看起来像个发现，其实是窗口切出来的假象。除数必须是
        # 阶段真正产生借用的那些天。
        term = self.make_term(
            starts_on=self.today - timedelta(days=103),
            ends_on=self.today + timedelta(days=30),
        )
        category = self.make_category('窗口截断')
        # 考试周是第 98-111 天，其中第 98-103 天刚刚过去，只落在最近 6 天的
        # 窗口里。若用阶段名义长度 14 天做除数，日均会被压成 6/14；用真正
        # 产生借用的 6 天做除数才是 1.0。
        for index in range(6):
            self.make_borrow(
                category=category,
                created_at=self.at_midnight(term.starts_on + timedelta(days=98 + index)),
            )

        row = self.borrows_in_phase(self.report(days=6), 'exam')

        self.assertEqual(row['active_days'], 6)
        self.assertEqual(row['borrow_per_day'], 1.0)

    def test_the_share_is_taken_inside_the_phase(self):
        # 阶段内占比回答「考试周发生的事里有多少出了错」，用全站借用做分母
        # 回答的是另一个问题，而且会随总量涨落。
        self.make_term()
        category = self.make_category('阶段内占比')
        for index in range(4):
            self.make_borrow(category=category, created_at=self.at_midnight(self.today))
        for index in range(2):
            self.make_borrow(
                category=category,
                created_at=self.at_midnight(self.today - timedelta(days=100)),
            )

        report = self.report()

        current = self.borrows_in_phase(report, 'regular')
        self.assertEqual(current['share_in_window'], 66.7)

    # -- the category cross ------------------------------------------------

    def test_the_category_breakdown_adds_up_to_the_phase_total(self):
        # 阶段合计和阶段内分类明细必须对得上：两张表并排读，内层加不起来
        # 读者就不会再信其中任何一张。
        self.make_term()
        category = self.make_category('明细对账')
        for _ in range(3):
            self.make_borrow(category=category, created_at=self.at_midnight(self.today))
        other = self.make_category('明细对账另一类')
        for _ in range(2):
            self.make_borrow(category=other, created_at=self.at_midnight(self.today))

        row = self.borrows_in_phase(self.report(), 'regular')
        inner = {entry['key']: entry for entry in row['category_rows']}

        self.assertEqual(sum(entry['borrow_count'] for entry in row['category_rows']), 5)
        self.assertEqual(inner[category.name]['share_in_phase'], 60.0)
        self.assertEqual(inner[other.name]['share_in_phase'], 40.0)

    def test_the_category_share_is_normalised_inside_the_phase(self):
        # 阶段内分类占比的分母是这个阶段的借用总数，不是全站借用数。
        self.make_term()
        category = self.make_category('归一化')
        self.make_borrow(category=category, created_at=self.at_midnight(self.today))
        other = self.make_category('归一化另一类')
        self.make_borrow(category=other, created_at=self.at_midnight(self.today))

        row = self.borrows_in_phase(self.report(), 'regular')
        inner = {entry['key']: entry for entry in row['category_rows']}

        self.assertEqual(inner[category.name]['share_in_phase'], 50.0)

    # -- sparse cells ------------------------------------------------------

    def test_a_cell_too_thin_to_read_is_marked_not_hidden(self):
        # 2 笔里 1 笔拖到第三级是 50%，这个数字不能当结论，但直接藏起来会让
        # 人以为这个分类没有风险。
        self.make_term()
        category = self.make_category('小样本分类')
        self.make_borrow(category=category, overdue_days=9)
        self.make_borrow(category=category)
        escalate_overdue_borrows(now=self.now)

        row = self.borrows_in_phase(self.report(), 'regular')
        inner = row['category_rows'][0]

        self.assertEqual(inner['borrow_count'], 2)
        self.assertTrue(inner['is_small_sample'])

    def test_the_floor_is_not_lowered_for_the_cross(self):
        # 交叉表的一个格子证据比任何一张父表都薄，父表里算太薄的数字不能
        # 到交叉表里突然变够。门槛必须和风险分层完全一致。
        self.assertEqual(MIN_SAMPLE_SIZE, 5)

        self.make_term()
        category = self.make_category('门槛对齐')
        for index in range(MIN_SAMPLE_SIZE):
            self.make_borrow(category=category, created_at=self.at_midnight(self.today))

        row = self.borrows_in_phase(self.report(), 'regular')

        self.assertEqual(row['borrow_count'], MIN_SAMPLE_SIZE)
        self.assertFalse(row['is_small_sample'])

    def test_a_phase_below_the_floor_is_excluded_from_the_conclusion(self):
        # 样本不足的阶段可以列出来，但不该被拿去当「最忙阶段」的结论。
        self.make_term()
        category = self.make_category('不参与结论')
        self.make_borrow(category=category, created_at=self.at_midnight(self.today))
        escalate_overdue_borrows(now=self.now)

        report = self.report()

        self.assertFalse(report['has_sample'])
        self.assertIn('单量不足', report['summary'])

    # -- borrows the calendar cannot place ---------------------------------

    def test_a_borrow_outside_every_term_is_counted_and_labelled(self):
        # 平台还没配学期日历时，借用照样发生。静默丢掉会缩小分母，把每个
        # 阶段的占比悄悄放大，所以它必须自己占一行并被说出来。
        category = self.make_category('无学期日历')
        for _ in range(MIN_SAMPLE_SIZE + 1):
            self.make_borrow(category=category, created_at=self.at_midnight(self.today))

        report = self.report()

        self.assertEqual(report['borrow_count'], MIN_SAMPLE_SIZE + 1)
        self.assertFalse(report['has_calendar'])
        self.assertEqual(report['unplaced_count'], MIN_SAMPLE_SIZE + 1)
        self.assertEqual(report['covered_count'], 0)
        self.assertEqual(report['covered_count'], 0)
        row = self.borrows_in_phase(report, NO_TERM_KEY)
        self.assertEqual(row['phase_label'], '无学期日历')
        self.assertEqual(row['borrow_count'], MIN_SAMPLE_SIZE + 1)
        self.assertIn('覆盖范围之外', report['summary'])

    def test_a_borrow_in_a_gap_between_phases_is_its_own_bucket(self):
        # 运营可以故意留一段不标阶段，那是配置状态不是数据错误。
        term = self.make_term()
        term.phases.all().delete()
        from .models import AcademicPhase
        AcademicPhase.objects.create(
            term=term, phase='regular', start_offset=0, end_offset=5,
        )

        category = self.make_category('阶段空隙')
        self.make_borrow(category=category, created_at=self.at_midnight(term.starts_on + timedelta(days=20)))

        report = self.report()
        row = self.borrows_in_phase(report, UNASSIGNED_PHASE_KEY)

        self.assertEqual(row['borrow_count'], 1)
        self.assertEqual(row['phase_label'], '未划分阶段')
        self.assertEqual(report['unplaced_count'], 1)

    def test_the_synthetic_buckets_never_compete_for_the_conclusion(self):
        # 「无学期日历」描述的是覆盖率，不是节律，不能出现在「最忙阶段」里。
        self.make_term()
        category = self.make_category('合成桶不参赛')
        for index in range(8):
            self.make_borrow(category=category, created_at=self.at_midnight(self.today))
        unplaced_category = self.make_category('落在日历外')
        for index in range(6):
            self.make_borrow(
                category=unplaced_category,
                created_at=self.at_midnight(self.today - timedelta(days=500)),
            )

        report = self.report(days=900)

        from .borrow_rhythm import _usable_phases
        usable = _usable_phases(report['rows'])

        self.assertEqual(
            [row['phase_key'] for row in usable],
            ['regular'],
        )
        self.assertIn('覆盖范围之外', report['summary'])
        self.assertNotIn('无学期日历日均', report['summary'])

    # -- what the cross must never do --------------------------------------

    def test_the_module_never_reaches_back_into_the_escalation_ladder(self):
        # 考试周看起来风险高，是给人判断的事实，不是提前催收的许可。催收
        # 时机是「同学的东西最多能丢多久」的策略，不该被一个统计数字推着走。
        self.make_term()
        category = self.make_category('不碰催收')
        for index in range(6):
            self.make_borrow(
                category=category,
                overdue_days=9,
                created_at=self.at_midnight(self.today - timedelta(days=10 + index)),
            )
        escalate_overdue_borrows(now=self.now)

        before = dict(ESCALATION_SCHEDULE)
        report = self.report()

        self.assertEqual(dict(ESCALATION_SCHEDULE), before)
        # 交叉表本身不改写任何催收行：等级和升级次数只由催收阶梯推进。
        self.assertEqual(
            sorted(row['level_three_count'] for row in report['rows']),
            sorted(row['level_three_count'] for row in report['rows']),
        )
        self.assertNotIn('escalate', report['summary'])

    def test_the_empty_history_reports_rather_than_raising(self):
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['borrow_count'], 0)
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['summary'], '周期内没有借用订单，学期节律交叉需要先有借用发生。')

    def test_the_phase_order_covers_every_configured_phase(self):
        self.assertEqual(PHASE_ORDER, ('registration', 'regular', 'exam', 'graduation', 'holiday'))
        self.assertEqual(SYNTHETIC_PHASE_KEYS, (NO_TERM_KEY, UNASSIGNED_PHASE_KEY))

    def test_a_closed_case_reports_how_long_it_took_from_the_rung(self):
        # 闭环时长从真正发生的那一级催收起算，和风险分层同一口径。
        self.make_term()
        category = self.make_category('闭环时长')
        # 借用发生在九天前，到期日比借用日晚五天，催收跑在 now：到期日已过
        # 九天，跨过第三级的 7 天门槛。
        order = self.make_borrow(
            category=category, overdue_days=9,
            created_at=self.at_midnight(self.today - timedelta(days=9)),
        )
        escalate_overdue_borrows(now=self.now)

        from listings.models import BorrowReturnEscalation
        self.assertTrue(
            BorrowReturnEscalation.objects.filter(order=order).exists(),
            '催收阶梯应该已经为这笔逾期借用建行，否则闭环时长无从量起',
        )

        from listings.borrow_escalation import close_escalation
        closed = close_escalation(order, now=self.now + timedelta(days=3))
        self.assertEqual(closed, 1)

        row = self.borrows_in_phase(self.report(), 'regular')

        self.assertEqual(row['resolved_count'], 1)
        self.assertEqual(row['open_count'], 0)
        self.assertLessEqual(row['average_close_days'], 3.1)
