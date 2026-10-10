# -*- coding: utf-8 -*-
"""Cover the overdue borrow escalation ladder and the board built from it.

The cases worth covering are the ones the ladder exists for. The overdue notice
fires once, so a borrow left unreturned for weeks is silent unless something
escalates it - that is what this module adds, and each test below pins down one
rung of it. Just as important are the things it must not do: it must not cancel
the order, must not touch the deposit, must not turn a slow return into a
permanent mark on a classmate, and must not chase the same borrower twice in one
evening. Escalating to the borrower twice would add no information, so level two
is where the lender is finally told, and level three is where the case leaves
the private channel for the governance queue.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .borrow_escalation import (
    DEFAULT_LOOKBACK_DAYS,
    ESCALATION_SCHEDULE,
    MAX_ESCALATION_LEVEL,
    MIN_ESCALATION_INTERVAL,
    build_borrow_escalation_report,
    close_escalation,
    escalate_overdue_borrows,
)

from .models import (
    BorrowReturnEscalation, Category, CampusLocation, DeliveryConfirmation, Item,
    Notification, Order, TaskRun,
)


class EscalationLadderTests(TestCase):
    """Each rung of the ladder, and the boundary of each one."""

    def setUp(self):
        self.seller = User.objects.create_user(username='esc-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='esc-borrower', password='safe-password-123')
        self.category = Category.objects.create(name='催收测试分类')
        self.location = CampusLocation.objects.create(name='催收测试点', building='东校区')
        self.item = Item.objects.create(
            title='催收测试充电宝',
            description='用于借用逾期催收测试',
            trade_mode='borrow',
            price='88.00',
            deposit_amount='120.00',
            borrow_days=14,
            category=self.category,
            location=self.location,
            condition='9成新',
            seller=self.seller,
        )
        self.now = timezone.now()

    def make_borrowed_order(self, *, overdue_days):
        """One borrowed order whose due date sits `overdue_days` in the past."""
        order = Order.objects.create(
            item=self.item,
            buyer=self.borrower,
            seller=self.seller,
            agreed_price='0.00',
            deposit_amount='120.00',
            status='borrowed',
        )
        due_at = self.now - timedelta(days=overdue_days)
        Order.objects.filter(pk=order.pk).update(
            created_at=due_at - timedelta(days=14), return_due_at=due_at,
        )
        order.refresh_from_db()
        return order

    def titles_for(self, order, recipient):
        return list(
            Notification.objects.filter(order=order, recipient=recipient)
            .values_list('title', flat=True)
        )

    def test_level_one_asks_only_the_borrower(self):
        order = self.make_borrowed_order(overdue_days=1)

        result = escalate_overdue_borrows(now=self.now)

        self.assertEqual(result['level_one'], 1)
        self.assertEqual(result['escalated'], 1)
        escalation = order.borrow_escalation
        self.assertEqual(escalation.escalation_level, 1)
        self.assertEqual(escalation.escalation_count, 1)
        # 第一级只告诉借用方：出借方本来就知道东西是自己的、也已经逾期了。
        self.assertEqual(self.titles_for(order, self.borrower), ['借用已逾期，请尽快归还'])
        self.assertEqual(self.titles_for(order, self.seller), [])

    def test_level_two_tells_both_sides(self):
        order = self.make_borrowed_order(overdue_days=3)

        result = escalate_overdue_borrows(now=self.now)

        self.assertEqual(result['level_two'], 1)
        self.assertEqual(order.borrow_escalation.escalation_level, 2)
        for recipient in (self.borrower, self.seller):
            self.assertEqual(
                self.titles_for(order, recipient), ['借用逾期已进入平台记录'],
            )

    def test_level_three_hands_the_case_to_the_governance_queue(self):
        order = self.make_borrowed_order(overdue_days=7)

        result = escalate_overdue_borrows(now=self.now)

        self.assertEqual(result['level_three'], 1)
        self.assertEqual(order.borrow_escalation.escalation_level, 3)
        self.assertTrue(order.borrow_escalation.note.startswith('已上报治理工作台'))
        # 第三级本身离开双方私域，不再补发第三轮私信；但借出方在第二级才第一次
        # 被告知，跨级跳上来时必须补上这一声。
        for recipient in (self.borrower, self.seller):
            self.assertEqual(
                self.titles_for(order, recipient), ['借用逾期已进入平台记录'],
            )

    def test_a_week_old_overdue_is_not_chased_more_than_once(self):
        order = self.make_borrowed_order(overdue_days=30)

        escalate_overdue_borrows(now=self.now)
        second = escalate_overdue_borrows(now=self.now + timedelta(hours=12))

        # 阶梯封顶在第三级，继续逾期只是让治理队列继续等，不会再发一轮消息。
        self.assertEqual(second['escalated'], 0)
        self.assertEqual(order.borrow_escalation.escalation_count, 1)
        self.assertEqual(order.borrow_escalation.escalation_level, 3)

    def test_fresh_overdue_stays_quiet(self):
        self.make_borrowed_order(overdue_days=0)

        result = escalate_overdue_borrows(now=self.now)

        self.assertEqual(result['escalated'], 0)
        self.assertEqual(result['tracked'], 0)
        self.assertEqual(BorrowReturnEscalation.objects.count(), 0)

    def test_a_skipped_rung_is_not_replayed(self):
        # 漏跑一天之后直接追到第三级，不补发第一级那声「你忘了」。
        order = self.make_borrowed_order(overdue_days=9)

        escalate_overdue_borrows(now=self.now)

        self.assertEqual(order.borrow_escalation.escalation_level, 3)
        self.assertEqual(order.borrow_escalation.escalation_count, 1)
        self.assertEqual(
            self.titles_for(order, self.borrower), ['借用逾期已进入平台记录'],
        )

    def test_two_escalations_are_spaced_by_a_day(self):
        order = self.make_borrowed_order(overdue_days=3)
        escalate_overdue_borrows(now=self.now)

        # 刚升到第二级，一小时后仍不够 24 小时间隔，不能立刻再升。
        same_day = escalate_overdue_borrows(now=self.now + timedelta(hours=1))
        self.assertEqual(same_day['escalated'], 0)

        later = escalate_overdue_borrows(now=self.now + timedelta(days=5))
        self.assertEqual(later['escalated'], 1)
        self.assertEqual(order.borrow_escalation.escalation_level, 3)

    def test_a_returned_borrow_is_closed_and_never_escalated_again(self):
        order = self.make_borrowed_order(overdue_days=3)
        escalate_overdue_borrows(now=self.now)
        before = order.borrow_escalation.escalation_count

        closed = close_escalation(order, now=self.now + timedelta(hours=2))
        after = escalate_overdue_borrows(now=self.now + timedelta(days=10))

        self.assertEqual(closed, 1)
        self.assertEqual(after['escalated'], 0)
        escalation = BorrowReturnEscalation.objects.get(order=order)
        self.assertTrue(escalation.is_resolved)
        self.assertEqual(escalation.resolved_at, self.now + timedelta(hours=2))
        # 关闭不是删除：等级和次数都留着，时间线仍然可以回看。
        self.assertEqual(escalation.escalation_level, 2)
        self.assertEqual(escalation.escalation_count, before)
        self.assertEqual(escalation.get_escalation_level_display(), '已通知双方')

    def test_close_escalation_is_idempotent(self):
        order = self.make_borrowed_order(overdue_days=3)
        escalate_overdue_borrows(now=self.now)

        self.assertEqual(close_escalation(order, now=self.now), 1)
        self.assertEqual(close_escalation(order, now=self.now), 0)

    def test_escalation_never_decides_the_outcome(self):
        order = self.make_borrowed_order(overdue_days=10)
        deposit_before = order.deposit_amount

        escalate_overdue_borrows(now=self.now)

        order.refresh_from_db()
        # 押金由两个同学线下结算，平台扣款会发明一个双方都没同意的金融关系。
        self.assertEqual(order.deposit_amount, deposit_before)
        self.assertEqual(order.status, 'borrowed')
        self.assertEqual(self.item.status, 'available')

    def test_only_orders_past_the_due_date_are_picked_up(self):
        order = self.make_borrowed_order(overdue_days=0)
        Order.objects.filter(pk=order.pk).update(return_due_at=self.now + timedelta(days=3))

        result = escalate_overdue_borrows(now=self.now)

        self.assertEqual(result['escalated'], 0)
        self.assertEqual(BorrowReturnEscalation.objects.count(), 0)

    def test_old_orders_outside_the_lookback_are_left_alone(self):
        self.make_borrowed_order(overdue_days=DEFAULT_LOOKBACK_DAYS + 5)

        result = escalate_overdue_borrows(now=self.now, lookback_days=7)

        self.assertEqual(result['escalated'], 0)
        self.assertEqual(BorrowReturnEscalation.objects.count(), 0)

    def test_schedule_and_cap_are_the_documented_ladder(self):
        self.assertEqual(sorted(ESCALATION_SCHEDULE), [1, 2, 3])
        self.assertEqual(MAX_ESCALATION_LEVEL, 3)
        self.assertEqual(MIN_ESCALATION_INTERVAL, timedelta(hours=24))


class EscalationReportTests(TestCase):
    """The board only lists escalations that actually happened."""

    def setUp(self):
        self.seller = User.objects.create_user(username='esc-board-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='esc-board-borrower', password='safe-password-123')
        self.category = Category.objects.create(name='催收看板分类')
        self.location = CampusLocation.objects.create(name='催收看板地点')
        self.item = Item.objects.create(
            title='催收看板台灯',
            description='用于借用逾期催收看板测试',
            trade_mode='borrow',
            price='30.00',
            deposit_amount='60.00',
            borrow_days=7,
            category=self.category,
            location=self.location,
            condition='8成新',
            seller=self.seller,
        )
        self.now = timezone.now()

    def make_order(self, *, overdue_days):
        order = Order.objects.create(
            item=self.item,
            buyer=self.borrower,
            seller=self.seller,
            agreed_price='0.00',
            status='borrowed',
        )
        due_at = self.now - timedelta(days=overdue_days)
        Order.objects.filter(pk=order.pk).update(
            created_at=due_at - timedelta(days=7), return_due_at=due_at,
        )
        order.refresh_from_db()
        return order

    def test_empty_board_says_there_is_nothing_to_collect(self):
        data = build_borrow_escalation_report(now=self.now)

        self.assertFalse(data['has_data'])
        self.assertEqual(data['rows'], [])
        self.assertIn('没有需要催收', data['summary_text'])

    def test_board_lists_open_and_closed_escalations_separately(self):
        escalated = self.make_order(overdue_days=3)
        escalate_overdue_borrows(now=self.now)
        close_escalation(escalated, now=self.now + timedelta(hours=5))

        data = build_borrow_escalation_report(now=self.now + timedelta(hours=6))

        self.assertTrue(data['has_data'])
        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(data['summary']['open_count'], 0)
        self.assertEqual(data['summary']['resolved_count'], 1)
        self.assertIn('已闭环', data['summary_text'])

    def test_a_late_but_never_escalated_return_is_not_a_case(self):
        # 迟还但从未升级过的订单不填充看板，否则运营要读一堆不需要处理的行。
        self.make_order(overdue_days=2)
        data = build_borrow_escalation_report(now=self.now)

        self.assertFalse(data['has_data'])
        self.assertEqual(data['summary']['period_count'], 0)

    def test_summary_leads_with_the_most_urgent_fact(self):
        self.make_order(overdue_days=9)
        escalate_overdue_borrows(now=self.now)

        data = build_borrow_escalation_report(now=self.now)

        self.assertEqual(data['summary']['level_three_count'], 1)
        self.assertEqual(data['summary']['max_overdue_days'], 9)
        self.assertIn('已上报治理队列', data['summary_text'])


class EscalationIntegrationTests(TestCase):
    """The ladder has to be wired into the places a real run touches.

    These are the joins a module test cannot catch: the return endpoint must
    close the row, the scheduled command must actually walk the ladder, and the
    board must render on an empty database. A module that passes its own unit
    tests but is never called is exactly the kind of dead code the command
    ledger tests exist to catch.
    """

    def setUp(self):
        self.seller = User.objects.create_user(username='esc-int-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='esc-int-borrower', password='safe-password-123')
        self.category = Category.objects.create(name='催收接入分类')
        self.location = CampusLocation.objects.create(name='催收接入地点', building='东校区')
        self.item = Item.objects.create(
            title='催收接入充电宝', description='用于借用逾期催收接入测试',
            trade_mode='borrow', price='88.00', deposit_amount='120.00',
            borrow_days=14, category=self.category, location=self.location,
            condition='9成新', seller=self.seller,
        )
        self.now = timezone.now()

    def make_borrowed_order(self, *, overdue_days):
        order = Order.objects.create(
            item=self.item, buyer=self.borrower, seller=self.seller,
            agreed_price='0.00', deposit_amount='120.00', status='borrowed',
        )
        due_at = self.now - timedelta(days=overdue_days)
        Order.objects.filter(pk=order.pk).update(
            created_at=due_at - timedelta(days=14), return_due_at=due_at,
        )
        order.refresh_from_db()
        return order

    def test_confirm_return_closes_the_escalation(self):
        order = self.make_borrowed_order(overdue_days=4)
        escalate_overdue_borrows(now=self.now)
        self.assertTrue(order.borrow_escalation.is_open)

        # 走真实的归还确认入口，而不是直接调 close_escalation：接入点必须自己生效。
        # 借用方先登记，出借方再确认，双方都确认的那一刻才是归还闭环。
        self.client.login(username='esc-int-borrower', password='safe-password-123')
        response = self.client.post(reverse('confirm_return', args=[order.id]))
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.client.logout()
        self.client.login(username='esc-int-seller', password='safe-password-123')
        response = self.client.post(reverse('confirm_return', args=[order.id]))
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))

        escalation = BorrowReturnEscalation.objects.get(order=order)
        self.assertFalse(escalation.is_open)
        self.assertIsNotNone(escalation.resolved_at)
        # 关闭不是删除：等级和次数都留着，时间线仍然可以回看。
        self.assertEqual(escalation.escalation_level, 2)

    def test_return_confirmed_from_the_admin_also_closes_the_row(self):
        # 后台手动把订单改成已归还时，视图钩子不会触发；调度命令兜底关闭这一行，
        # 否则看板会一直把一笔已经还清的借用算成待办。
        order = self.make_borrowed_order(overdue_days=4)
        escalate_overdue_borrows(now=self.now)
        Order.objects.filter(pk=order.pk).update(
            status='returned', returned_at=self.now,
        )

        result = escalate_overdue_borrows(now=self.now + timedelta(hours=2))

        self.assertEqual(result['resolved'], 1)
        escalation = BorrowReturnEscalation.objects.get(order=order)
        self.assertFalse(escalation.is_open)

    def test_scheduled_command_records_the_escalation_in_the_ledger(self):
        self.make_borrowed_order(overdue_days=2)

        call_command('process_order_timeouts')

        row = TaskRun.objects.get(name='process_order_timeouts')
        self.assertEqual(row.status, 'succeeded')
        # 三级分布留在命令输出里，台账只记一个可横向比较的汇总数。
        self.assertEqual(row.metrics.get('marked'), 1)
        self.assertTrue(BorrowReturnEscalation.objects.filter(escalation_level=1).exists())

    def test_dashboard_renders_the_escalation_panel(self):
        staff = User.objects.create_user(
            username='esc-int-staff', password='safe-password-123', is_staff=True,
        )
        self.make_borrowed_order(overdue_days=9)
        escalate_overdue_borrows(now=self.now)
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '借用逾期催收阶梯')
        self.assertContains(response, '催收接入充电宝')
        report = response.context['borrow_escalation']
        self.assertEqual(report['summary']['open_count'], 1)

    def test_dashboard_renders_the_empty_panel_without_any_borrow(self):
        staff = User.objects.create_user(
            username='esc-int-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '当前没有需要催收的借用逾期')
        self.assertFalse(response.context['borrow_escalation']['has_data'])

