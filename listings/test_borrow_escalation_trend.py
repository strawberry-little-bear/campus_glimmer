# -*- coding: utf-8 -*-
"""Cover the cross-period comparison built on the borrow escalation snapshot.

The snapshot panel answers "how bad is it right now", which is the question an
operator asks first and the one they keep asking afterwards. These tests
therefore pin down the things that decide whether the comparison is a real
answer to "is it getting worse" or an artefact of how the windows were cut.

Three of them matter more than the rest. A period in which nothing was escalated
must still be counted, or both windows would show a 100% escalation rate and the
panel would report perfect stability on a queue that had tripled. A metric
measured against today rather than against the period it describes must not be
compared, because the previous window is always the older one and the number
would fall on its own. And close duration must start from the rung that actually
happened, because an order that waited a week at level one is not an order the
operator spent a week on.

The rest are boundaries: a metric that exists in only one window is reported as
new or gone rather than given a delta from zero, a window below the floor is
marked insufficient instead of compared, and nothing in this module reaches
back into the escalation ladder.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .borrow_escalation import ESCALATION_SCHEDULE, close_escalation, escalate_overdue_borrows
from .borrow_escalation_trend import (
    COMPARED_METRICS,
    TREND_DIRECTIONS,
    TREND_LABELS,
    build_borrow_escalation_trend,
)
from .borrow_risk import MIN_SAMPLE_SIZE
from .models import CampusLocation, Category, Item, Order


class BorrowEscalationTrendTests(TestCase):
    """Which borrows land in which window, and what the comparison may say."""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='trend-seller', password='safe-password-123',
        )
        self.borrower = User.objects.create_user(
            username='trend-borrower', password='safe-password-123',
        )
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)
        self.location_serial = 0
        self.category_serial = 0

    def make_category(self, name):
        self.category_serial += 1
        return Category.objects.create(name=f'{name}{self.category_serial}')

    def make_borrow(self, *, category, created_at, overdue_days=None):
        """One borrow order placed on a given instant, optionally overdue.

        `overdue_days` is signed: -9 means "falls due nine days after the
        borrow", which is the common case, while a positive number means it was
        already overdue when it was placed. Both windows are cut by the borrow's
        own timestamp, so the sign is what puts it in one period or the other.
        """
        self.location_serial += 1
        item = Item.objects.create(
            title=f'{category.name}测试物',
            description='用于借用催收跨周期对比测试',
            trade_mode='borrow',
            price='60.00',
            deposit_amount='100.00',
            borrow_days=14,
            category=category,
            location=CampusLocation.objects.create(
                name=f'趋势地点{self.location_serial}',
            ),
            condition='9成新',
            seller=self.seller,
        )
        order = Order.objects.create(
            item=item,
            buyer=self.borrower,
            seller=self.seller,
            agreed_price='0.00',
            deposit_amount='100.00',
            status='borrowed',
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        if overdue_days is not None:
            Order.objects.filter(pk=order.pk).update(
                return_due_at=created_at + timedelta(days=overdue_days),
            )
        order.refresh_from_db()
        return order

    def report(self, days=30):
        # The current window has to sit after the rows: auto_now_add writes the
        # real insert time, which is later than the self.now captured in setUp.
        return build_borrow_escalation_trend(days=days, now=self.now + timedelta(minutes=5))

    def metric(self, report, key):
        for row in report['metric_rows']:
            if row['key'] == key:
                return row
        return None

    # -- the population ----------------------------------------------------

    def test_borrows_that_were_never_escalated_stay_in_the_denominator(self):
        # 这一条决定了这个面板有没有意义：只看升级过的订单，两个周期都会是
        # 100% 升级率，差值恒为零，于是面板会在一个翻了三倍的队列上报完美稳定。
        category = self.make_category('计入分母')
        for index in range(MIN_SAMPLE_SIZE):
            self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=2),
            )

        report = self.report()

        self.assertEqual(report['current']['borrow_count'], MIN_SAMPLE_SIZE)
        self.assertEqual(report['current']['escalated_count'], 0)
        self.assertEqual(report['current']['escalation_rate'], 0)
        self.assertEqual(report['previous']['borrow_count'], 0)

    def test_a_case_is_counted_in_the_period_its_borrow_was_placed_in(self):
        # 分子分母必须用同一把时钟：升级行属于哪一周是调度决定的，借用属于哪
        # 一周是用户决定的。按升级行归期会把上一周期的尾巴拖进本周期。
        category = self.make_category('归期口径')
        # 借用发生在本周期，升级行在本周期之后才建。
        order = self.make_borrow(
            category=category,
            created_at=self.now - timedelta(days=2),
            overdue_days=-1,
        )
        escalate_overdue_borrows(now=self.now + timedelta(minutes=5))

        report = self.report()

        # 借用在本周期，升级也在本周期：两边都计入本周期。
        self.assertEqual(report['current']['borrow_count'], 1)
        self.assertEqual(report['current']['escalated_count'], 1)
        self.assertEqual(report['previous']['borrow_count'], 0)
        # 到期日晚于借用一天，一级催收已到期，但借用量不足样本量。
        self.assertFalse(report['has_sample'])
        self.assertEqual(order.status, 'borrowed')

    def test_the_escalation_rate_direction_names_the_queue_that_grew(self):
        # 借用量翻倍、升级率也翻倍时，必须报的是升级率在动，而不是借用量在动：
        # 后者只说明平台变热闹了，不说明队列变差了。
        category = self.make_category('方向判定')
        # 上一周期：五笔借用，全部升级，升级率 100%。
        for index in range(MIN_SAMPLE_SIZE):
            order = self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=45 + index),
                overdue_days=-9,
            )
            escalate_overdue_borrows(now=order.created_at + timedelta(days=10))
        # 本周期：五笔借用，都不升级，升级率 0%。
        for index in range(MIN_SAMPLE_SIZE):
            self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=2),
            )

        report = self.report(days=30)

        borrow_row = self.metric(report, 'borrow_count')
        rate_row = self.metric(report, 'escalation_rate')
        self.assertLess(
            report['current']['escalation_rate'], report['previous']['escalation_rate'],
        )
        self.assertEqual(rate_row['direction'], 'falling')
        # 借用量本身不是队列变差的证据，不出现在结论文本里。
        self.assertNotIn('周期内借用', report['summary'])
        self.assertEqual(borrow_row['current'], borrow_row['previous'])

    # -- the close duration ------------------------------------------------

    def test_close_duration_starts_from_the_rung_that_actually_happened(self):
        # 在等级一坐了一周的订单，不该被记成运营花了一周处理。否则队列越空，
        # 闭环时长越难看，面板会把"没人管"显示成"处理得慢"。
        category = self.make_category('闭环起算')
        order = self.make_borrow(
            category=category,
            created_at=self.now - timedelta(days=45),
            overdue_days=-9,
        )
        # 到期后第 3 天才真正发出第一级催收，再 2 天后闭环：从等级起算是 2 天，
        # 从到期日算是 5 天。
        escalate_overdue_borrows(now=order.return_due_at + timedelta(days=3))
        close_escalation(order, now=order.return_due_at + timedelta(days=5))

        row = self.report()['previous']

        self.assertEqual(row['resolved_count'], 1)
        self.assertIsNotNone(row['average_close_days'])
        self.assertLessEqual(row['average_close_days'], 2.1)
        self.assertGreaterEqual(row['average_close_days'], 1.9)

    def test_a_period_in_which_nothing_closed_has_no_delta_from_zero(self):
        # 上一周期什么都没闭环时，本期有时长不该显示成"改善"——那是拿 0 当分母，
        # 而 0 的含义是"还不知道"，不是"不花时间"。
        category = self.make_category('无闭环基准')
        order = self.make_borrow(
            category=category,
            created_at=self.now - timedelta(days=45),
            overdue_days=-9,
        )
        escalate_overdue_borrows(now=self.now)
        # 本周期闭环一笔，上一周期一笔都没闭环。
        current_order = self.make_borrow(
            category=category,
            created_at=self.now - timedelta(days=2),
            overdue_days=-1,
        )
        escalate_overdue_borrows(now=current_order.return_due_at + timedelta(days=1))
        close_escalation(
            current_order, now=current_order.return_due_at + timedelta(days=2),
        )

        row = self.metric(self.report(), 'average_close_days')

        self.assertIsNone(row['previous'])
        self.assertEqual(row['direction'], 'new')

    def test_how_overdue_the_open_cases_are_is_never_compared(self):
        # 未闭环订单的逾期天数是对着"今天"算的，上一周期因此永远是更老的那个，
        # 这个指标只会自己往下掉。它是快照面板的答案，不是同期的答案。
        category = self.make_category('不对比逾期')
        keys = {key for key, _label in COMPARED_METRICS}
        self.assertNotIn('max_overdue_days', keys)
        # 队列里既有上一周期也有本周期的未闭环逾期订单。
        for index in range(MIN_SAMPLE_SIZE):
            self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=45 + index),
                overdue_days=-9,
            )
        for index in range(MIN_SAMPLE_SIZE):
            self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=2),
                overdue_days=-9,
            )

        report = self.report(days=30)

        for figures in (report['current'], report['previous']):
            self.assertNotIn('max_overdue_days', figures)
        self.assertTrue(report['has_sample'])

    # -- sparse windows ----------------------------------------------------

    def test_a_window_below_the_floor_is_not_compared(self):
        # 3 笔借用算出来的差值是噪声。面板不能成为"隔壁表里算太薄、这里刚好"
        # 的那种地方。
        category = self.make_category('门槛对齐')
        self.assertEqual(MIN_SAMPLE_SIZE, 5)

        for index in range(3):
            self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=2),
            )
        for index in range(MIN_SAMPLE_SIZE):
            self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=45 + index),
            )

        report = self.report()

        self.assertFalse(report['has_sample'])
        self.assertIn('单量不足', report['summary'])

    def test_the_direction_vocabulary_matches_the_search_trend_panel(self):
        # 两个面板并排摆在同一张看板上，读者在一处学会"上升"是十分位，不该在
        # 另一处重新学一个数。
        self.assertEqual(
            TREND_DIRECTIONS,
            ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient'),
        )
        self.assertEqual(TREND_LABELS['rising'], '较上期上升')
        self.assertEqual(TREND_LABELS['insufficient'], '样本不足')

    def test_the_compared_metrics_cover_both_rate_and_duration(self):
        # 只比率会漏掉"处理变慢"，只时长会漏掉"变频繁"，两边的键都要在。
        keys = {key for key, _label in COMPARED_METRICS}
        for expected in (
            'escalation_rate', 'level_three_rate',
            'average_close_days', 'max_close_days',
        ):
            self.assertIn(expected, keys)

    # -- what the comparison must never do ---------------------------------

    def test_the_module_never_reaches_back_into_the_escalation_ladder(self):
        # 上一周期更难看，是给人判断的事实，不是提前催收的许可。
        category = self.make_category('不碰阶梯')
        for index in range(MIN_SAMPLE_SIZE):
            order = self.make_borrow(
                category=category,
                created_at=self.now - timedelta(days=45 + index),
                overdue_days=-9,
            )
            escalate_overdue_borrows(now=order.return_due_at + timedelta(days=10))

        before = dict(ESCALATION_SCHEDULE)
        report = self.report()

        self.assertEqual(dict(ESCALATION_SCHEDULE), before)
        # 摘要可以谈论催收时效，因为那就是它在报告的东西；但它不能谈论"提前
        # 催收"或"调整阶梯"，那属于改动而不是报告。
        self.assertIn('催收时效', report['summary'])
        for forbidden in ('提前催收', '调整阶梯', '改为', '立即催收'):
            self.assertNotIn(forbidden, report['summary'])
        self.assertGreater(report['previous']['escalated_count'], 0)

    def test_the_empty_history_reports_rather_than_raising(self):
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertEqual(report['current']['borrow_count'], 0)
        self.assertEqual(report['previous']['borrow_count'], 0)
        self.assertIn('都没有借用订单', report['summary'])

    def test_the_two_windows_are_bounded_by_local_midnight(self):
        # 两个窗口都以本地午夜为界，和搜索趋势同一个函数：同一笔借用在这里
        # 被计入，在那里也必须被计入。
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
