# -*- coding: utf-8 -*-
"""为「内容申诉处理质量」钉住提交线、处理线与积压线的边界。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from chat_messages.models import ModerationAppeal, ModerationEvent
from .moderation_appeal_quality import (
    PENDING_LIST_LIMIT, build_moderation_appeal_quality,
)
from .models import Category, Item


class ModerationAppealQualityTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='appeal-quality-author', password='safe-password-123',
        )
        self.reviewer = User.objects.create_user(
            username='appeal-quality-reviewer', password='safe-password-123',
        )
        self.reviewer_two = User.objects.create_user(
            username='appeal-quality-reviewer-two', password='safe-password-123',
        )
        self.category = Category.objects.create(name='申诉质量测试分类')
        self.item = Item.objects.create(
            title='申诉质量测试商品', description='用于申诉质量统计',
            price='10.00', category=self.category, condition='良好',
            seller=User.objects.create_user(
                username='appeal-quality-seller', password='safe-password-123',
            ),
        )
        self.now = timezone.now()
        self._seq = 0

    def make_appeal(
        self, *, status='pending', reason='not_violation', statement='请再看一次语境。',
        submitted_days_ago=1, original_days_ago=2, resolved_days_ago=1,
        original_reviewer=None, resolver=None, channel='comment',
    ):
        self._seq += 1
        original_reviewer = original_reviewer or self.reviewer
        original_at = self.now - timedelta(days=original_days_ago)
        submitted_at = self.now - timedelta(days=submitted_days_ago)
        resolved_at = self.now - timedelta(days=resolved_days_ago)
        event_status = 'dismissed' if status.startswith('accepted') else 'confirmed'
        event = ModerationEvent.objects.create(
            channel=channel,
            author=self.author,
            item=self.item,
            content=f'申诉质量测试内容 {self._seq}',
            matched_terms='外部链接',
            risk_score=20,
            risk_level='low',
            risk_tier='caution',
            status=event_status,
            reviewed_by=resolver if status.startswith('accepted') else original_reviewer,
            reviewed_at=resolved_at if status.startswith('accepted') else original_at,
        )
        appeal = ModerationAppeal.objects.create(
            event=event,
            appellant=self.author,
            reason=reason,
            statement=statement,
            status=status,
            original_reviewed_by=original_reviewer,
            original_reviewed_at=original_at,
            resolved_by=resolver if status != 'pending' else None,
            resolved_at=resolved_at if status != 'pending' else None,
            resolution_note='已按申诉口径复核。' if status != 'pending' else '',
        )
        appeal.submitted_at = submitted_at
        appeal.save(update_fields=['submitted_at'])
        return appeal

    def make_confirmed_without_appeal(self, reviewed_days_ago=2):
        self._seq += 1
        return ModerationEvent.objects.create(
            channel='comment', author=self.author, item=self.item,
            content=f'未申诉内容 {self._seq}', matched_terms='外部链接',
            risk_score=20, risk_level='low', risk_tier='caution',
            status='confirmed', reviewed_by=self.reviewer,
            reviewed_at=self.now - timedelta(days=reviewed_days_ago),
        )

    def test_empty_report_keeps_every_denominator_explicit(self):
        report = build_moderation_appeal_quality(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['submission']['submitted_count'], 0)
        self.assertEqual(report['submission']['original_decision_count'], 0)
        self.assertEqual(report['outcome']['measured_count'], 0)
        self.assertEqual(report['pending']['measured_count'], 0)
        self.assertEqual(report['submission']['delay']['median_label'], '—')
        self.assertEqual(report['resolution']['duration']['median_label'], '—')

    def test_submission_and_resolution_windows_stay_separate(self):
        # 提交线：当前待处理 + 当前周期内提交并处理完的一条。
        self.make_appeal(status='pending', submitted_days_ago=1, original_days_ago=2)
        self.make_appeal(
            status='upheld', submitted_days_ago=2, original_days_ago=3,
            resolved_days_ago=1,
        )
        # 处理线：40 天前提交、昨天才处理完的历史申诉。
        self.make_appeal(
            status='accepted', submitted_days_ago=40, original_days_ago=41,
            resolved_days_ago=1,
        )

        report = build_moderation_appeal_quality(days=30, now=self.now)
        self.assertEqual(report['submission']['submitted_count'], 2)
        self.assertEqual(report['outcome']['measured_count'], 2)
        self.assertEqual(report['pending']['measured_count'], 1)
        # 重叠的一条会同时属于两条时间线，这是口径，不是重复计数。
        self.assertNotEqual(
            report['submission']['submitted_count'],
            report['outcome']['measured_count'] + report['pending']['measured_count'],
        )

    def test_appeal_rate_is_a_cohort_lower_bound(self):
        # 同期三条确认违规，只有一条已申诉：可见申诉率 1 / 3。
        self.make_appeal(
            submitted_days_ago=1, original_days_ago=2,
            original_reviewer=self.reviewer,
        )
        self.make_confirmed_without_appeal(reviewed_days_ago=2)
        self.make_confirmed_without_appeal(reviewed_days_ago=3)
        # 40 天前的历史结论今天来申诉，进入提交量，但不进入同期原判分母。
        self.make_appeal(
            submitted_days_ago=1, original_days_ago=40,
            original_reviewer=self.reviewer,
        )

        report = build_moderation_appeal_quality(days=30, now=self.now)
        self.assertEqual(report['submission']['submitted_count'], 2)
        self.assertEqual(report['submission']['original_decision_count'], 3)
        self.assertEqual(report['submission']['cohort_appeal_count'], 1)
        self.assertEqual(report['submission']['appeal_rate'], 33.3)
        self.assertEqual(report['submission']['historical_decision_count'], 1)

    def test_accepted_without_restore_is_still_accepted(self):
        self.make_appeal(status='upheld', resolved_days_ago=2)
        self.make_appeal(status='accepted', resolved_days_ago=2)
        self.make_appeal(status='accepted_without_restore', resolved_days_ago=1)
        self.make_appeal(status='accepted_without_restore', resolved_days_ago=1)

        report = build_moderation_appeal_quality(days=30, now=self.now)
        self.assertEqual(report['outcome']['measured_count'], 4)
        self.assertEqual(report['outcome']['accepted_total'], 3)
        self.assertEqual(report['outcome']['accepted_rate'], 75.0)
        self.assertEqual(report['outcome']['accepted_without_restore_rate'], 66.7)
        by_status = {row['status']: row['count'] for row in report['outcome']['rows']}
        self.assertEqual(by_status['accepted_without_restore'], 2)

    def test_resolution_buckets_are_mutually_exclusive_and_clamp_negative_values(self):
        appeals = [self.make_appeal(status='upheld') for _index in range(4)]
        resolved_times = [
            self.now - timedelta(days=1, seconds=sample)
            for sample in (300, 1200, 7200)
        ]
        resolved_times.append(self.now - timedelta(days=1))
        submitted_times = [
            resolved_times[0] - timedelta(seconds=300),
            resolved_times[1] - timedelta(seconds=1200),
            resolved_times[2] - timedelta(seconds=7200),
            # submitted_at 晚于 resolved_at，模拟补录产生的负值，应夹到零。
            resolved_times[3] + timedelta(seconds=600),
        ]
        for appeal, submitted_at, resolved_at in zip(
            appeals, submitted_times, resolved_times,
        ):
            ModerationAppeal.objects.filter(pk=appeal.pk).update(
                submitted_at=submitted_at, resolved_at=resolved_at,
            )

        report = build_moderation_appeal_quality(days=30, now=self.now)
        rows = {
            row['label']: row['count']
            for row in report['resolution']['duration']['rows']
        }
        self.assertEqual(rows['十分钟内'], 2)
        self.assertEqual(rows['十分钟到一小时'], 1)
        self.assertEqual(rows['一小时到一天'], 1)
        self.assertEqual(sum(rows.values()), report['outcome']['measured_count'])

    def test_pending_list_is_limited_and_privacy_safe(self):
        for _index in range(PENDING_LIST_LIMIT + 2):
            self.make_appeal(
                status='pending', submitted_days_ago=1,
                statement=f'第 {_index} 条只用于统计长度的说明。',
            )

        report = build_moderation_appeal_quality(days=30, now=self.now)
        self.assertEqual(report['pending']['row_total'], PENDING_LIST_LIMIT + 2)
        self.assertGreater(report['pending']['row_total'], PENDING_LIST_LIMIT)
        rendered = str(report['pending']['rows'][:PENDING_LIST_LIMIT])
        self.assertNotIn('appeal-quality-author', rendered)
        self.assertNotIn('appeal-quality-reviewer', rendered)
        self.assertNotIn('只用于统计长度的说明', rendered)
        self.assertNotIn('申诉质量测试内容', rendered)
        for row in report['pending']['rows'][:PENDING_LIST_LIMIT]:
            self.assertEqual(
                set(row) - {'submitted_at'},
                {
                    'appeal_id', 'event_id', 'channel', 'channel_label', 'reason',
                    'reason_label', 'wait_label', 'statement_length',
                    'has_original_reviewer',
                },
            )

    def test_reviewer_change_requires_both_sides(self):
        self.make_appeal(
            status='upheld', resolved_days_ago=2,
            original_reviewer=self.reviewer, resolver=self.reviewer,
        )
        self.make_appeal(
            status='upheld', resolved_days_ago=2,
            original_reviewer=self.reviewer, resolver=self.reviewer_two,
        )
        missing_snapshot = self.make_appeal(
            status='upheld', resolved_days_ago=1,
            original_reviewer=self.reviewer, resolver=self.reviewer,
        )
        ModerationAppeal.objects.filter(pk=missing_snapshot.pk).update(
            original_reviewed_by=None,
        )

        report = build_moderation_appeal_quality(days=30, now=self.now)
        reviewer = report['resolution']['reviewer']
        self.assertEqual(reviewer['same_count'], 1)
        self.assertEqual(reviewer['different_count'], 1)
        self.assertEqual(reviewer['unknown_count'], 1)
        self.assertEqual(reviewer['different_rate'], 50.0)

    def test_reason_rows_keep_all_choices_and_missing_snapshots_out_of_delay(self):
        self.make_appeal(reason='not_violation', submitted_days_ago=1)
        self.make_appeal(reason='missing_context', submitted_days_ago=1)
        appeal = self.make_appeal(reason='other', submitted_days_ago=1)
        ModerationAppeal.objects.filter(pk=appeal.pk).update(
            original_reviewed_at=None,
        )

        report = build_moderation_appeal_quality(days=30, now=self.now)
        self.assertEqual(
            [row['reason'] for row in report['reason_rows']],
            ['not_violation', 'missing_context', 'other'],
        )
        self.assertTrue(all(row['is_small_sample'] for row in report['reason_rows']))
        self.assertEqual(report['submission']['missing_snapshot_count'], 1)
        self.assertEqual(report['submission']['delay']['measured_count'], 2)

    def test_invalid_period_falls_back_to_thirty_days(self):
        self.make_appeal(submitted_days_ago=10, original_days_ago=11)
        report = build_moderation_appeal_quality(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)
        self.assertEqual(report['submission']['submitted_count'], 1)
