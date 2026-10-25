# -*- coding: utf-8 -*-
"""Pagination and ordering contracts for the two governance queues."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import report_priority
from .models import Category, Item, Order, OrderDispute, Report
from .report_priority import (
    build_report_page, build_report_priority_for_report,
    rank_report_queryset, report_priority_summary,
)


class GovernanceQueueTestBase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.staff = User.objects.create_user(
            username='governance-pager-staff',
            password='safe-password-123',
            is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='governance-pager-seller',
            password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='governance-pager-buyer',
            password='safe-password-123',
        )
        self.category = Category.objects.create(name='治理队列分页分类')
        self.sequence = 0
        self.client.force_login(self.staff)

    def make_item(self, *, title=None, status='available', expires_at=None):
        self.sequence += 1
        return Item.objects.create(
            title=title or f'治理队列商品 {self.sequence:03d}',
            description='用于治理队列分页测试。',
            trade_mode='sale',
            price='20.00',
            category=self.category,
            condition='九成新',
            seller=self.seller,
            status=status,
            expires_at=expires_at,
        )

    def make_report(
        self, *, reason='spam', status='pending', item=None,
        reporter=None, created_ago=timedelta(),
    ):
        report = Report.objects.create(
            item=item or self.make_item(),
            reporter=reporter or self.buyer,
            reason=reason,
            detail='治理队列分页测试举报。',
            status=status,
        )
        Report.objects.filter(pk=report.pk).update(
            created_at=self.now - created_ago,
        )
        report.refresh_from_db()
        return report

    def make_dispute(self, *, status='open', created_ago=timedelta(), title=None):
        item = self.make_item(title=title)
        order = Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=self.seller,
            agreed_price='20.00',
            status='confirmed',
        )
        dispute = OrderDispute.objects.create(
            order=order,
            opened_by=self.buyer,
            reason='not_received',
            detail='治理队列分页测试争议。',
            status=status,
        )
        OrderDispute.objects.filter(pk=dispute.pk).update(
            created_at=self.now - created_ago,
        )
        dispute.refresh_from_db()
        return dispute


class ReportQueuePaginationTests(GovernanceQueueTestBase):
    def test_database_priority_rank_matches_the_explainable_python_rule(self):
        scam_item = self.make_item()
        shared_item = self.make_item()
        expired_item = self.make_item(expires_at=self.now - timedelta(hours=1))
        sold_item = self.make_item(status='sold')
        reporter = User.objects.create_user(
            username='governance-witness', password='safe-password-123',
        )
        second_witness = User.objects.create_user(
            username='governance-second-witness', password='safe-password-123',
        )

        reports = [
            self.make_report(item=scam_item, reason='scam'),
            self.make_report(item=sold_item, reason='scam'),
            self.make_report(item=scam_item, reason='inappropriate', reporter=reporter),
            self.make_report(item=shared_item, reason='spam'),
            self.make_report(item=shared_item, reason='spam', reporter=reporter),
            self.make_report(item=expired_item, reason='spam'),
            self.make_report(item=sold_item, reason='inappropriate', reporter=reporter),
            self.make_report(
                item=scam_item, reason='scam', status='resolved', reporter=second_witness,
            ),
        ]

        ranked = {
            report.pk: report
            for report in rank_report_queryset(
                Report.objects.select_related('item'), now=self.now,
            )
        }
        for report in reports:
            expected = build_report_priority_for_report(report, now=self.now)
            self.assertEqual(ranked[report.pk].report_priority_rank, expected['priority_rank'])

        summary = report_priority_summary(now=self.now)
        self.assertEqual(summary['open_total'], 7)
        self.assertEqual(summary['priority_counts'], {
            'urgent': 1,
            'high': 5,
            'normal': 1,
        })

    def test_open_reports_precede_closed_reports_and_priority_survives_pages(self):
        closed_urgent = self.make_report(
            reason='scam', status='resolved', created_ago=timedelta(minutes=1),
        )
        normal = self.make_report(reason='spam', created_ago=timedelta(hours=3))
        high = self.make_report(
            reason='inappropriate', created_ago=timedelta(hours=2),
        )
        urgent = self.make_report(reason='scam', created_ago=timedelta(hours=1))

        queryset = Report.objects.select_related('item', 'feedback')
        first = build_report_page(queryset, page_number=1, per_page=2, now=self.now)
        second = build_report_page(queryset, page_number=2, per_page=2, now=self.now)

        self.assertEqual(
            [row['report'].pk for row in first['rows'] + second['rows']],
            [urgent.pk, high.pk, normal.pk, closed_urgent.pk],
        )
        self.assertEqual(
            [row['priority_key'] for row in first['rows'] + second['rows']],
            ['urgent', 'high', 'normal', 'urgent'],
        )
        self.assertEqual(first['priority_counts'], {
            'urgent': 1,
            'high': 1,
            'normal': 1,
        })
        self.assertEqual(first['open_total'], 3)

    def test_report_endpoint_loads_ten_rows_per_page_and_keeps_global_counts(self):
        for index in range(13):
            self.make_report(created_ago=timedelta(minutes=index))

        with patch.object(
            report_priority, 'build_report_rows',
            wraps=report_priority.build_report_rows,
        ) as rows:
            first = self.client.get(reverse('report_list'))
            second = self.client.get(reverse('report_list'), {'page': 2})

        self.assertEqual(len(rows.call_args_list[0].args[0]), 10)
        self.assertEqual(len(rows.call_args_list[1].args[0]), 3)
        self.assertEqual(first.context['page_obj'].paginator.count, 13)
        self.assertEqual(first.context['pending_report_count'], 13)
        self.assertEqual(first.context['priority_counts']['normal'], 13)
        self.assertEqual(second.context['page_obj'].number, 2)
        self.assertContains(first, '第 1–10 条 · 共 13 条')
        self.assertContains(second, '第 11–13 条 · 共 13 条')

    def test_report_filter_and_out_of_range_page_are_preserved_safely(self):
        for index in range(13):
            self.make_report(created_ago=timedelta(minutes=index))

        filtered = self.client.get(
            reverse('report_list'), {'status': 'pending', 'page': 2},
        )
        too_high = self.client.get(reverse('report_list'), {'page': 999})
        invalid = self.client.get(reverse('report_list'), {'page': 'not-a-number'})

        self.assertEqual(filtered.context['status_filter'], 'pending')
        self.assertEqual(filtered.context['page_obj'].number, 2)
        self.assertContains(filtered, '?page=1&amp;status=pending', html=False)
        self.assertContains(filtered, 'aria-label="商品举报分页"')
        self.assertEqual(too_high.context['page_obj'].number, 2)
        self.assertEqual(invalid.context['page_obj'].number, 1)

    def test_report_summary_stays_global_when_a_status_filter_is_active(self):
        self.make_report(reason='scam')
        self.make_report(reason='inappropriate', created_ago=timedelta(minutes=1))
        self.make_report(reason='spam', created_ago=timedelta(minutes=2))
        self.make_report(reason='spam', status='resolved', created_ago=timedelta(minutes=3))

        response = self.client.get(reverse('report_list'), {'status': 'resolved'})

        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(response.context['pending_report_count'], 3)
        self.assertEqual(response.context['priority_counts'], {
            'urgent': 1,
            'high': 1,
            'normal': 1,
        })

    def test_single_report_page_hides_pagination_and_non_staff_is_denied(self):
        for index in range(10):
            self.make_report(created_ago=timedelta(minutes=index))

        page = self.client.get(reverse('report_list'))
        self.assertNotContains(page, 'report-pagination')

        self.client.force_login(self.buyer)
        denied = self.client.get(reverse('report_list'))
        self.assertEqual(denied.status_code, 403)


class DisputeQueuePaginationTests(GovernanceQueueTestBase):
    def test_dispute_endpoint_uses_twelve_rows_and_keeps_pending_count_global(self):
        for index in range(13):
            self.make_dispute(created_ago=timedelta(minutes=index))

        first = self.client.get(reverse('dispute_list'))
        second = self.client.get(reverse('dispute_list'), {'page': 2})

        self.assertEqual(first.context['page_obj'].paginator.count, 13)
        self.assertEqual(len(first.context['disputes']), 12)
        self.assertEqual(len(second.context['disputes']), 1)
        self.assertEqual(first.context['pending_dispute_count'], 13)
        self.assertEqual(second.context['pending_dispute_count'], 13)
        self.assertContains(first, '第 1–12 条 · 共 13 条')
        self.assertContains(second, '第 13–13 条 · 共 13 条')

    def test_dispute_status_filter_preserves_page_and_global_pending_total(self):
        for index in range(13):
            self.make_dispute(created_ago=timedelta(minutes=index))
        resolved = self.make_dispute(status='resolved', created_ago=timedelta(hours=1))

        filtered = self.client.get(
            reverse('dispute_list'), {'status': 'resolved', 'page': 2},
        )
        pending = self.client.get(
            reverse('dispute_list'), {'status': 'open', 'page': 2},
        )

        self.assertEqual(filtered.context['status_filter'], 'resolved')
        self.assertEqual(
            [dispute.pk for dispute in filtered.context['disputes']],
            [resolved.pk],
        )
        self.assertEqual(filtered.context['pending_dispute_count'], 13)
        self.assertContains(pending, '?page=1&amp;status=open', html=False)
        self.assertContains(pending, 'aria-label="交易争议分页"')

    def test_dispute_order_is_open_then_reviewing_then_closed_newest_first(self):
        open_dispute = self.make_dispute(
            status='open', created_ago=timedelta(days=5), title='待处理争议',
        )
        reviewing = self.make_dispute(
            status='reviewing', created_ago=timedelta(hours=1), title='处理中争议',
        )
        resolved = self.make_dispute(
            status='resolved', created_ago=timedelta(minutes=20), title='已解决争议',
        )
        rejected = self.make_dispute(
            status='rejected', created_ago=timedelta(hours=2), title='已驳回争议',
        )

        response = self.client.get(reverse('dispute_list'))

        self.assertEqual(
            [dispute.pk for dispute in response.context['disputes']],
            [open_dispute.pk, reviewing.pk, resolved.pk, rejected.pk],
        )
        self.assertNotContains(response, 'dispute-pagination')

    def test_dispute_out_of_range_and_invalid_pages_fall_back_safely(self):
        for index in range(13):
            self.make_dispute(created_ago=timedelta(minutes=index))

        too_high = self.client.get(reverse('dispute_list'), {'page': 999})
        invalid = self.client.get(reverse('dispute_list'), {'page': 'not-a-number'})

        self.assertEqual(too_high.context['page_obj'].number, 2)
        self.assertEqual(invalid.context['page_obj'].number, 1)

    def test_non_staff_cannot_open_the_dispute_queue(self):
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('dispute_list'))

        self.assertEqual(response.status_code, 403)
