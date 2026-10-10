# -*- coding: utf-8 -*-
"""Cover the borrow risk panel on the operations dashboard.

The panel is worth a test of its own because a stratification nobody can read
is the same as no stratification. What must survive rendering is the warning
that a group is too small to draw a conclusion from: a row showing 1 overdue
tent out of 2 borrows as a 50% third-level rate, with no hint that the sample
is tiny, is exactly the kind of number an operator would act on and regret.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .borrow_escalation import escalate_overdue_borrows
from .models import Category, CampusLocation, Item, Order


class BorrowRiskPanelTests(TestCase):
    """The stratification reaches the operations board, sample caveats and all."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='risk-panel-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(username='risk-panel-seller', password='safe-password-123')
        self.borrower = User.objects.create_user(username='risk-panel-borrower', password='safe-password-123')
        self.now = timezone.now()

    def make_borrow(self, *, name, deposit_amount='100.00', overdue_days=None):
        category = Category.objects.create(name=f'{name}分类')
        item = Item.objects.create(
            title=f'{name}测试物',
            description='用于借用逾期风险面板测试',
            trade_mode='borrow',
            price='60.00',
            deposit_amount=deposit_amount,
            borrow_days=14,
            category=category,
            location=CampusLocation.objects.create(name=f'{name}地点'),
            condition='9成新',
            seller=self.seller,
        )
        order = Order.objects.create(
            item=item, buyer=self.borrower, seller=self.seller,
            agreed_price='0.00', deposit_amount=deposit_amount, status='borrowed',
        )
        if overdue_days is not None:
            due_at = self.now - timedelta(days=overdue_days)
            Order.objects.filter(pk=order.pk).update(
                created_at=due_at - timedelta(days=14), return_due_at=due_at,
            )
            order.refresh_from_db()
        return order

    def test_the_panel_reaches_the_operations_board(self):
        self.make_borrow(name='面板高风险', overdue_days=9)
        escalate_overdue_borrows(now=self.now + timedelta(minutes=5))

        self.client.force_login(self.staff)
        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertIn('借用逾期风险分层', response.content.decode('utf-8'))
        self.assertIn('面板高风险分类', response.content.decode('utf-8'))

    def test_a_group_too_small_to_read_is_dimmed_not_hidden(self):
        # 样本不足的那一行仍然要显示，但必须带提示；直接藏起来会让人以为
        # 这个分类没有风险。
        self.make_borrow(name='小样本', overdue_days=9)
        escalate_overdue_borrows(now=self.now + timedelta(minutes=5))

        self.client.force_login(self.staff)
        response = self.client.get(reverse('operations_dashboard'))
        body = response.content.decode('utf-8')

        self.assertIn('小样本分类', body)
        self.assertIn('样本不足', body)


class BorrowRiskExportTests(TestCase):
    """导出要和面板说同一件事，否则下载回去的表会被读成另一个项目的数据。"""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='risk-export-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='risk-export-seller', password='safe-password-123',
        )
        self.borrower = User.objects.create_user(
            username='risk-export-borrower', password='safe-password-123',
        )
        self.now = timezone.now()

    def make_borrow(self, *, name, deposit_amount='100.00', overdue_days=None):
        category = Category.objects.create(name=f'{name}分类')
        item = Item.objects.create(
            title=f'{name}导出物',
            description='用于借用逾期风险导出测试',
            trade_mode='borrow',
            price='60.00',
            deposit_amount=deposit_amount,
            borrow_days=14,
            category=category,
            location=CampusLocation.objects.create(name=f'{name}地点'),
            condition='9成新',
            seller=self.seller,
        )
        order = Order.objects.create(
            item=item, buyer=self.borrower, seller=self.seller,
            agreed_price='0.00', deposit_amount=deposit_amount, status='borrowed',
        )
        if overdue_days is not None:
            due_at = self.now - timedelta(days=overdue_days)
            Order.objects.filter(pk=order.pk).update(
                created_at=due_at - timedelta(days=14), return_due_at=due_at,
            )
            order.refresh_from_db()
        return order

    def test_the_export_carries_both_stratifications(self):
        # 两种分层都要出现在导出里：只导一种，读者就分不清是品类问题还是金额问题。
        self.make_borrow(name='导出高风险', overdue_days=9)
        escalate_overdue_borrows(now=self.now + timedelta(minutes=5))
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('借用逾期风险分层', text)
        self.assertIn('按分类', text)
        self.assertIn('按押金档位', text)
        self.assertIn('导出高风险分类', text)
        self.assertIn('样本不足', text)

    def test_the_export_states_that_it_only_reports(self):
        # 边界要随表导出：风险分层最容易被读成"该提前催收这个分类"的授权，
        # 这句话必须在下载回去的表里，而不是只留在看板上。
        self.make_borrow(name='导出只报告', overdue_days=9)
        escalate_overdue_borrows(now=self.now + timedelta(minutes=5))
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        text = response.content.decode('utf-8-sig')
        self.assertIn('此处只报告', text)
        self.assertIn('不因为某个分类看起来更容易拖就提前催收', text)
