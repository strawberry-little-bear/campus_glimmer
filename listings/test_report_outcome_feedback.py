# -*- coding: utf-8 -*-
"""钉住商品举报结果回访的边界。

回访只回答一件事：举报人收到完结结果后，这份结果有没有被看懂、被接受。它不是申诉，
也不是第二轮回审。因此这里同时钉住三侧：

1. 用户只能看见自己的举报，只能对完结举报提交一次三档结构化反馈；
2. 管理员能读到回访事实，但回访不参与优先级，也不是改判依据；
3. 运营统计只报回访率、分布与时延，未回访是右删失样本，不算 0 小时。
"""

from datetime import timedelta

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, Notification, Report, ReportOutcomeFeedback
from .report_priority import build_report_queue
from .report_resolution_quality import build_report_resolution_quality


class ReportOutcomeFeedbackTestBase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.reporter = User.objects.create_user(
            username='outcome-feedback-reporter', password='safe-password-123',
        )
        self.other_reporter = User.objects.create_user(
            username='outcome-feedback-witness', password='safe-password-123',
        )
        self.staff = User.objects.create_user(
            username='outcome-feedback-staff', password='safe-password-123',
            is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='outcome-feedback-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='举报回访分类')

    def make_item(self, *, title='举报回访商品', status='sold'):
        return Item.objects.create(
            title=title,
            description='用于测试商品举报结果回访。',
            trade_mode='sale',
            price='30.00',
            category=self.category,
            condition='九成新',
            seller=self.seller,
            status=status,
        )

    def make_report(self, *, item=None, reporter=None, reason='spam', status='pending', created_at=None, reviewed_at=None):
        report = Report.objects.create(
            item=item or self.make_item(),
            reporter=reporter or self.reporter,
            reason=reason,
            detail='用于测试结果回访。',
            status=status,
            reviewed_at=reviewed_at,
        )
        fields = {'created_at': created_at or self.now - timedelta(hours=2)}
        if reviewed_at is not None:
            fields['reviewed_at'] = reviewed_at
        Report.objects.filter(pk=report.pk).update(**fields)
        report.refresh_from_db()
        return report

    def make_feedback(self, report, choice='handled', created_at=None):
        feedback = ReportOutcomeFeedback.objects.create(
            report=report, choice=choice,
        )
        ReportOutcomeFeedback.objects.filter(pk=feedback.pk).update(
            created_at=created_at or self.now - timedelta(hours=1),
        )
        feedback.refresh_from_db()
        return feedback


class MyReportsTests(ReportOutcomeFeedbackTestBase):
    def test_my_reports_only_shows_reports_submitted_by_the_visitor(self):
        mine = self.make_report(item=self.make_item(title='我的回访举报'))
        other = self.make_report(
            item=self.make_item(title='别人的回访举报'), reporter=self.other_reporter,
        )

        self.client.force_login(self.reporter)
        response = self.client.get(reverse('my_reports'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['reports']), [mine])
        self.assertContains(response, mine.item.title)
        self.assertNotContains(response, other.item.title)

    def test_the_feedback_write_path_is_post_only(self):
        report = self.make_report(status='resolved', reviewed_at=self.now - timedelta(hours=2))

        self.client.force_login(self.reporter)
        response = self.client.get(reverse('submit_report_feedback', args=[report.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertFalse(ReportOutcomeFeedback.objects.exists())

    def test_another_reporter_cannot_submit_feedback_for_someone_else(self):
        report = self.make_report(status='resolved', reviewed_at=self.now - timedelta(hours=2))

        self.client.force_login(self.other_reporter)
        response = self.client.post(
            reverse('submit_report_feedback', args=[report.pk]), {'choice': 'handled'},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(ReportOutcomeFeedback.objects.exists())

    def test_an_open_report_cannot_be_visited_yet(self):
        report = self.make_report(status='reviewing')

        self.client.force_login(self.reporter)
        response = self.client.post(
            reverse('submit_report_feedback', args=[report.pk]), {'choice': 'handled'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ReportOutcomeFeedback.objects.exists())

    def test_a_closed_report_accepts_one_structured_reply_only(self):
        report = self.make_report(status='resolved', reviewed_at=self.now - timedelta(hours=2))
        url = reverse('submit_report_feedback', args=[report.pk])
        self.client.force_login(self.reporter)

        first = self.client.post(url, {'choice': 'unclear'})
        second = self.client.post(url, {'choice': 'disagree'})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        feedback = ReportOutcomeFeedback.objects.get(report=report)
        self.assertEqual(feedback.choice, 'unclear')
        self.assertEqual(ReportOutcomeFeedback.objects.count(), 1)

    def test_an_unknown_choice_is_rejected(self):
        report = self.make_report(status='rejected', reviewed_at=self.now - timedelta(hours=2))

        self.client.force_login(self.reporter)
        response = self.client.post(
            reverse('submit_report_feedback', args=[report.pk]), {'choice': 'appeal'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ReportOutcomeFeedback.objects.exists())

    def test_saving_a_review_notifies_the_reporter_to_visit_my_reports(self):
        report = self.make_report(status='pending')

        self.client.force_login(self.staff)
        response = self.client.post(reverse('review_report', args=[report.pk]), {
            'status': 'resolved',
            'review_note': '已核实并下架相关内容。',
        })

        self.assertEqual(response.status_code, 302)
        notification = Notification.objects.get(kind='report_update', recipient=self.reporter)
        self.assertEqual(notification.target_url, reverse('my_reports'))


class StaffReadPathTests(ReportOutcomeFeedbackTestBase):
    def test_staff_queue_and_review_page_show_the_feedback_without_letting_it_change_priority(self):
        routine_first = self.make_report(
            item=self.make_item(title='回访排序前一条'), reason='spam',
            status='rejected', reviewed_at=self.now - timedelta(hours=3),
            created_at=self.now - timedelta(hours=1),
        )
        routine_second = self.make_report(
            item=self.make_item(title='回访排序后一条'), reason='spam',
            status='rejected', reviewed_at=self.now - timedelta(hours=3),
            created_at=self.now - timedelta(hours=2),
        )
        self.make_feedback(routine_second, choice='disagree')

        queue = build_report_queue(
            Report.objects.select_related('item', 'reporter'), now=self.now,
        )

        self.assertEqual(
            [row['report'].pk for row in queue['rows']], [routine_first.pk, routine_second.pk],
        )
        self.assertEqual(queue['rows'][0]['priority_key'], 'normal')
        self.assertEqual(queue['rows'][1]['priority_key'], 'normal')
        self.assertIsNone(queue['rows'][0]['outcome_feedback'])
        self.assertEqual(queue['rows'][1]['outcome_feedback'], 'disagree')
        self.assertEqual(queue['rows'][1]['outcome_feedback_label'], '不认同结果')

        self.client.force_login(self.staff)
        queue_response = self.client.get(reverse('report_list'))
        review_response = self.client.get(reverse('review_report', args=[routine_second.pk]))

        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(queue_response, '回访：不认同结果')
        self.assertContains(queue_response, '也不是申诉或改判依据')
        self.assertContains(review_response, '回访：不认同结果')

    def test_django_admin_reads_the_feedback_but_does_not_offer_editing(self):
        report = self.make_report(status='resolved', reviewed_at=self.now - timedelta(hours=2))
        self.make_feedback(report, choice='unclear')
        from .admin import ReportAdmin
        admin_instance = ReportAdmin(Report, django_admin.site)

        self.assertEqual(admin_instance.outcome_feedback(report), '结果看不懂')
        self.assertIn('outcome_feedback', admin_instance.readonly_fields)
        self.assertIn('feedback', admin_instance.list_select_related)


class OutcomeFeedbackQualityTests(ReportOutcomeFeedbackTestBase):
    def test_quality_joins_report_items_without_assuming_ids_align(self):
        for _ in range(3):
            self.make_item()
        report = self.make_report(
            item=self.make_item(status='sold'), status='rejected',
            reviewed_at=self.now - timedelta(hours=2),
        )
        self.make_feedback(report, choice='handled')

        result = build_report_resolution_quality(days=30, now=self.now)

        self.assertEqual(result['concentration']['inactive_item_count'], 1)
        self.assertEqual(result['outcome_feedback']['replied_count'], 1)

    def test_an_open_report_is_not_a_denominator_for_feedback_rate(self):
        self.make_report(status='pending')

        result = build_report_resolution_quality(days=30, now=self.now)

        feedback = result['outcome_feedback']
        self.assertEqual(feedback['closed_count'], 0)
        self.assertEqual(feedback['replied_count'], 0)
        self.assertEqual(feedback['waiting_count'], 0)
        self.assertIsNone(feedback['response_rate'])
        self.assertTrue(feedback['is_empty'])

    def test_response_distribution_and_latency_are_split_by_outcome(self):
        resolved_fast = self.make_report(
            item=self.make_item(title='回访已处理快速'), status='resolved',
            reviewed_at=self.now - timedelta(hours=2),
        )
        rejected_medium = self.make_report(
            item=self.make_item(title='回访已驳回中速'), status='rejected',
            reviewed_at=self.now - timedelta(hours=3),
        )
        resolved_slow = self.make_report(
            item=self.make_item(title='回访已处理慢速'), status='resolved',
            reviewed_at=self.now - timedelta(days=3),
        )
        resolved_waiting = self.make_report(
            item=self.make_item(title='回访已处理待回访'), status='resolved',
            reviewed_at=self.now - timedelta(hours=1),
        )
        rejected_waiting = self.make_report(
            item=self.make_item(title='回访已驳回待回访'), status='rejected',
            reviewed_at=self.now - timedelta(hours=1),
        )
        self.make_feedback(resolved_fast, 'handled', self.now - timedelta(hours=1, minutes=45))
        self.make_feedback(rejected_medium, 'unclear', self.now - timedelta(hours=1))
        self.make_feedback(resolved_slow, 'disagree', self.now - timedelta(hours=1))

        result = build_report_resolution_quality(days=30, now=self.now)
        feedback = result['outcome_feedback']

        self.assertEqual(feedback['closed_count'], 5)
        self.assertEqual(feedback['replied_count'], 3)
        self.assertEqual(feedback['waiting_count'], 2)
        self.assertEqual(feedback['response_rate'], 60.0)
        self.assertEqual(
            feedback['choice_rows'],
            [
                {'choice': 'disagree', 'label': '不认同结果', 'count': 1, 'share': 33.3,
                 'resolved_count': 1, 'rejected_count': 0, 'is_empty': False},
                {'choice': 'handled', 'label': '问题已处理', 'count': 1, 'share': 33.3,
                 'resolved_count': 1, 'rejected_count': 0, 'is_empty': False},
                {'choice': 'unclear', 'label': '结果看不懂', 'count': 1, 'share': 33.3,
                 'resolved_count': 0, 'rejected_count': 1, 'is_empty': False},
            ],
        )
        self.assertEqual(
            [(row['key'], row['replied_count'], row['response_rate']) for row in feedback['by_outcome']],
            [('resolved', 2, 66.7), ('rejected', 1, 50.0)],
        )
        self.assertEqual([row['count'] for row in feedback['latency_rows']], [1, 1, 1])
        self.assertEqual([row['share'] for row in feedback['latency_rows']], [33.3, 33.3, 33.3])
        self.assertEqual(feedback['latency_sample_count'], 3)
        self.assertEqual(feedback['invalid_latency_count'], 0)
        self.assertTrue(feedback['is_small_sample'])
        self.assertIn('样本不足', result['summary'])

    def test_operations_dashboard_and_csv_export_render_the_feedback_contract(self):
        report = self.make_report(
            status='rejected', reviewed_at=self.now - timedelta(hours=2),
        )
        self.make_feedback(report, choice='disagree')

        self.client.force_login(self.staff)
        dashboard = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        export_text = export.content.decode('utf-8-sig')

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(dashboard, '结果回访率')
        self.assertContains(dashboard, '不认同结果')
        self.assertNotContains(dashboard, '用于测试结果回访。')
        self.assertIn('结果回访率（%）', export_text)
        self.assertIn('不认同结果', export_text)
        self.assertNotIn('用于测试结果回访。', export_text)

    def test_negative_latency_is_an_anomaly_not_a_zero_delay(self):
        report = self.make_report(
            status='resolved', reviewed_at=self.now - timedelta(hours=1),
        )
        self.make_feedback(report, 'handled', self.now - timedelta(hours=2))

        feedback = build_report_resolution_quality(days=30, now=self.now)['outcome_feedback']

        self.assertEqual(feedback['replied_count'], 1)
        self.assertEqual(feedback['latency_sample_count'], 0)
        self.assertEqual(feedback['invalid_latency_count'], 1)
        self.assertEqual([row['count'] for row in feedback['latency_rows']], [0, 0, 0])
        self.assertTrue(all(row['share'] is None for row in feedback['latency_rows']))

    def test_feedback_after_the_statistics_endpoint_is_excluded(self):
        report = self.make_report(
            status='rejected', reviewed_at=self.now - timedelta(days=2),
        )
        self.make_feedback(report, 'handled', self.now + timedelta(hours=1))

        feedback = build_report_resolution_quality(days=30, now=self.now)['outcome_feedback']

        self.assertEqual(feedback['closed_count'], 1)
        self.assertEqual(feedback['replied_count'], 0)
        self.assertEqual(feedback['waiting_count'], 1)
        self.assertEqual(feedback['response_rate'], 0.0)
