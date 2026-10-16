# -*- coding: utf-8 -*-
"""Audit contribution milestone facts against the contribution ledger.

The milestone ledger records first crossings after they happen.  Before those
facts are used for period comparisons, this read-only panel answers the more
basic question: does the ledger agree with a stable replay of contribution
rows?  Missing, extra, and inconsistent facts are kept separate so a rule
snapshot difference is not mistaken for damaged data.
"""

from collections import defaultdict

from django.utils import timezone

from .contributions import (
    CONTRIBUTION_BADGE_RULES,
    CONTRIBUTION_LEVELS,
    calculate_contribution_milestones,
)
from .models import CommunityContribution, ContributionMilestoneEvent

PERIOD_CHOICES = (7, 30, 90, 365)
TYPE_LABELS = {'level': '积分等级', 'badge': '贡献徽章'}


def _period_days(days):
    return days if days in PERIOD_CHOICES else 30


def _event_key(user_id, milestone_type, milestone_code):
    return (user_id, milestone_type, milestone_code)


def _rule_order():
    """Stable display order taken from the product-side rule tables."""
    order = {}
    for index, (threshold, _) in enumerate(CONTRIBUTION_LEVELS):
        order[('level', f'level-{threshold}')] = index
    for index, rule in enumerate(CONTRIBUTION_BADGE_RULES):
        order[('badge', rule['code'])] = index
    return order


def _expected_facts():
    """Replay every contribution with the same helper used by the write path."""
    expected = {}
    totals = defaultdict(lambda: {'count': 0, 'users': set()})
    contributions = CommunityContribution.objects.order_by(
        'user_id', 'occurred_at', 'id',
    )

    state = {'user_id': None, 'points': 0, 'events': 0, 'kinds': {}}
    for contribution in contributions:
        if contribution.user_id != state['user_id']:
            state = {'user_id': contribution.user_id, 'points': 0, 'events': 0, 'kinds': {}}

        rows = calculate_contribution_milestones(
            contribution,
            previous_points=state['points'],
            previous_events=state['events'],
            previous_kind_counts=state['kinds'],
        )
        for row in rows:
            key = _event_key(
                row['user_id'], row['milestone_type'], row['milestone_code'],
            )
            expected[key] = row
            totals[(row['milestone_type'], row['milestone_code'])]['count'] += 1
            totals[(row['milestone_type'], row['milestone_code'])]['users'].add(
                row['user_id'],
            )

        state['points'] += contribution.points
        state['events'] += 1
        state['kinds'][contribution.kind] = (
            state['kinds'].get(contribution.kind, 0) + 1
        )
    return expected, totals


def _actual_facts():
    actual = {}
    events = list(
        ContributionMilestoneEvent.objects.select_related(
            'contribution', 'user',
        ).order_by('user_id', 'milestone_type', 'milestone_code', 'id'),
    )
    for event in events:
        key = _event_key(event.user_id, event.milestone_type, event.milestone_code)
        actual[key] = event
    return actual, events


def _rate(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _build_type_rows(expected, actual):
    """Aggregate code-level audit rows by milestone type."""
    code_rows = _build_code_rows(expected, actual)
    grouped = {
        milestone_type: {
            'expected': 0, 'covered': 0, 'missing': 0, 'extra': 0,
            'backfilled': 0, 'online': 0,
        }
        for milestone_type in TYPE_LABELS
    }
    for row in code_rows:
        bucket = grouped.setdefault(row['milestone_type'], {
            'expected': 0, 'covered': 0, 'missing': 0, 'extra': 0,
            'backfilled': 0, 'online': 0,
        })
        for name in bucket:
            bucket[name] += row[name]
    for bucket in grouped.values():
        bucket['coverage_rate'] = _rate(bucket['covered'], bucket['expected'])
    return [
        {'milestone_type': milestone_type, 'type_label': label, **grouped[milestone_type]}
        for milestone_type, label in TYPE_LABELS.items()
        if grouped.get(milestone_type)
    ]


def _build_code_rows(expected, actual):
    grouped = {}
    for key, row in expected.items():
        bucket = grouped.setdefault((key[1], key[2]), {
            'expected': 0, 'actual': 0, 'covered': 0, 'missing': 0, 'extra': 0,
            'backfilled': 0, 'online': 0, 'snapshot_diff': 0, 'titles': set(),
        })
        bucket['expected'] += 1
        bucket['titles'].add(row['milestone_title'])
    for key, event in actual.items():
        bucket = grouped.setdefault((key[1], key[2]), {
            'expected': 0, 'actual': 0, 'covered': 0, 'missing': 0, 'extra': 0,
            'backfilled': 0, 'online': 0, 'snapshot_diff': 0, 'titles': set(),
        })
        bucket['actual'] += 1
        bucket['titles'].add(event.milestone_title)
        bucket['backfilled' if event.backfilled else 'online'] += 1
        if key in expected:
            row = expected[key]
            if (
                event.threshold != row['threshold'] or
                event.milestone_title != row['milestone_title']
            ):
                bucket['snapshot_diff'] += 1

    order = _rule_order()
    rows = []
    for (milestone_type, code), bucket in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0], order.get(item[0], 10_000), item[0][1],
        ),
    ):
        bucket['covered'] = min(bucket['expected'], bucket['actual'])
        bucket['missing'] = max(bucket['expected'] - bucket['actual'], 0)
        bucket['extra'] = max(bucket['actual'] - bucket['expected'], 0)
        bucket['coverage_rate'] = _rate(bucket['covered'], bucket['expected'])
        rows.append({
            'milestone_type': milestone_type,
            'type_label': TYPE_LABELS.get(milestone_type, milestone_type),
            'milestone_code': code,
            'title': ' / '.join(sorted(bucket['titles'])) or '（当前规则外）',
            **{
                name: value for name, value in bucket.items()
                if name not in {'titles'}
            },
        })
    return rows


def _audit(expected, actual, events):
    expected_keys = set(expected)
    actual_keys = set(actual)
    covered_keys = expected_keys & actual_keys
    missing_count = len(expected_keys - actual_keys)
    extra_count = len(actual_keys - expected_keys)

    owner_mismatch_count = 0
    trigger_mismatch_count = 0
    time_mismatch_count = 0
    value_mismatch_count = 0
    boundary_mismatch_count = 0
    snapshot_diff_count = 0
    inconsistent_keys = set()

    for key in actual_keys:
        event = actual[key]
        if event.user_id != event.contribution.user_id:
            owner_mismatch_count += 1
            inconsistent_keys.add(key)
        if key in expected and event.contribution_id != expected[key]['contribution'].pk:
            trigger_mismatch_count += 1
            inconsistent_keys.add(key)
        if event.reached_at != event.contribution.occurred_at:
            time_mismatch_count += 1
            inconsistent_keys.add(key)
        if not (event.previous_value < event.threshold <= event.reached_value):
            boundary_mismatch_count += 1
            inconsistent_keys.add(key)
        if key in expected:
            row = expected[key]
            if (
                event.previous_value != row['previous_value'] or
                event.reached_value != row['reached_value']
            ):
                value_mismatch_count += 1
                inconsistent_keys.add(key)
            if (
                event.threshold != row['threshold'] or
                event.milestone_title != row['milestone_title']
            ):
                snapshot_diff_count += 1

    fact_mismatch_count = len(inconsistent_keys)
    return {
        'expected_count': len(expected_keys),
        'actual_count': len(actual_keys),
        'covered_count': len(covered_keys),
        'missing_count': missing_count,
        'extra_count': extra_count,
        'coverage_rate': _rate(len(covered_keys), len(expected_keys)),
        'backfilled_count': sum(1 for event in events if event.backfilled),
        'online_count': sum(1 for event in events if not event.backfilled),
        'owner_mismatch_count': owner_mismatch_count,
        'trigger_mismatch_count': trigger_mismatch_count,
        'time_mismatch_count': time_mismatch_count,
        'value_mismatch_count': value_mismatch_count,
        'boundary_mismatch_count': boundary_mismatch_count,
        'fact_mismatch_count': fact_mismatch_count,
        'snapshot_diff_count': snapshot_diff_count,
    }


def _summary(*, contribution_count, audit):
    if not contribution_count:
        if audit['actual_count']:
            return (
                f"贡献流水为空，但台账仍有 {audit['actual_count']} 条事件；"
                '这些事件当前无法指回真实贡献，先按多余事实人工核对，不自动删除。'
            )
        return '还没有贡献流水，也不能对账；这里不把覆盖率伪装成 0。'
    if (
        audit['missing_count'] or audit['extra_count'] or
        audit['fact_mismatch_count']
    ):
        return (
            f"稳定重放应得到 {audit['expected_count']} 条首次跨越，"
            f"当前缺失 {audit['missing_count']} 条、多余 {audit['extra_count']} 条、"
            f"事实不一致 {audit['fact_mismatch_count']} 条；先人工核对，不自动修复。"
        )
    if audit['snapshot_diff_count']:
        return (
            f"应有 {audit['expected_count']} 条首次跨越，台账全部覆盖；"
            f"另有 {audit['snapshot_diff_count']} 条规则快照差异，按规则演进单独核对。"
        )
    return (
        f"应有 {audit['expected_count']} 条首次跨越，台账全部覆盖；"
        f"其中回填 {audit['backfilled_count']} 条、在线写入 {audit['online_count']} 条。"
    )


def build_contribution_milestone_coverage(days=30, now=None):
    """Build the read-only milestone coverage report.

    ``days`` is accepted for dashboard registry symmetry and intentionally
    ignored: this audit checks all facts, not a period slice.
    """
    now = now or timezone.now()
    expected, expected_totals = _expected_facts()
    actual, events = _actual_facts()
    audit = _audit(expected, actual, events)
    contribution_count = CommunityContribution.objects.count()
    reached_values = [event.reached_at for event in events]

    return {
        'period_days': _period_days(days),
        'period_end': now,
        'has_data': bool(contribution_count or events),
        'contribution_count': contribution_count,
        'contribution_user_count': CommunityContribution.objects.values(
            'user',
        ).distinct().count(),
        'event_user_count': len({event.user_id for event in events}),
        'first_reached_at': min(reached_values) if reached_values else None,
        'last_reached_at': max(reached_values) if reached_values else None,
        'audit': audit,
        'type_rows': _build_type_rows(expected, actual),
        'code_rows': _build_code_rows(expected, actual),
        'expected_totals': {
            f'{milestone_type}:{code}': {
                'count': values['count'], 'user_count': len(values['users']),
            }
            for (milestone_type, code), values in expected_totals.items()
        },
        'summary': _summary(contribution_count=contribution_count, audit=audit),
        'source_note': (
            '对账重放复用 calculate_contribution_milestones，按用户、业务时间与记录 ID '
            '排序；页面与 CSV 共用同一份结果。'
        ),
        'privacy_note': (
            '缺失、多余与不一致只聚合到里程碑类型或代码，不输出用户、贡献标题、描述或幂等键。'
        ),
    }
