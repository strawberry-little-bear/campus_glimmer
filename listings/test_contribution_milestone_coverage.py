# -*- coding: utf-8 -*-
"""贡献里程碑台账与贡献流水的对账测试。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .contributions import record_contribution
from .contribution_milestone_coverage import build_contribution_milestone_coverage
from .models import ContributionMilestoneEvent


class ContributionMilestoneCoverageTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.user = User.objects.create_user(
            username='milestone-user', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='milestone-other', password='safe-password-123',
        )

    def add_contribution(self, user, *, kind, points, source_key, days_ago=0):
        return record_contribution(
            user=user,
            kind=kind,
            points=points,
            title=f'{kind} 对账记录',
            description='用于测试里程碑覆盖审计',
            source_key=source_key,
            occurred_at=self.now - timedelta(days=days_ago),
        )[0]

    def test_online_ledger_is_fully_covered_and_separated_from_backfill(self):
        self.add_contribution(
            self.user, kind='trade_completed', points=10,
            source_key='coverage:trade', days_ago=2,
        )
        self.add_contribution(
            self.other_user, kind='gift_completed', points=8,
            source_key='coverage:gift', days_ago=1,
        )
        ContributionMilestoneEvent.objects.filter(
            user=self.user, milestone_code='level-10',
        ).update(backfilled=True)

        result = build_contribution_milestone_coverage(days=30, now=self.now)

        self.assertTrue(result['has_data'])
        self.assertEqual(result['contribution_count'], 2)
        self.assertEqual(result['contribution_user_count'], 2)
        self.assertEqual(result['event_user_count'], 2)
        self.assertEqual(result['audit']['expected_count'], 4)
        self.assertEqual(result['audit']['actual_count'], 4)
        self.assertEqual(result['audit']['covered_count'], 4)
        self.assertEqual(result['audit']['missing_count'], 0)
        self.assertEqual(result['audit']['extra_count'], 0)
        self.assertEqual(result['audit']['coverage_rate'], 100.0)
        self.assertEqual(result['audit']['backfilled_count'], 1)
        self.assertEqual(result['audit']['online_count'], 3)
        self.assertEqual(result['audit']['fact_mismatch_count'], 0)
        self.assertIn('台账全部覆盖', result['summary'])

        type_rows = {row['milestone_type']: row for row in result['type_rows']}
        self.assertEqual(type_rows['level']['expected'], 1)
        self.assertEqual(type_rows['badge']['expected'], 3)
        self.assertEqual(type_rows['badge']['coverage_rate'], 100.0)

    def test_missing_event_is_reported_without_being_called_extra(self):
        self.add_contribution(
            self.user, kind='trade_completed', points=10,
            source_key='coverage:missing',
        )
        ContributionMilestoneEvent.objects.filter(
            user=self.user, milestone_code='first_contribution',
        ).delete()

        result = build_contribution_milestone_coverage(days=30, now=self.now)

        self.assertEqual(result['audit']['expected_count'], 2)
        self.assertEqual(result['audit']['covered_count'], 1)
        self.assertEqual(result['audit']['missing_count'], 1)
        self.assertEqual(result['audit']['extra_count'], 0)
        self.assertEqual(result['audit']['coverage_rate'], 50.0)
        row = next(
            row for row in result['code_rows']
            if row['milestone_code'] == 'first_contribution'
        )
        self.assertEqual(row['missing'], 1)
        self.assertEqual(row['extra'], 0)

    def test_event_outside_current_replay_is_extra_not_missing(self):
        contribution = self.add_contribution(
            self.user, kind='trade_completed', points=10,
            source_key='coverage:extra',
        )
        ContributionMilestoneEvent.objects.create(
            user=self.user,
            contribution=contribution,
            milestone_type='badge',
            milestone_code='legacy_helper',
            milestone_title='历史规则徽章',
            threshold=1,
            previous_value=0,
            reached_value=1,
            reached_at=contribution.occurred_at,
        )

        result = build_contribution_milestone_coverage(days=30, now=self.now)

        self.assertEqual(result['audit']['expected_count'], 2)
        self.assertEqual(result['audit']['actual_count'], 3)
        self.assertEqual(result['audit']['missing_count'], 0)
        self.assertEqual(result['audit']['extra_count'], 1)
        row = next(
            row for row in result['code_rows']
            if row['milestone_code'] == 'legacy_helper'
        )
        self.assertEqual(row['extra'], 1)
        self.assertEqual(row['title'], '历史规则徽章')

    def test_wrong_trigger_contribution_is_a_fact_mismatch(self):
        first = self.add_contribution(
            self.user, kind='trade_completed', points=10,
            source_key='coverage:trigger-first', days_ago=2,
        )
        second = self.add_contribution(
            self.user, kind='gift_completed', points=0,
            source_key='coverage:trigger-second', days_ago=2,
        )
        # The two contributions share user, business time, and the event still
        # keeps the replayed crossing values; only its trigger pointer is wrong.
        self.assertEqual(first.occurred_at, second.occurred_at)
        ContributionMilestoneEvent.objects.filter(
            user=self.user, milestone_code='first_contribution',
        ).update(contribution=second)

        result = build_contribution_milestone_coverage(days=30, now=self.now)

        self.assertEqual(result['audit']['owner_mismatch_count'], 0)
        self.assertEqual(result['audit']['time_mismatch_count'], 0)
        self.assertEqual(result['audit']['value_mismatch_count'], 0)
        self.assertEqual(result['audit']['trigger_mismatch_count'], 1)
        self.assertEqual(result['audit']['fact_mismatch_count'], 1)
        self.assertIn('事实不一致 1 条', result['summary'])

    def test_rule_snapshot_difference_stays_out_of_fact_mismatch(self):
        self.add_contribution(
            self.user, kind='trade_completed', points=10,
            source_key='coverage:snapshot',
        )
        ContributionMilestoneEvent.objects.filter(
            user=self.user, milestone_code='first_contribution',
        ).update(milestone_title='旧版迈出第一步')

        result = build_contribution_milestone_coverage(days=30, now=self.now)

        self.assertEqual(result['audit']['coverage_rate'], 100.0)
        self.assertEqual(result['audit']['snapshot_diff_count'], 1)
        self.assertEqual(result['audit']['fact_mismatch_count'], 0)
        self.assertIn('规则快照差异', result['summary'])

    def test_time_and_owner_mismatches_count_rows_not_signals(self):
        trade = self.add_contribution(
            self.user, kind='trade_completed', points=10,
            source_key='coverage:mismatch-a', days_ago=2,
        )
        gift = self.add_contribution(
            self.other_user, kind='gift_completed', points=8,
            source_key='coverage:mismatch-b', days_ago=1,
        )
        ContributionMilestoneEvent.objects.filter(
            user=self.user, milestone_code='level-10',
        ).update(reached_at=trade.occurred_at + timedelta(minutes=1))
        ContributionMilestoneEvent.objects.filter(
            user=self.other_user, milestone_code='first_contribution',
        ).update(
            contribution=trade,
            reached_at=trade.occurred_at,
        )

        result = build_contribution_milestone_coverage(days=30, now=self.now)

        self.assertEqual(result['audit']['time_mismatch_count'], 1)
        self.assertEqual(result['audit']['owner_mismatch_count'], 1)
        self.assertEqual(result['audit']['fact_mismatch_count'], 2)
        self.assertEqual(result['audit']['missing_count'], 0)
        self.assertEqual(result['audit']['extra_count'], 0)

    def test_empty_ledger_has_no_coverage_denominator(self):
        result = build_contribution_milestone_coverage(days=7, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertIsNone(result['audit']['coverage_rate'])
        self.assertIn('不把覆盖率伪装成 0', result['summary'])

    def test_dashboard_and_export_share_the_same_read_only_contract(self):
        self.add_contribution(
            self.user, kind='trade_completed', points=10,
            source_key='coverage:dashboard',
        )
        staff = User.objects.create_user(
            username='coverage-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        page = self.client.get(reverse('operations_dashboard'), {'days': 7})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 7})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '贡献里程碑覆盖审计')
        self.assertContains(page, '不自动修复')
        csv_text = export.content.decode('utf-8-sig')
        self.assertIn('贡献里程碑覆盖审计', csv_text)
        self.assertIn('稳定重放应有', csv_text)
        self.assertIn('不输出用户', csv_text)
