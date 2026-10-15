# -*- coding: utf-8 -*-
"""Pin the explainable viewing order used by the report queue."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .governance_sla import build_governance_sla
from .models import Category, Item, Report
from .report_priority import build_report_priority, build_report_queue


class ReportPriorityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='priority-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='priority-seller', password='safe-password-123',
        )
        self.reporter = User.objects.create_user(
            username='priority-reporter', password='safe-password-123',
        )
        self.other_reporter = User.objects.create_user(
            username='priority-witness', password='safe-password-123',
        )
        self.category = Category.objects.create(name='举报优先级分类')
        self.now = timezone.now()

    def make_item(self, *, title='举报优先级商品', status='available', expires_at=None):
        return Item.objects.create(
            title=title,
            description='用于测试商品举报优先级。',
            trade_mode='sale',
            price='30.00',
            category=self.category,
            condition='九成新',
            seller=self.seller,
            status=status,
            expires_at=expires_at,
        )

    def make_report(self, item=None, reporter=None, *, reason='scam', status='pending', created_ago=timedelta()):
        report = Report.objects.create(
            item=item or self.make_item(),
            reporter=reporter or self.reporter,
            reason=reason,
            detail='用于测试优先级。',
            status=status,
        )
        Report.objects.filter(pk=report.pk).update(created_at=self.now - created_ago)
        report.refresh_from_db()
        return report

    def test_priority_comes_from_named_signals_without_a_black_box_score(self):
        exposed = build_report_priority(
            reason='scam', item_status='available',
            item_expires_at=self.now + timedelta(days=1), now=self.now,
        )
        closed_scam = build_report_priority(
            reason='scam', item_status='sold', item_expires_at=None, now=self.now,
        )
        inappropriate = build_report_priority(
            reason='inappropriate', item_status='available',
            item_expires_at=None, now=self.now,
        )
        spam = build_report_priority(
            reason='spam', item_status='available',
            item_expires_at=None, open_report_count=1, now=self.now,
        )
        corroborated_spam = build_report_priority(
            reason='spam', item_status='available',
            item_expires_at=None, open_report_count=2, now=self.now,
        )
        unknown = build_report_priority(
            reason='legacy-reason', item_status='sold',
            item_expires_at=None, now=self.now,
        )

        self.assertEqual(exposed['priority_key'], 'urgent')
        self.assertIn('商品仍在售', exposed['priority_signals'])
        self.assertEqual(closed_scam['priority_key'], 'high')
        self.assertEqual(inappropriate['priority_key'], 'high')
        self.assertEqual(spam['priority_key'], 'normal')
        self.assertEqual(corroborated_spam['priority_key'], 'high')
        self.assertIn('多人举报同一商品', corroborated_spam['priority_signals'])
        self.assertEqual(unknown['priority_key'], 'normal')
        self.assertIn('常规治理信号', unknown['priority_signals'])

    def test_queue_puts_open_urgent_reports_before_newer_routine_and_closed_rows(self):
        urgent = self.make_report(
            reason='scam', created_ago=timedelta(hours=3),
        )
        routine = self.make_report(
            reason='spam', created_ago=timedelta(hours=1),
        )
        closed = self.make_report(
            reason='scam', status='resolved', created_ago=timedelta(minutes=30),
        )

        queue = build_report_queue(Report.objects.select_related('item', 'reporter'), now=self.now)

        self.assertEqual([row['report'].pk for row in queue['rows']], [urgent.pk, routine.pk, closed.pk])
        self.assertEqual(queue['priority_counts'], {'urgent': 1, 'high': 0, 'normal': 1})
        self.assertEqual(queue['open_total'], 2)

    def test_staff_queue_and_review_page_explain_why_a_report_is_first(self):
        report = self.make_report(reason='scam')

        self.client.force_login(self.staff)
        queue_response = self.client.get(reverse('report_list'))
        review_response = self.client.get(reverse('review_report', args=[report.pk]))

        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(queue_response, '优先处理')
        self.assertContains(queue_response, '疑似诈骗')
        self.assertContains(queue_response, '不是违规结论')
        self.assertEqual(
            queue_response.context['report_rows'][0]['report'].pk, report.pk,
        )
        self.assertEqual(review_response.context['priority']['priority_key'], 'urgent')
        self.assertContains(review_response, '商品仍在售')

    def test_governance_workbench_keeps_sla_order_and_shows_report_priority(self):
        urgent = self.make_report(reason='scam')
        self.make_report(reason='spam')

        data = build_governance_sla(days=30, now=self.now)
        report_cases = {
            row['case_id']: row
            for queue in data['queues'] if queue['kind'] == 'report'
            for row in queue['cases']
        }

        self.assertEqual(report_cases[urgent.pk]['priority_key'], 'urgent')
        self.assertIn('疑似诈骗', report_cases[urgent.pk]['priority_signals'])

        self.client.force_login(self.staff)
        response = self.client.get(reverse('governance_workbench'), {'kind': 'report'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '优先处理')
