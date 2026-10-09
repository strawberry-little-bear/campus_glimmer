from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Notification
from .notification_response import (
    BUSINESS_CRITICAL_KINDS,
    LATENCY_BUCKETS,
    build_notification_response_insights,
    response_seconds_of,
)


class NotificationResponseInsightTests(TestCase):
    """Cover the latency metrics derived from the read_at timestamp."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='response-owner', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='response-other', password='safe-password-123',
        )
        self.now = timezone.now()

    def create_notification(self, *, kind='order_created', read_after=None, created_ago=None):
        """Create a notification with a pinned created_at.

        created_at is auto_now_add, so it has to be rewritten through update()
        to land inside the measured period; otherwise every row would sit just
        after the reference instant and be filtered out.
        """
        notification = Notification.objects.create(
            recipient=self.user,
            kind=kind,
            title='处理时效测试通知',
            message='用于验证通知处理时效统计。',
        )
        created_at = self.now - (created_ago or timedelta())
        updates = {'created_at': created_at}
        if read_after is not None:
            updates['is_read'] = True
            updates['read_at'] = created_at + read_after
        Notification.objects.filter(pk=notification.pk).update(**updates)
        notification.refresh_from_db()
        return notification

    def test_mark_read_is_idempotent_and_keeps_the_original_timestamp(self):
        notification = self.create_notification()
        first_stamp = self.now + timedelta(hours=3)

        self.assertTrue(notification.mark_read(now=first_stamp))
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertEqual(notification.read_at, first_stamp)
        self.assertEqual(notification.response_seconds, 3 * 3600)

        # Marking again must not move the timestamp, otherwise the measured
        # latency would drift with every repeated click.
        self.assertFalse(notification.mark_read(now=self.now + timedelta(days=1)))
        notification.refresh_from_db()
        self.assertEqual(notification.read_at, first_stamp)

    def test_response_seconds_is_none_until_the_notification_is_read(self):
        notification = self.create_notification()
        self.assertIsNone(notification.response_seconds)
        self.assertIsNotNone(notification.age_seconds)
        self.assertFalse(notification.is_stale)

    def test_unread_notification_older_than_a_week_is_stale(self):
        notification = self.create_notification(created_ago=timedelta(days=9))
        self.assertTrue(notification.is_stale)
        self.assertIsNone(notification.response_seconds)

    def test_response_seconds_of_skips_rows_without_a_read_timestamp(self):
        notification = self.create_notification()
        row = {
            'created_at': notification.created_at,
            'read_at': None,
            'is_read': False,
        }
        self.assertIsNone(response_seconds_of(row))

    def test_empty_period_reports_no_data(self):
        insights = build_notification_response_insights(days=30, now=self.now)

        self.assertFalse(insights['has_data'])
        self.assertEqual(insights['summary']['sent'], 0)
        self.assertEqual(insights['summary']['median_label'], '—')
        self.assertEqual(insights['latency_total'], 0)
        self.assertEqual(len(insights['latency_buckets']), len(LATENCY_BUCKETS))
        self.assertEqual(
            insights['recommendations'],
            ['当前周期各类通知的处理时效没有明显异常，继续保持。'],
        )

    def test_summary_counts_sent_handled_unread_and_stale(self):
        self.create_notification(kind='order_created', read_after=timedelta(hours=2))
        self.create_notification(kind='order_created', read_after=timedelta(days=2))
        self.create_notification(kind='message_received')
        self.create_notification(kind='report_update', created_ago=timedelta(days=10))

        insights = build_notification_response_insights(days=30, now=self.now)
        summary = insights['summary']

        self.assertTrue(insights['has_data'])
        self.assertEqual(summary['sent'], 4)
        self.assertEqual(summary['handled'], 2)
        self.assertEqual(summary['unread'], 2)
        self.assertEqual(summary['stale_unread'], 1)
        self.assertEqual(summary['handling_rate'], 50.0)
        self.assertEqual(summary['median_seconds'], 2 * 3600)
        self.assertEqual(summary['median_label'], '2 小时')

    def test_notifications_outside_the_period_are_excluded(self):
        self.create_notification(kind='order_created', read_after=timedelta(hours=1))
        self.create_notification(
            kind='order_created',
            created_ago=timedelta(days=45),
            read_after=timedelta(hours=1),
        )

        insights = build_notification_response_insights(days=30, now=self.now)

        self.assertEqual(insights['summary']['sent'], 1)
        self.assertEqual(insights['summary']['handled'], 1)

    def test_kind_rows_group_by_type_and_flag_slow_or_unread_types(self):
        self.create_notification(kind='order_created', read_after=timedelta(minutes=30))
        self.create_notification(kind='order_created', read_after=timedelta(minutes=90))
        self.create_notification(kind='report_update')
        self.create_notification(kind='report_update')
        self.create_notification(kind='message_received', read_after=timedelta(days=9))

        insights = build_notification_response_insights(days=30, now=self.now)
        rows = {row['kind']: row for row in insights['kind_rows']}

        self.assertEqual(len(insights['kind_rows']), 3)
        order_row = rows['order_created']
        self.assertEqual(order_row['sent'], 2)
        self.assertEqual(order_row['read'], 2)
        self.assertEqual(order_row['read_rate'], 100.0)
        self.assertEqual(order_row['avg_seconds'], 3600)
        self.assertEqual(order_row['avg_label'], '1 小时')
        self.assertEqual(order_row['fastest_label'], '30 分钟')
        self.assertEqual(order_row['slowest_label'], '2 小时')
        self.assertTrue(order_row['is_business_critical'])
        self.assertFalse(order_row['needs_attention'])

        report_row = rows['report_update']
        self.assertEqual(report_row['read'], 0)
        self.assertEqual(report_row['read_rate'], 0)
        self.assertTrue(report_row['needs_attention'])

        message_row = rows['message_received']
        self.assertEqual(message_row['avg_seconds'], 9 * 86400)
        self.assertTrue(message_row['needs_attention'])
        self.assertEqual(insights['attention_rows'], [report_row, message_row])

    def test_latency_buckets_describe_the_distribution(self):
        self.create_notification(kind='order_created', read_after=timedelta(minutes=10))
        self.create_notification(kind='order_created', read_after=timedelta(hours=5))
        self.create_notification(kind='order_created', read_after=timedelta(days=2))
        self.create_notification(kind='order_created', read_after=timedelta(days=20))

        insights = build_notification_response_insights(days=30, now=self.now)
        buckets = {bucket['label']: bucket for bucket in insights['latency_buckets']}

        self.assertEqual(insights['latency_total'], 4)
        self.assertEqual(buckets['1 小时内']['count'], 1)
        self.assertEqual(buckets['1 天内']['count'], 1)
        self.assertEqual(buckets['3 天内']['count'], 1)
        self.assertEqual(buckets['1 周以上']['count'], 1)
        self.assertEqual(buckets['1 周内']['count'], 0)
        self.assertEqual(buckets['1 小时内']['share'], 25.0)

    def test_critical_summary_only_counts_business_critical_kinds(self):
        self.create_notification(kind='order_created', read_after=timedelta(hours=1))
        self.create_notification(kind='order_expiring', read_after=timedelta(hours=2))
        self.create_notification(kind='report_update')
        self.create_notification(kind='message_received', read_after=timedelta(hours=3))

        insights = build_notification_response_insights(days=30, now=self.now)
        critical = insights['critical_summary']

        self.assertEqual(critical['sent'], 2)
        self.assertEqual(critical['read'], 2)
        self.assertEqual(critical['read_rate'], 100.0)
        self.assertEqual(critical['count'], 2)
        self.assertNotIn('report_update', BUSINESS_CRITICAL_KINDS)

    def test_hour_profile_spans_a_full_day_and_marks_handled_hours(self):
        read_after = timedelta(hours=4)
        self.create_notification(kind='order_created', read_after=read_after)
        self.create_notification(kind='order_created', read_after=read_after)

        insights = build_notification_response_insights(days=30, now=self.now)
        profile = insights['hour_profile']

        self.assertEqual(len(profile), 24)
        self.assertEqual(profile[0]['label'], '00:00')
        self.assertEqual(sum(point['sent'] for point in profile), 2)
        self.assertEqual(sum(point['read'] for point in profile), 2)
        read_hour = next(point['hour'] for point in profile if point['read'])
        self.assertNotEqual(read_hour, timezone.localtime(self.now).hour)

    def test_handled_trend_covers_every_day_of_the_period(self):
        # Kept inside the same day so the handling lands on a date the trend
        # actually reports.
        self.create_notification(kind='order_created', read_after=timedelta(minutes=45))

        insights = build_notification_response_insights(days=7, now=self.now)

        self.assertEqual(len(insights['handled_trend']), 7)
        self.assertEqual(sum(point['handled'] for point in insights['handled_trend']), 1)
        self.assertGreaterEqual(insights['handled_trend_max'], 1)

    def test_recommendations_call_out_critical_types_and_stale_rows(self):
        self.create_notification(kind='order_created')
        self.create_notification(kind='order_created')
        self.create_notification(kind='report_update', created_ago=timedelta(days=10))

        insights = build_notification_response_insights(days=30, now=self.now)
        recommendations = insights['recommendations']

        self.assertTrue(any('交易推进' in text for text in recommendations))
        self.assertTrue(any('超过一周仍未处理' in text for text in recommendations))

    def test_sending_hours_without_any_handling_are_reported(self):
        for _ in range(5):
            self.create_notification(kind='message_received')

        insights = build_notification_response_insights(days=30, now=self.now)

        self.assertTrue(any('时段发送的通知目前没有处理记录' in text for text in insights['recommendations']))


class NotificationReadAtViewTests(TestCase):
    """Every mark-read path must stamp read_at, or latency cannot be measured."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='read-at-owner', password='safe-password-123',
        )

    def create_notification(self, kind='order_created'):
        return Notification.objects.create(
            recipient=self.user,
            kind=kind,
            title='已读时间测试通知',
            message='用于验证已读时间写入。',
        )

    def test_marking_a_single_notification_writes_read_at(self):
        notification = self.create_notification()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('mark_notification_read', args=[notification.id]),
        )

        self.assertEqual(response.status_code, 302)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)

    def test_marking_all_notifications_writes_read_at(self):
        first = self.create_notification()
        second = self.create_notification(kind='message_received')
        self.client.force_login(self.user)

        self.client.post(reverse('mark_all_notifications_read'))

        for notification in (first, second):
            notification.refresh_from_db()
            self.assertTrue(notification.is_read)
            self.assertIsNotNone(notification.read_at)

    def test_marking_selected_notifications_writes_read_at(self):
        target = self.create_notification()
        self.create_notification(kind='report_update')
        self.client.force_login(self.user)

        self.client.post(
            reverse('mark_selected_notifications_read'),
            {'notification_ids': [str(target.id)]},
        )

        target.refresh_from_db()
        self.assertTrue(target.is_read)
        self.assertIsNotNone(target.read_at)

    def test_marking_all_activity_writes_read_at(self):
        notification = self.create_notification()
        self.client.force_login(self.user)

        self.client.post(reverse('mark_all_activity_read'))

        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)