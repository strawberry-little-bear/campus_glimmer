# -*- coding: utf-8 -*-
"""Pin down the report loop on lost-and-found records.

Lost-and-found is the only flow where a stranger is asked to hand over an
object on the strength of a description, so it gets its own report target
instead of borrowing ``Report``. These tests walk the whole loop: what the
model refuses, what the form accepts, what a reader can submit, what an
operator can close, and what each side is allowed to see afterwards.

The asymmetry under test is deliberate and is the reason this feature exists:
the reporter learns the verdict, the publisher learns nothing. A test that only
checks "a report can be created" would not catch a regression that quietly
started showing the reporter's name to the publisher.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import LostFoundReportForm, LostFoundReportReviewForm
from .models import LostFoundPost, LostFoundReport, Notification, Report


def make_post(reporter, *, post_type='lost', title='图书馆遗失黑色水杯', status='active'):
    return LostFoundPost.objects.create(
        reporter=reporter,
        post_type=post_type,
        title=title,
        description='黑色保温水杯，杯身有蓝色贴纸。',
        occurred_at=timezone.now() - timedelta(hours=3),
        expires_at=timezone.now() + timedelta(days=10),
        status=status,
    )


class LostFoundReportModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='lf-owner', password='safe-password-123')
        self.reader = User.objects.create_user(username='lf-reader', password='safe-password-123')
        self.post = make_post(self.owner)

    def test_one_reader_can_only_report_one_record_once(self):
        LostFoundReport.objects.create(post=self.post, reporter=self.reader, reason='impersonation')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LostFoundReport.objects.create(post=self.post, reporter=self.reader, reason='fraud')

    def test_a_publisher_cannot_report_their_own_record(self):
        report = LostFoundReport(post=self.post, reporter=self.owner, reason='other')
        with self.assertRaises(Exception):
            report.full_clean()

    def test_reasons_are_lost_and_found_shaped_not_item_shaped(self):
        reasons = dict(LostFoundReport.REASON_CHOICES)
        # 冒充失主和泄露身份信息是商品举报没有的两项；否则这个队列就只是 Report 的副本。
        self.assertIn('impersonation', reasons)
        self.assertIn('privacy', reasons)
        self.assertNotIn('scam', reasons)

    def test_deleting_the_record_takes_its_reports_with_it(self):
        LostFoundReport.objects.create(post=self.post, reporter=self.reader, reason='false_info')
        self.post.delete()
        self.assertFalse(LostFoundReport.objects.exists())

    def test_report_is_independent_of_the_item_report_queue(self):
        LostFoundReport.objects.create(post=self.post, reporter=self.reader, reason='false_info')
        self.assertEqual(Report.objects.count(), 0)


class LostFoundReportFormTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='lf-form-owner', password='safe-password-123')
        self.post = make_post(self.owner)

    def test_detail_is_optional_and_reason_is_required(self):
        self.assertTrue(LostFoundReportForm().fields['detail'].required is False)
        self.assertTrue(LostFoundReportForm().fields['reason'].required)

    def test_review_form_only_exposes_the_verdict_and_the_note(self):
        form = LostFoundReportReviewForm()
        self.assertEqual(list(form.fields), ['status', 'review_note'])


class ReportLostFoundViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='lf-view-owner', password='safe-password-123')
        self.reader = User.objects.create_user(username='lf-view-reader', password='safe-password-123')
        self.post = make_post(self.owner)
        self.url = reverse('report_lost_found', args=[self.post.id])

    def test_anonymous_reader_is_sent_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_reader_submits_a_report_and_is_told_it_landed(self):
        self.client.force_login(self.reader)
        response = self.client.post(self.url, {'reason': 'impersonation', 'detail': '说的地点和记录不一致。'})
        self.assertRedirects(response, reverse('lost_found_detail', args=[self.post.id]))
        report = LostFoundReport.objects.get()
        self.assertEqual(report.reporter, self.reader)
        self.assertEqual(report.post, self.post)
        self.assertEqual(report.status, 'pending')
        self.assertIsNone(report.reviewer)
        self.assertIsNone(report.reviewed_at)

    def test_publisher_cannot_report_their_own_record(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url, {'reason': 'other', 'detail': ''})
        self.assertRedirects(response, reverse('lost_found_detail', args=[self.post.id]))
        self.assertFalse(LostFoundReport.objects.exists())

    def test_second_report_from_the_same_reader_is_not_stored(self):
        self.client.force_login(self.reader)
        self.client.post(self.url, {'reason': 'impersonation'})
        response = self.client.post(self.url, {'reason': 'fraud'})
        self.assertRedirects(response, reverse('lost_found_detail', args=[self.post.id]))
        self.assertEqual(LostFoundReport.objects.count(), 1)

    def test_a_closed_record_can_still_be_reported(self):
        self.post.status = 'closed'
        self.post.save(update_fields=['status', 'updated_at'])
        self.client.force_login(self.reader)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_detail_page_shows_the_entry_only_to_other_signed_in_readers(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse('lost_found_detail', args=[self.post.id]))
        self.assertContains(response, reverse('report_lost_found', args=[self.post.id]))

    def test_detail_page_tells_a_reporter_they_already_reported(self):
        report = LostFoundReport.objects.create(post=self.post, reporter=self.reader, reason='impersonation')
        self.client.force_login(self.reader)
        response = self.client.get(reverse('lost_found_detail', args=[self.post.id]))
        self.assertContains(response, '你已经举报过这条记录')
        self.assertContains(response, report.get_status_display())

    def test_detail_page_never_offers_the_entry_to_the_publisher(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('lost_found_detail', args=[self.post.id]))
        self.assertNotContains(response, reverse('report_lost_found', args=[self.post.id]))


class MyLostFoundReportsViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='lf-mine-owner', password='safe-password-123')
        self.reader = User.objects.create_user(username='lf-mine-reader', password='safe-password-123')
        self.other = User.objects.create_user(username='lf-mine-other', password='safe-password-123')
        self.post = make_post(self.owner)

    def test_a_reader_only_sees_their_own_reports(self):
        mine = LostFoundReport.objects.create(post=self.post, reporter=self.reader, reason='impersonation')
        LostFoundReport.objects.create(post=self.post, reporter=self.other, reason='fraud')
        self.client.force_login(self.reader)
        response = self.client.get(reverse('my_lost_found_reports'))
        self.assertContains(response, mine.get_reason_display())
        self.assertNotContains(response, '疑似诈骗或站外引流')

    def test_empty_state_points_back_to_the_public_list(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse('my_lost_found_reports'))
        self.assertContains(response, reverse('lost_found_list'))


class LostFoundReportQueueTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='lf-staff', password='safe-password-123', is_staff=True,
        )
        self.reader = User.objects.create_user(username='lf-queue-reader', password='safe-password-123')
        self.owner = User.objects.create_user(username='lf-queue-owner', password='safe-password-123')
        self.post = make_post(self.owner)
        self.url = reverse('lost_found_report_list')

    def test_non_staff_cannot_open_the_queue(self):
        self.client.force_login(self.reader)
        response = self.client.get(self.url)
        # 视图直接 raise PermissionDenied，测试客户端会把它转成 403 响应，不会把异常抛出来。
        self.assertEqual(response.status_code, 403)

    def test_queue_lists_the_report_without_exposing_the_reporter_to_the_publisher(self):
        report = LostFoundReport.objects.create(
            post=self.post, reporter=self.reader, reason='impersonation',
            detail='说的地点和记录不一致。',
        )
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertContains(response, report.post.title)
        self.assertContains(response, '冒充失主或拾获者')
        self.assertContains(response, '说的地点和记录不一致')

    def test_status_filter_narrows_the_queue(self):
        closed = LostFoundReport.objects.create(
            post=self.post, reporter=self.reader, reason='false_info', status='resolved',
        )
        LostFoundReport.objects.create(post=self.post, reporter=self.owner, reason='other')
        self.client.force_login(self.staff)
        response = self.client.get(self.url + '?status=resolved')
        self.assertContains(response, closed.get_reason_display())
        self.assertNotContains(response, '其他问题')

    def test_review_writes_the_verdict_and_notifies_only_the_reporter(self):
        report = LostFoundReport.objects.create(
            post=self.post, reporter=self.reader, reason='impersonation',
        )
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('review_lost_found_report', args=[report.id]),
            {'status': 'resolved', 'review_note': '已联系发布者核实，记录已关闭。'},
        )
        self.assertRedirects(response, reverse('lost_found_report_list'))
        report.refresh_from_db()
        self.assertEqual(report.status, 'resolved')
        self.assertEqual(report.reviewer, self.staff)
        self.assertIsNotNone(report.reviewed_at)
        self.assertEqual(report.review_note, '已联系发布者核实，记录已关闭。')

        reporter_notice = Notification.objects.filter(recipient=self.reader).get()
        self.assertEqual(reporter_notice.kind, 'report_update')
        self.assertIn('你提交的举报有处理进展', reporter_notice.title)
        self.assertIn('举报状态已更新', reporter_notice.message)
        # 发布者不得知道谁举报了自己，也不得知道审核结论。
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())

    def test_a_reporter_cannot_review_their_own_report(self):
        report = LostFoundReport.objects.create(post=self.post, reporter=self.reader, reason='impersonation')
        self.client.force_login(self.reader)
        response = self.client.post(
            reverse('review_lost_found_report', args=[report.id]),
            {'status': 'resolved', 'review_note': '自己审自己。'},
        )
        self.assertEqual(response.status_code, 403)
        report.refresh_from_db()
        self.assertEqual(report.status, 'pending')
        self.assertIsNone(report.reviewer)
