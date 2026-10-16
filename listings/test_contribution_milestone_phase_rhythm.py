# -*- coding: utf-8 -*-
"""贡献里程碑阶段节奏的对照测试。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .academic_calendar import ensure_default_phases
from .contribution_milestone_phase_rhythm import (
    DEFAULT_LOOKBACK_DAYS,
    MIN_EVENTS_PER_CELL,
    build_contribution_milestone_phase_rhythm,
)
from .contributions import record_contribution
from .models import AcademicPhase, AcademicTerm, ContributionMilestoneEvent


def _local_noon(day):
    return timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time())) + timedelta(hours=12)


class ContributionMilestonePhaseRhythmTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.now = _local_noon(self.today)
        self.user_sequence = 0

    def make_term(self, *, name, kind, starts_on):
        term = AcademicTerm.objects.create(
            name=name,
            slug=name,
            kind=kind,
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=140),
        )
        ensure_default_phases(term)
        return term

    def add_first_contributions(self, *, count, at):
        """Each zero-point trade creates exactly one first-contribution event."""
        for _ in range(count):
            self.user_sequence += 1
            record_contribution(
                user=User.objects.create_user(username=f'rhythm-{self.user_sequence}'),
                kind='trade_completed',
                points=0,
                title='完成一笔交易',
                source_key=f'rhythm:{self.user_sequence}',
                occurred_at=at,
            )

    def at_day_offset(self, term, offset):
        return _local_noon(term.starts_on + timedelta(days=offset))

    def test_same_phase_is_compared_across_the_latest_two_same_kind_terms(self):
        old = self.make_term(
            name='前一个秋季', kind='autumn', starts_on=self.today - timedelta(days=400),
        )
        latest = self.make_term(
            name='这个秋季', kind='autumn', starts_on=self.today - timedelta(days=110),
        )
        self.add_first_contributions(count=3, at=self.at_day_offset(old, 1))
        self.add_first_contributions(count=6, at=self.at_day_offset(latest, 1))

        result = build_contribution_milestone_phase_rhythm(days=7, now=self.now)

        self.assertTrue(result['has_data'])
        self.assertTrue(result['coverage_gate']['is_clean'])
        self.assertEqual(result['lookback_days'], DEFAULT_LOOKBACK_DAYS)
        self.assertEqual(result['period_days'], 7)
        group = result['groups'][0]
        self.assertEqual(group['phase_key'], 'registration')
        self.assertEqual(group['term_kind'], 'autumn')
        self.assertEqual(group['latest_term']['name'], '这个秋季')
        self.assertEqual(group['previous_term']['name'], '前一个秋季')
        row = next(
            item for item in group['rows']
            if item['latest']['milestone_code'] == 'first_contribution'
        )
        self.assertEqual(row['status'], 'compared')
        self.assertEqual(row['direction'], 'faster')
        self.assertEqual(row['latest']['event_count'], 6)
        self.assertEqual(row['previous']['event_count'], 3)
        self.assertEqual(row['latest']['user_count'], 6)
        self.assertEqual(row['event_count_delta'], 3)
        self.assertEqual(row['intensity_delta'], 21.5)
        self.assertIn('节奏加快', result['summary'])

    def test_direction_uses_daily_intensity_instead_of_raw_event_count(self):
        old = self.make_term(
            name='短阶段秋季', kind='autumn', starts_on=self.today - timedelta(days=400),
        )
        latest = self.make_term(
            name='长阶段秋季', kind='autumn', starts_on=self.today - timedelta(days=110),
        )
        AcademicPhase.objects.filter(term=old, phase='registration').update(end_offset=2)
        self.add_first_contributions(count=3, at=self.at_day_offset(old, 1))
        self.add_first_contributions(count=6, at=self.at_day_offset(latest, 1))

        result = build_contribution_milestone_phase_rhythm(now=self.now)
        row = result['groups'][0]['rows'][0]

        self.assertEqual(row['previous']['day_count'], 3)
        self.assertEqual(row['latest']['day_count'], 14)
        self.assertEqual(row['previous']['events_per_100_days'], 100.0)
        self.assertEqual(row['latest']['events_per_100_days'], 42.9)
        self.assertEqual(row['event_count_delta'], 3)
        self.assertEqual(row['direction'], 'slower')
        self.assertEqual(row['direction_label'], '节奏放缓')

    def test_a_damaged_coverage_ledger_blocks_every_direction(self):
        old = self.make_term(
            name='阻断旧秋季', kind='autumn', starts_on=self.today - timedelta(days=400),
        )
        latest = self.make_term(
            name='阻断新秋季', kind='autumn', starts_on=self.today - timedelta(days=110),
        )
        self.add_first_contributions(count=3, at=self.at_day_offset(old, 1))
        self.add_first_contributions(count=3, at=self.at_day_offset(latest, 1))
        ContributionMilestoneEvent.objects.filter(
            milestone_code='first_contribution',
        ).first().delete()

        result = build_contribution_milestone_phase_rhythm(now=self.now)

        self.assertFalse(result['coverage_gate']['is_clean'])
        self.assertEqual(result['coverage_gate']['missing_count'], 1)
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['direction_counts'], {'faster': 0, 'slower': 0, 'flat': 0})
        self.assertIn('不输出方向', result['summary'])

    def test_rule_snapshot_difference_does_not_block_the_comparison(self):
        old = self.make_term(
            name='快照旧秋季', kind='autumn', starts_on=self.today - timedelta(days=400),
        )
        latest = self.make_term(
            name='快照新秋季', kind='autumn', starts_on=self.today - timedelta(days=110),
        )
        self.add_first_contributions(count=3, at=self.at_day_offset(old, 1))
        self.add_first_contributions(count=3, at=self.at_day_offset(latest, 1))
        ContributionMilestoneEvent.objects.filter(
            milestone_code='first_contribution', user__username='rhythm-4',
        ).update(milestone_title='旧版迈出第一步')

        result = build_contribution_milestone_phase_rhythm(now=self.now)

        self.assertTrue(result['coverage_gate']['is_clean'])
        self.assertEqual(result['coverage_gate']['snapshot_diff_count'], 1)
        self.assertEqual(result['compared_count'], 1)
        self.assertEqual(result['direction_counts']['flat'], 1)

    def test_thin_cells_keep_numbers_but_do_not_get_a_direction(self):
        old = self.make_term(
            name='薄样本旧秋季', kind='autumn', starts_on=self.today - timedelta(days=400),
        )
        latest = self.make_term(
            name='薄样本新秋季', kind='autumn', starts_on=self.today - timedelta(days=110),
        )
        self.add_first_contributions(count=3, at=self.at_day_offset(old, 1))
        self.add_first_contributions(count=2, at=self.at_day_offset(latest, 1))

        result = build_contribution_milestone_phase_rhythm(now=self.now)
        row = result['groups'][0]['rows'][0]

        self.assertEqual(row['status'], 'thin_sample')
        self.assertEqual(row['direction'], '')
        self.assertEqual(row['latest']['event_count'], 2)
        self.assertEqual(row['previous']['event_count'], 3)
        self.assertEqual(result['compared_count'], 0)
        self.assertEqual(result['thin_sample_count'], 1)
        self.assertIn(f'不足 {MIN_EVENTS_PER_CELL} 条', row['note'])

    def test_a_term_kind_waits_for_its_own_previous_term(self):
        autumn = self.make_term(
            name='异类旧秋季', kind='autumn', starts_on=self.today - timedelta(days=400),
        )
        spring = self.make_term(
            name='异类新春季', kind='spring', starts_on=self.today - timedelta(days=110),
        )
        self.add_first_contributions(count=3, at=self.at_day_offset(autumn, 1))
        self.add_first_contributions(count=3, at=self.at_day_offset(spring, 1))

        result = build_contribution_milestone_phase_rhythm(now=self.now)
        groups = {(group['term_kind'], group['phase_key']): group for group in result['groups']}

        self.assertEqual(groups[('spring', 'registration')]['previous_term'], None)
        self.assertIsNone(groups[('autumn', 'registration')]['previous_term'])
        spring_row = groups[('spring', 'registration')]['rows'][0]
        self.assertEqual(spring_row['status'], 'waiting_previous_term')
        self.assertEqual(result['waiting_count'], 2)

    def test_ongoing_and_unplaced_events_are_counted_without_being_compared(self):
        term = self.make_term(
            name='进行中学期', kind='autumn', starts_on=self.today - timedelta(days=5),
        )
        self.add_first_contributions(count=3, at=self.at_day_offset(term, 1))
        self.add_first_contributions(count=3, at=self.now - timedelta(days=10))

        result = build_contribution_milestone_phase_rhythm(now=self.now)

        self.assertEqual(result['ongoing_phase_event_count'], 3)
        self.assertEqual(result['no_term_event_count'], 3)
        self.assertEqual(result['groups'], [])
        self.assertIn('进行中的事件均已单独计数', result['summary'])

    def test_dashboard_and_export_share_the_same_read_only_contract(self):
        from django.urls import reverse

        old = self.make_term(
            name='页面旧秋季', kind='autumn', starts_on=self.today - timedelta(days=400),
        )
        latest = self.make_term(
            name='页面新秋季', kind='autumn', starts_on=self.today - timedelta(days=110),
        )
        self.add_first_contributions(count=3, at=self.at_day_offset(old, 1))
        self.add_first_contributions(count=3, at=self.at_day_offset(latest, 1))
        staff = User.objects.create_user(
            username='rhythm-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        page = self.client.get(reverse('operations_dashboard'), {'days': 7})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 7})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '贡献里程碑阶段节奏')
        self.assertContains(page, '不输出用户')
        self.assertNotContains(page, 'rhythm-1')
        csv_text = export.content.decode('utf-8-sig')
        self.assertIn('贡献里程碑阶段节奏', csv_text)
        self.assertIn('同类学期同阶段对照', csv_text)
        self.assertIn('方向只按每百天事件强度判断', csv_text)
        self.assertNotIn('rhythm-1', csv_text)
