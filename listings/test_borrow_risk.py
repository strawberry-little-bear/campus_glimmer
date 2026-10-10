# -*- coding: utf-8 -*-
"""Cover the borrow risk stratification built for the operations board.

The stratification exists to answer a question the escalation ladder and the
governance queue both leave open: is one category of item structurally harder
to get back, or is this a run of unlucky students? That question is only
answerable if the numbers are computed on the right population, so most of
these tests are about who is counted rather than about the arithmetic.

Two things must hold. First, borrows that were never escalated still belong in
the denominator - a table that only lists escalated orders would show a 100%
escalation rate in every group and be worse than nothing. Second, a group with
a handful of borrows must not be presented as a finding, because 1 overdue tent
out of 2 borrows is 50% and means nothing.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .borrow_escalation import escalate_overdue_borrows
from .borrow_risk import DEPOSIT_BANDS, MIN_SAMPLE_SIZE, build_borrow_risk

from .models import Category, CampusLocation, Item, Order


class BorrowRiskStratificationTests(TestCase):
    """Which borrows get counted, and how the two stratifications differ."""

    def setUp(self):
        self.seller = User.objects.create_user(username='risk-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='risk-borrower', password='safe-password-123')
        self.now = timezone.now()

    def make_category(self, name):
        return Category.objects.create(name=name)

    def make_location(self, category):
        """A location unique per borrow, since the name is unique."""
        self.location_serial = getattr(self, 'location_serial', 0) + 1
        return CampusLocation.objects.create(
            name=f'{category.name}地点{self.location_serial}',
        )

    def make_borrow(self, *, category, deposit_amount='100.00', overdue_days=None, borrower=None):
        """One borrow order, optionally sitting overdue by a number of days."""
        item = Item.objects.create(
            title=f'{category.name}测试物',
            description='用于借用逾期风险分层测试',
            trade_mode='borrow',
            price='60.00',
            deposit_amount=deposit_amount,
            borrow_days=14,
            category=category,
            location=self.make_location(category),
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
            due_at = self.now - timedelta(days=overdue_days)
            Order.objects.filter(pk=order.pk).update(
                created_at=due_at - timedelta(days=14), return_due_at=due_at,
            )
            order.refresh_from_db()
        return order

    def report(self, days=120):
        # auto_now_add writes the real insert time, which is later than the
        # self.now captured in setUp; the window upper bound has to sit after
        # the rows or every borrow would fall outside the period.
        return build_borrow_risk(days=days, now=self.now + timedelta(minutes=5))

    def test_borrows_that_were_never_escalated_stay_in_the_denominator(self):
        # 这一条是整个分层有意义的前提：没被催收的借用必须计入分母，
        # 否则每一组的升级率都会变成 100%，分层就白做了。
        category = self.make_category('计算分母分类')
        for index in range(3):
            self.make_borrow(category=category, borrower=self.borrower)
        # 同一件商品不能被同一个人反复借走，所以另借一件给第二个同学。

        report = self.report()

        row = report['category_rows'][0]
        self.assertEqual(row['key'], '计算分母分类')
        self.assertEqual(row['borrow_count'], 3)
        self.assertEqual(row['escalated_count'], 0)
        self.assertEqual(row['escalation_rate'], 0)
        self.assertEqual(row['level_three_rate'], 0)

    def test_escalated_borrow_is_counted_once_in_its_own_group(self):
        category = self.make_category('计入分组分类')
        self.make_borrow(category=category, overdue_days=8)
        escalate_overdue_borrows(now=self.now)

        row = self.report()['category_rows'][0]

        self.assertEqual(row['borrow_count'], 1)
        self.assertEqual(row['escalated_count'], 1)
        self.assertEqual(row['level_three_count'], 1)
        self.assertEqual(row['level_three_rate'], 100.0)

    def test_the_group_that_stands_out_leads_the_table(self):
        # 一个分类全拖到第三级，另一个一次都没催收：表格要把前者排在最前，
        # 否则运营得自己翻完整张表才知道该看哪一行。
        risky = self.make_category('高风险分类')
        calm = self.make_category('低风险分类')
        self.make_borrow(category=risky, overdue_days=9)
        escalate_overdue_borrows(now=self.now)
        for _ in range(3):
            self.make_borrow(category=calm)

        rows = self.report()['category_rows']

        self.assertEqual(rows[0]['key'], '高风险分类')
        self.assertEqual(rows[0]['level_three_rate'], 100.0)
        self.assertEqual(rows[1]['key'], '低风险分类')
        self.assertEqual(rows[1]['level_three_rate'], 0)


    def test_deposit_bands_split_the_same_borrows_by_their_deposit(self):
        # 同一批借用单在押金维度上必须被切开，而不是重复计一次。
        category = self.make_category('押金分档分类')
        self.make_borrow(category=category, deposit_amount='30.00', overdue_days=9)
        self.make_borrow(category=category, deposit_amount='120.00')
        self.make_borrow(category=category, deposit_amount='260.00')
        escalate_overdue_borrows(now=self.now)

        rows = {row['key']: row for row in self.report()['deposit_rows']}

        self.assertEqual(rows['押金 50 元以下']['borrow_count'], 1)
        self.assertEqual(rows['押金 50 元以下']['level_three_count'], 1)
        self.assertEqual(rows['押金 100-200 元']['borrow_count'], 1)
        self.assertEqual(rows['押金 100-200 元']['level_three_count'], 0)
        self.assertEqual(rows['押金 200 元以上']['borrow_count'], 1)

    def test_a_group_too_small_to_read_is_marked_as_such(self):
        # 2 笔里 1 笔拖到第三级是 50%，这个数字不能拿去当结论。
        category = self.make_category('样本不足分类')
        self.make_borrow(category=category, overdue_days=9)
        self.make_borrow(category=category)
        escalate_overdue_borrows(now=self.now)

        row = self.report()['category_rows'][0]

        self.assertEqual(row['borrow_count'], 2)
        self.assertTrue(row['is_small_sample'])
        self.assertEqual(self.report()['summary'], '周期内借用单量不足，暂时无法按分类或押金额度比较逾期风险。')

    def test_a_closed_case_reports_how_long_it_took_from_the_third_rung(self):
        # 闭环时长从第三级那一刻起算，而不是从到期日：在 L1 等一周的同学，
        # 不该被记成运营花了一周处理。
        category = self.make_category('闭环时长分类')
        order = self.make_borrow(category=category, overdue_days=9)
        escalate_overdue_borrows(now=self.now)

        from listings.borrow_escalation import close_escalation
        close_escalation(order, now=self.now + timedelta(days=3))

        row = self.report()['category_rows'][0]

        self.assertEqual(row['resolved_count'], 1)
        self.assertEqual(row['open_count'], 0)
        self.assertIsNotNone(row['average_close_days'])
        self.assertLessEqual(row['average_close_days'], 3.1)

    def test_sales_are_not_counted_as_borrows(self):
        # 只统计借用单：把普通买卖算进来会让所有比率都被稀释。
        category = self.make_category('普通买卖分类')
        item = Item.objects.create(
            title='普通买卖测试物', description='不是借用',
            trade_mode='sale', price='60.00', deposit_amount='0.00',
            borrow_days=0, category=category,
            location=CampusLocation.objects.create(name='买卖地点'),
            condition='9成新', seller=self.seller,
        )
        Order.objects.create(
            item=item, buyer=self.borrower, seller=self.seller,
            agreed_price='60.00', deposit_amount='0.00', status='completed',
        )

        report = self.report()

        self.assertEqual(report['borrow_count'], 0)
        self.assertFalse(report['has_data'])

    def test_orders_outside_the_window_are_left_out(self):
        category = self.make_category('窗口外分类')
        order = self.make_borrow(category=category, overdue_days=9)
        # 把借用时间推到窗口之外，窗口内就应该是空的。
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=400))
        escalate_overdue_borrows(now=self.now)

        report = self.report(days=30)

        self.assertEqual(report['borrow_count'], 0)
        self.assertFalse(report['has_data'])

    def test_the_summary_names_the_group_that_stands_out(self):
        risky = self.make_category('结论高风险分类')
        calm = self.make_category('结论低风险分类')
        for index in range(5):
            self.make_borrow(category=risky, overdue_days=9)
        escalate_overdue_borrows(now=self.now)
        for index in range(5):
            self.make_borrow(category=calm)

        summary = self.report()['summary']

        self.assertIn('结论高风险分类', summary)
        self.assertIn('第三级', summary)

    def test_deposit_bands_are_ordered_and_cover_every_amount(self):
        # 档位必须连续覆盖，否则一笔押金会从表里凭空消失。
        self.assertEqual(DEPOSIT_BANDS[0][0], 0)
        for lower, upper, _label in DEPOSIT_BANDS[:-1]:
            following = DEPOSIT_BANDS[DEPOSIT_BANDS.index((lower, upper, _label)) + 1]
            self.assertEqual(following[0], upper)
        self.assertIsNone(DEPOSIT_BANDS[-1][1])

    def test_min_sample_size_is_positive(self):
        self.assertGreater(MIN_SAMPLE_SIZE, 0)

