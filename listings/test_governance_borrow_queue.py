# -*- coding: utf-8 -*-
"""Cover the borrow queue inside the governance workbench.

The borrow queue exists because of a promise, not a feature request:
``_notify_level_three`` writes that the governance workbench reads cases at
``EscalationLevel >= 3``, and for a while that sentence was simply untrue -
the aggregation only ever looked at reports, disputes and incidents, so a
case the workbench supposedly owned was invisible in it. The tests below pin
the promise down: a level-three borrow must show up, must not show up one
rung earlier, and must stop counting the moment the item comes back.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .borrow_escalation import escalate_overdue_borrows
from .governance_sla import SLA_TARGETS, build_governance_sla
from .models import (
    BorrowReturnEscalation, Category, CampusLocation, Item, Order,
)


class BorrowQueueCaseTests(TestCase):
    """Which borrows become governance cases, and from what moment."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='bq-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(username='bq-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='bq-borrower', password='safe-password-123')
        self.category = Category.objects.create(name='催收入队分类')
        self.location = CampusLocation.objects.create(name='催收入队地点')
        self.now = timezone.now()

    def make_borrowed_order(self, *, overdue_days):
        """One borrowed order whose due date sits `overdue_days` in the past."""
        item = Item.objects.create(
            title='入队测试充电宝', description='用于测试借用催收入队。',
            trade_mode='borrow', price='88.00', deposit_amount='120.00',
            borrow_days=14, category=self.category, location=self.location,
            condition='9成新', seller=self.seller,
        )
        order = Order.objects.create(
            item=item, buyer=self.borrower, seller=self.seller,
            agreed_price='0.00', deposit_amount='120.00', status='borrowed',
        )
        due_at = self.now - timedelta(days=overdue_days)
        Order.objects.filter(pk=order.pk).update(
            created_at=due_at - timedelta(days=14), return_due_at=due_at,
        )
        order.refresh_from_db()
        return order

    def escalate_to_level_three(self, order):
        """Run the ladder until this order reaches the third rung."""
        # 等级按逾期时长推导，一次运行就能直接追到当前该在的那一级。
        result = escalate_overdue_borrows(now=self.now)
        self.assertEqual(result["level_three"], 1)
        return result

    def borrow_queue(self, days=30):
        rows = {
            row['kind']: row
            for row in build_governance_sla(days=days, now=self.now)['queues']
        }
        return rows['borrow']

    def test_a_level_three_borrow_shows_up_in_the_workbench(self):
        # 这是本模块存在的理由：三级催收说"已上报治理队列"，工作台里就必须能看见它。
        order = self.make_borrowed_order(overdue_days=8)
        self.escalate_to_level_three(order)

        queue = self.borrow_queue()

        self.assertEqual(queue["open"], 1)
        self.assertEqual(queue["total"], 1)
        self.assertEqual(queue["label"], "借用逾期催收")
        self.assertEqual(queue["sla_label"], "7 天")

    def test_the_queue_measures_from_the_third_rung_not_the_first(self):
        # 同一笔订单逾期 20 天：L1 在第 1 天就建了行，但治理只该从 L3 那一刻开始算等待。
        order = self.make_borrowed_order(overdue_days=20)
        self.escalate_to_level_three(order)

        case = self.borrow_queue()["cases"][0]

        # 一次运行直接追到三级，所以入队时间就是现在，而不是 20 天前。
        self.assertEqual(case["created_at"], self.now)
        self.assertEqual(case["age_seconds"], 0)
        self.assertFalse(case["is_overdue"])

    def test_a_borrow_below_level_three_never_enters_the_queue(self):
        # 一级和二级还在两个同学之间，进工作台等于把一次慢还变成公开示众。
        order = self.make_borrowed_order(overdue_days=2)
        escalate_overdue_borrows(now=self.now)

        queue = self.borrow_queue()

        self.assertEqual(queue["total"], 0)
        self.assertEqual(queue["open"], 0)
        data = build_governance_sla(days=30, now=self.now)
        self.assertEqual(data["total_open"], 0)

    def test_a_returned_borrow_leaves_the_queue_and_counts_as_closed(self):
        # 归还即闭环：行还在，等级还在，但不再占用治理队列。
        order = self.make_borrowed_order(overdue_days=8)
        self.escalate_to_level_three(order)
        self.assertEqual(self.borrow_queue()["open"], 1)

        BorrowReturnEscalation.objects.filter(order=order).update(
            resolved_at=self.now,
        )

        queue = self.borrow_queue()
        self.assertEqual(queue["open"], 0)
        self.assertEqual(queue["closed"], 1)
        self.assertEqual(queue["overdue"], 0)

    def test_the_queue_goes_overdue_after_its_own_deadline(self):
        # 治理侧再给 7 天，合计逾期上限 14 天；超过才算治理没跟上。
        order = self.make_borrowed_order(overdue_days=8)
        self.escalate_to_level_three(order)
        BorrowReturnEscalation.objects.filter(order=order).update(
            last_escalated_at=self.now - timedelta(days=8),
        )

        queue = self.borrow_queue()

        self.assertEqual(queue["overdue"], 1)
        self.assertTrue(queue["cases"][0]["is_overdue"])

    def test_a_case_opened_before_the_period_is_not_hidden_by_the_window(self):
        # 统计窗口只有 1 天，这件三级催收发在 10 天前，不能从待办里消失。
        order = self.make_borrowed_order(overdue_days=18)
        self.escalate_to_level_three(order)
        BorrowReturnEscalation.objects.filter(order=order).update(
            last_escalated_at=self.now - timedelta(days=10),
        )

        data = build_governance_sla(days=1, now=self.now)

        self.assertEqual(data["total_open"], 1)
        self.assertEqual(data["total_overdue"], 1)

    def test_the_borrow_deadline_is_longer_than_a_dispute(self):
        # 借用催收不阻塞一次线下交付，时限比交易争议宽，比举报的时效更长。
        self.assertGreater(SLA_TARGETS['borrow'], SLA_TARGETS['dispute'])
        self.assertGreater(SLA_TARGETS['borrow'], SLA_TARGETS['report'])

    def test_an_empty_platform_counts_no_borrow_cases(self):
        data = build_governance_sla(days=30, now=self.now)

        queue = self.borrow_queue()
        self.assertEqual(queue["total"], 0)
        self.assertFalse(data["has_data"])
        self.assertEqual(data["total_overdue"], 0)


class BorrowQueueWorkbenchTests(TestCase):
    """The workbench page and the alert that points at it."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='bq-wb-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(username='bq-wb-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='bq-wb-borrower', password='safe-password-123')
        self.category = Category.objects.create(name='催收工作台分类')
        self.location = CampusLocation.objects.create(name='催收工作台地点')
        self.now = timezone.now()

    def make_level_three_borrow(self):
        item = Item.objects.create(
            title='工作台催收充电宝', description='用于测试工作台催收队列。',
            trade_mode='borrow', price='88.00', deposit_amount='120.00',
            borrow_days=14, category=self.category, location=self.location,
            condition='9成新', seller=self.seller,
        )
        order = Order.objects.create(
            item=item, buyer=self.borrower, seller=self.seller,
            agreed_price='0.00', deposit_amount='120.00', status='borrowed',
        )
        due_at = self.now - timedelta(days=8)
        Order.objects.filter(pk=order.pk).update(
            created_at=due_at - timedelta(days=14), return_due_at=due_at,
        )
        order.refresh_from_db()
        escalate_overdue_borrows(now=self.now)
        return order

    def test_workbench_lists_the_level_three_borrow(self):
        order = self.make_level_three_borrow()

        self.client.login(username="bq-wb-staff", password="safe-password-123")
        response = self.client.get(reverse("governance_workbench"))

        self.assertEqual(response.status_code, 200)
        kinds = {case["kind"] for case in response.context["cases"]}
        self.assertIn("borrow", kinds)
        self.assertContains(response, "借用逾期催收")
        self.assertContains(response, "工作台催收充电宝")
        self.assertContains(response, "查看订单时间线")
        case = next(c for c in response.context["cases"] if c["kind"] == "borrow")
        self.assertEqual(case["url"], f"/listings/order/{order.id}/")

    def test_workbench_offers_a_borrow_filter(self):
        self.make_level_three_borrow()

        self.client.login(username="bq-wb-staff", password="safe-password-123")
        response = self.client.get(reverse("governance_workbench"), {"kind": "borrow"})

        self.assertEqual(len(response.context["cases"]), 1)
        self.assertEqual(response.context["cases"][0]["kind"], "borrow")

    def test_borrow_case_never_shows_a_handle_button(self):
        # 平台不取消订单也不扣押金，界面上就不能出现一个假装能处置的按钮。
        self.make_level_three_borrow()

        self.client.login(username="bq-wb-staff", password="safe-password-123")
        response = self.client.get(reverse("governance_workbench"), {"kind": "borrow"})

        # 别处的"处理"链接与这个取舍无关，只数案件卡片里的实心按钮：
        # 三个原队列的待办按钮是 btn-primary，催收卡片只有描边的那一个。
        solid_buttons = response.content.decode().count('btn btn-sm btn-primary')
        self.assertEqual(solid_buttons, 0)
        self.assertContains(response, "查看订单时间线")

    def test_overdue_borrow_alerts_as_a_warning_not_a_critical(self):
        # 三件慢还的充电宝不该盖过一次约好的当面交付。
        self.make_level_three_borrow()
        BorrowReturnEscalation.objects.update(
            last_escalated_at=self.now - timedelta(days=8),
        )

        self.client.login(username="bq-wb-staff", password="safe-password-123")
        response = self.client.get(reverse("operations_dashboard"), {"days": 30})

        alerts = {a["key"]: a for a in response.context["operational_alerts"]}
        self.assertEqual(alerts["governance_overdue_borrow"]["severity"], "warning")
        self.assertEqual(
            alerts["governance_overdue_borrow"]["action_url_name"], "governance_workbench",
        )

    def test_alert_message_survives_a_queue_with_no_closed_case(self):
        # 借用队列可能一件都没闭环过，中位数是空的；拼出 None 比不说更糟。
        self.make_level_three_borrow()
        BorrowReturnEscalation.objects.update(
            last_escalated_at=self.now - timedelta(days=8),
        )

        self.client.login(username="bq-wb-staff", password="safe-password-123")
        response = self.client.get(reverse("operations_dashboard"), {"days": 30})

        alerts = {a["key"]: a for a in response.context["operational_alerts"]}
        self.assertIn("暂时无法给出处理时长中位数", alerts["governance_overdue_borrow"]["message"])
