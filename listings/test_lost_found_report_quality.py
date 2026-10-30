# -*- coding: utf-8 -*-
"""为「失物招领举报处理质量」钉住三条时间线与边界自检的口径。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .lost_found_report_quality import (
    PENDING_LIST_LIMIT, _elapsed_seconds, _format_elapsed,
    build_lost_found_report_quality,
)
from .models import Category, LostFoundPost, LostFoundReport, Notification


class LostFoundReportQualityTests(TestCase):
    def setUp(self):
        # member 是发布者，other 是举报人，outsider 两边都不是。
        self.member = User.objects.create_user(
            username='lfq-publisher', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='lfq-reporter', password='safe-password-123',
        )
        self.outsider = User.objects.create_user(
            username='lfq-outsider', password='safe-password-123',
        )
        self.category = Category.objects.create(name='举报质量测试分类')
        self.now = timezone.now()
        self._seq = 0

    def make_post(
        self, *, reporter=None, status='active', expires_at=None, title='举报质量测试失物招领',
    ):
        self._seq += 1
        return LostFoundPost.objects.create(
            reporter=reporter or self.member, post_type='lost',
            title=f'{title} {self._seq}', description='用于测试举报质量统计。',
            occurred_at=self.now - timedelta(hours=3),
            expires_at=self.now + timedelta(days=5) if expires_at is None else expires_at,
            status=status,
        )

    def make_report(
        self, *, post=None, reporter=None, status='pending', reason='impersonation',
        detail='需要处理。', review_note='已核实并下架。', created_ago=timedelta(),
        closed_ago=None,
    ):
        self._seq += 1
        post = post or self.make_post()
        report = LostFoundReport.objects.create(
            post=post, reporter=reporter or self.other, reason=reason,
            detail=detail, status=status,
        )
        updates = {'created_at': self.now - created_ago}
        if closed_ago is not None:
            updates['status'] = status
            updates['reviewed_at'] = self.now - closed_ago
            updates['reviewer_id'] = self.member.pk
            updates['review_note'] = review_note
        LostFoundReport.objects.filter(pk=report.pk).update(**updates)
        report.refresh_from_db()
        return report

    def notify(self, recipient):
        """走唯一的真实通道：report_update + 「我的失物招领举报」。"""
        return Notification.objects.create(
            recipient=recipient, kind='report_update',
            title='举报处理更新', message='你的举报已处理。',
            target_url=reverse('my_lost_found_reports'),
        )

    def test_empty_report_keeps_every_denominator_explicit(self):
        report = build_lost_found_report_quality(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['submission']['submitted_count'], 0)
        self.assertEqual(report['submission']['reporter_count'], 0)
        self.assertEqual(report['submission']['exposed_count'], 0)
        self.assertEqual(report['outcome']['measured_count'], 0)
        self.assertEqual(report['outcome']['resolved_rate'], 0)
        self.assertEqual(report['pending']['measured_count'], 0)
        # 空态不能把均值渲染成 0 分钟：那会让「没有数据」读成「处理很快」。
        self.assertEqual(report['handling']['median_label'], '—')
        self.assertEqual(report['handling']['mean_label'], '—')
        self.assertEqual(report['pending']['oldest_label'], '—')
        self.assertEqual(report['handled_without_notice'], 0)

    def test_pending_reports_stay_out_of_the_handling_distribution(self):
        # 待处理的一条：只进积压线，不进处理时长分布。
        self.make_report(status='pending', created_ago=timedelta(minutes=30))
        # 已处理的一条：2 小时前提交、1.5 小时前处理完，耗时 30 分钟。
        self.make_report(
            status='resolved', created_ago=timedelta(hours=2),
            closed_ago=timedelta(hours=1, minutes=30),
        )

        report = build_lost_found_report_quality(days=30, now=self.now)
        self.assertEqual(report['submission']['submitted_count'], 2)
        self.assertEqual(report['pending']['measured_count'], 1)
        self.assertEqual(report['outcome']['measured_count'], 1)
        # 处理时长分布只装得下已处理的那一条；待处理的按 0 秒算会把结论写反。
        self.assertEqual(
            sum(row['count'] for row in report['handling']['buckets']), 1,
        )
        self.assertEqual(
            report['handling']['buckets'][0]['label'], '1 小时内',
        )
        self.assertEqual(report['handling']['buckets'][0]['count'], 1)
        self.assertEqual(report['handling']['over_sla_count'], 0)

    def test_reason_rows_keep_all_choices_and_only_count_live_records_as_exposed(self):
        self.make_report(reason='impersonation')
        self.make_report(reason='impersonation')
        self.make_report(reason='fraud', post=self.make_post(status='closed'))
        self.make_report(
            reason='false_info', post=self.make_post(expires_at=self.now - timedelta(days=1)),
        )

        report = build_lost_found_report_quality(days=30, now=self.now)
        rows = {row['reason']: row for row in report['reason_rows']}
        # 五个理由一栏都不能少：没有举报的理由也要占一行，否则看不出是没人报
        # 还是没人用这个理由。
        self.assertEqual(
            [row['reason'] for row in report['reason_rows']],
            ['impersonation', 'privacy', 'fraud', 'false_info', 'other'],
        )
        self.assertEqual(rows['impersonation']['count'], 2)
        self.assertEqual(rows['impersonation']['exposed_count'], 2)
        # 已关闭和已过期的记录，伤害窗口已经关上，不计进「仍在架」。
        self.assertEqual(rows['fraud']['exposed_count'], 0)
        self.assertEqual(rows['false_info']['exposed_count'], 0)
        self.assertEqual(report['submission']['exposed_count'], 2)
        self.assertEqual(report['submission']['exposed_share'], 50.0)

    def test_small_samples_only_report_counts(self):
        self.make_report(status='resolved', closed_ago=timedelta(hours=1))
        self.make_report(status='rejected', closed_ago=timedelta(hours=2))

        report = build_lost_found_report_quality(days=30, now=self.now)
        self.assertEqual(report['outcome']['measured_count'], 2)
        self.assertTrue(report['outcome']['is_small_sample'])
        # 样本不足时比例照样给得出，但调用方要知道它不可信：这里只钉住
        # 「可识别为小样本」这件事，不钉住具体百分比。
        self.assertIn('只作参考', report['summary'])
        self.assertTrue(
            all(row['is_small_sample'] for row in report['reason_rows']),
        )

    def test_boundary_counts_the_set_difference_not_the_notification_body(self):
        post = self.make_post()
        self.make_report(post=post)
        # 举报人本该收到的那条通知。
        self.notify(self.other)

        report = build_lost_found_report_quality(days=30, now=self.now)
        boundary = report['boundary']
        self.assertEqual(boundary['channel_notification_count'], 1)
        self.assertEqual(boundary['channel_recipient_count'], 1)
        self.assertEqual(boundary['reporter_count'], 1)
        self.assertEqual(boundary['publisher_count'], 1)
        self.assertEqual(boundary['unexpected_recipient_count'], 0)
        self.assertEqual(boundary['publisher_recipient_count'], 0)
        self.assertEqual(boundary['ambiguous_recipient_count'], 0)
        self.assertTrue(boundary['is_lower_bound'])

    def test_an_unexpected_recipient_is_reported_as_a_leak(self):
        self.make_report()
        self.notify(self.other)
        # 一个既没举报、也没发布过被举报记录的人收到了这条通道的通知。
        self.notify(self.outsider)

        report = build_lost_found_report_quality(days=30, now=self.now)
        boundary = report['boundary']
        self.assertEqual(boundary['channel_recipient_count'], 2)
        self.assertEqual(boundary['unexpected_recipient_count'], 1)
        self.assertEqual(boundary['publisher_recipient_count'], 0)
        self.assertIn('边界需要排查', report['summary'])

    def test_an_ambiguous_recipient_is_not_counted_as_a_leak(self):
        # member 发布过被举报的记录，同时自己也举报过别人的记录：光看通知表
        # 分不清他收到的是哪一条，这类人单独数，不计进泄漏。
        self.make_report()
        self.make_report(post=self.make_post(reporter=self.outsider), reporter=self.member)
        self.notify(self.member)

        report = build_lost_found_report_quality(days=30, now=self.now)
        boundary = report['boundary']
        self.assertEqual(boundary['publisher_count'], 2)
        self.assertEqual(boundary['reporter_count'], 2)
        self.assertEqual(boundary['ambiguous_recipient_count'], 1)
        self.assertEqual(boundary['publisher_recipient_count'], 0)
        self.assertEqual(boundary['unexpected_recipient_count'], 0)

    def test_a_publisher_who_only_published_is_a_leak(self):
        # 只发布过、从没举报过的人收到这条通道的通知，就是明确的越界。
        self.make_report()
        self.notify(self.member)

        report = build_lost_found_report_quality(days=30, now=self.now)
        boundary = report['boundary']
        self.assertEqual(boundary['publisher_recipient_count'], 1)
        self.assertEqual(boundary['unexpected_recipient_count'], 0)
        self.assertEqual(boundary['ambiguous_recipient_count'], 0)

    def test_handled_without_notice_is_zero_on_the_normal_path(self):
        self.make_report(status='resolved', closed_ago=timedelta(hours=1))
        self.notify(self.other)

        report = build_lost_found_report_quality(days=30, now=self.now)
        self.assertEqual(report['outcome']['measured_count'], 1)
        self.assertEqual(report['handled_without_notice'], 0)
        self.assertNotIn('没收到任何通知', report['summary'])

    def test_handled_without_notice_turns_red_when_a_reporter_was_never_told(self):
        # 只有一条写入路径是后台用 readonly_fields 守下来的。哪天有人为脚本
        # 放开一个字段，这里会第一个变红，而用户侧只是「举报没回音」。
        self.make_report(status='resolved', closed_ago=timedelta(hours=1))

        report = build_lost_found_report_quality(days=30, now=self.now)
        self.assertEqual(report['handled_without_notice'], 1)
        self.assertIn('1 名举报人处理完了却没收到任何通知', report['summary'])

    def test_pending_list_is_limited_and_privacy_safe(self):
        for index in range(PENDING_LIST_LIMIT + 2):
            self.make_report(
                status='pending', created_ago=timedelta(minutes=index + 1),
                detail=f'第 {index} 条只用于统计长度的补充说明。',
            )

        report = build_lost_found_report_quality(days=30, now=self.now)
        self.assertEqual(report['pending']['row_total'], PENDING_LIST_LIMIT + 2)
        # 页面只读前 PENDING_LIST_LIMIT 条，越久的排越前。
        self.assertEqual(len(report['pending']['rows']), PENDING_LIST_LIMIT + 2)
        self.assertEqual(
            report['pending']['rows'][0]['wait_seconds'],
            max(row['wait_seconds'] for row in report['pending']['rows']),
        )
        rendered = str(report['pending']['rows'])
        self.assertNotIn('lfq-reporter', rendered)
        self.assertNotIn('lfq-publisher', rendered)
        self.assertNotIn('只用于统计长度的补充说明', rendered)
        self.assertNotIn('举报质量测试失物招领', rendered)
        for row in report['pending']['rows']:
            self.assertEqual(
                set(row),
                {
                    'report_id', 'reason_label', 'submitted_at', 'wait_label',
                    'wait_seconds', 'is_over_sla', 'post_is_exposed', 'detail_length',
                },
            )

    def test_oldest_wait_and_over_sla_are_measured_against_the_governance_deadline(self):
        self.make_report(status='pending', created_ago=timedelta(hours=30))
        self.make_report(status='pending', created_ago=timedelta(minutes=10))

        report = build_lost_found_report_quality(days=30, now=self.now)
        self.assertEqual(report['pending']['oldest_label'], '1 天')
        self.assertEqual(report['pending']['over_one_day_count'], 1)
        self.assertTrue(report['pending']['rows'][0]['is_over_sla'])
        self.assertFalse(report['pending']['rows'][1]['is_over_sla'])

    def test_invalid_period_falls_back_to_thirty_days(self):
        self.make_report(created_ago=timedelta(days=10))
        report = build_lost_found_report_quality(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)
        self.assertEqual(report['submission']['submitted_count'], 1)

    def test_a_report_opened_before_the_period_still_shows_up(self):
        # 40 天前提交的举报今天才处理完：进处理线，不进提交线。
        self.make_report(
            status='resolved', created_ago=timedelta(days=40), closed_ago=timedelta(hours=1),
        )
        self.make_report(created_ago=timedelta(days=40))

        report = build_lost_found_report_quality(days=30, now=self.now)
        self.assertEqual(report['submission']['submitted_count'], 0)
        self.assertEqual(report['outcome']['measured_count'], 1)
        self.assertEqual(report['pending']['measured_count'], 1)
        self.assertTrue(report['has_data'])

    def test_elapsed_seconds_clamps_negative_values_to_zero(self):
        now = timezone.now()
        self.assertEqual(
            _elapsed_seconds(now, now - timedelta(hours=1)), 0.0,
        )
        self.assertEqual(_elapsed_seconds(now, now), 0.0)
        self.assertEqual(_elapsed_seconds(None, now), None)
        self.assertEqual(_elapsed_seconds(now, None), None)

    def test_sub_minute_elapsed_is_not_rendered_as_zero_minutes(self):
        # 同一次审核里写完结论的那一瞬间就是 0 秒，写成「0 分钟」会让人
        # 以为数据缺了。
        self.assertEqual(_format_elapsed(0), '不到 1 分钟')
        self.assertEqual(_format_elapsed(59), '不到 1 分钟')
        self.assertEqual(_format_elapsed(60), '1 分钟')
        self.assertEqual(_format_elapsed(3600), '1 小时')
        self.assertEqual(_format_elapsed(86400), '1 天')
        self.assertEqual(_format_elapsed(None), '—')

    def test_handling_buckets_are_mutually_exclusive_and_complete(self):
        durations = [300, 7200, 40000, 200000, 400000]
        for index, seconds in enumerate(durations):
            self.make_report(
                status='resolved', created_ago=timedelta(seconds=seconds),
                closed_ago=timedelta(seconds=0),
            )

        report = build_lost_found_report_quality(days=30, now=self.now)
        rows = report['handling']['buckets']
        self.assertEqual(
            [row['label'] for row in rows],
            ['1 小时内', '1-6 小时', '6-24 小时', '1-3 天', '3 天以上'],
        )
        self.assertEqual([row['count'] for row in rows], [1, 1, 1, 1, 1])
        self.assertEqual(sum(row['count'] for row in rows), 5)
        # 6-24 小时那一档（40000 秒 ≈ 11 小时）仍在时限内，越线的只有后两档。
        self.assertEqual(report['handling']['over_sla_count'], 2)

    def test_review_note_length_is_counted_but_never_read(self):
        self.make_report(
            status='resolved', closed_ago=timedelta(hours=1),
            review_note='内部备注：疑似同一人连续作案，已转交老师跟进。',
        )

        report = build_lost_found_report_quality(days=30, now=self.now)
        note = report['handling']['note']
        self.assertEqual(note['filled_count'], 1)
        self.assertEqual(note['fill_rate'], 100.0)
        self.assertEqual(note['median'], len('内部备注：疑似同一人连续作案，已转交老师跟进。'))
        self.assertNotIn('同一人连续作案', str(report['summary']))