# -*- coding: utf-8 -*-
"""Compare contribution milestone rhythm across the same academic phase.

A milestone count is not a rhythm. The same fourteen-day opening fortnight can
hold three first contributions in one year and six in another, while an
eighty-four-day teaching phase can hold twenty and still be quieter per day.
This read-only panel therefore compares only the same phase in the latest two
terms of the same kind, and compares daily intensity rather than raw counts.

The panel also refuses to interpret a damaged ledger. It first runs the same
coverage audit shown beside it; any missing row, extra row, or fact mismatch
blocks every direction. A rule snapshot difference is allowed because the event
keeps the rule explanation that was true when the milestone was crossed.
"""

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from .academic_calendar import _term_for_day
from .contribution_milestone_coverage import build_contribution_milestone_coverage
from .contributions import CONTRIBUTION_BADGE_RULES, CONTRIBUTION_LEVELS
from .models import AcademicTerm, ContributionMilestoneEvent

DEFAULT_LOOKBACK_DAYS = 420
MIN_EVENTS_PER_CELL = 3
PHASE_ORDER = ('registration', 'regular', 'exam', 'graduation', 'holiday')
TYPE_LABELS = {'level': '积分等级', 'badge': '贡献徽章'}
DIRECTION_LABELS = {
    'faster': '节奏加快',
    'slower': '节奏放缓',
    'flat': '强度持平',
}

PERIOD_CHOICES = (7, 30, 90, 365)


def _period_days(days):
    return days if days in PERIOD_CHOICES else 30


def _rule_order():
    order = {}
    for index, (threshold, _) in enumerate(CONTRIBUTION_LEVELS):
        order[('level', f'level-{threshold}')] = index
    for index, rule in enumerate(CONTRIBUTION_BADGE_RULES):
        order[('badge', rule['code'])] = index
    return order


def _candidate_phase_cells(*, now, lookback_days):
    """Return closed phase cells whose whole term fits the fixed lookback."""
    today = timezone.localdate(now)
    first_day = today - timedelta(days=lookback_days)
    terms = list(
        AcademicTerm.objects.filter(
            is_active=True,
            starts_on__gte=first_day,
            starts_on__lte=today,
        ).prefetch_related('phases')
    )
    cells = {}
    for term in terms:
        for phase in term.phases.all():
            if phase.end_date >= today:
                continue
            key = (term.pk, phase.phase)
            cells[key] = {
                'term': term,
                'phase': phase,
                'term_id': term.pk,
                'term_name': term.name,
                'term_kind': term.kind,
                'kind_label': term.get_kind_display(),
                'term_starts_on': term.starts_on,
                'phase_key': phase.phase,
                'phase_label': phase.get_phase_display(),
                'start_date': phase.start_date,
                'end_date': phase.end_date,
                'day_count': (phase.end_date - phase.start_date).days + 1,
            }
    return terms, cells


def _assign_events(events, *, terms, cells, today):
    """File each event once using the same calendar helper the product uses."""
    buckets = defaultdict(lambda: {'events': [], 'users': set()})
    counts = {
        'no_term': 0,
        'unassigned_phase': 0,
        'ongoing_phase': 0,
        'outside_comparison': 0,
    }
    term_by_day = {}

    for event in events:
        day = timezone.localdate(event.reached_at)
        if day not in term_by_day:
            term_by_day[day] = _term_for_day(day, terms)[0]
        term = term_by_day[day]
        if term is None:
            counts['no_term'] += 1
            continue

        phase = next(
            (item for item in term.phases.all() if item.contains(day)), None,
        )
        if phase is None:
            counts['unassigned_phase'] += 1
            continue
        if (term.pk, phase.phase) not in cells:
            if phase.end_date >= today:
                counts['ongoing_phase'] += 1
            else:
                counts['outside_comparison'] += 1
            continue

        bucket = buckets[(term.pk, phase.phase)]
        bucket['events'].append(event)
        bucket['users'].add(event.user_id)
    return buckets, counts


def _milestone_rows(event):
    return event.milestone_type, event.milestone_code, event.milestone_title


def _selected_groups(cells):
    """Pick the latest two same-kind terms for each phase."""
    grouped = defaultdict(list)
    for cell in cells.values():
        grouped[(cell['phase_key'], cell['term_kind'])].append(cell)

    selected = {}
    for key, group in grouped.items():
        group.sort(key=lambda item: (item['term_starts_on'], item['term_id']), reverse=True)
        selected[key] = group[:2]
    return selected


def _cell_payload(cell, bucket):
    count = len(bucket['events'])
    return {
        'term_name': cell['term_name'],
        'term_kind': cell['term_kind'],
        'kind_label': cell['kind_label'],
        'phase_key': cell['phase_key'],
        'phase_label': cell['phase_label'],
        'start_date': cell['start_date'],
        'end_date': cell['end_date'],
        'day_count': cell['day_count'],
        'event_count': count,
        'user_count': len(bucket['users']),
        'events_per_100_days': round(count / cell['day_count'] * 100, 1),
    }


def _comparison_rows(selected, buckets):
    order = _rule_order()
    groups = []
    used_cells = set()
    compared_count = 0
    thin_count = 0
    waiting_count = 0
    direction_counts = {'faster': 0, 'slower': 0, 'flat': 0}
    term_ids = set()

    for (phase_key, term_kind), cells in sorted(selected.items()):
        latest = cells[0]
        previous = cells[1] if len(cells) > 1 else None
        latest_bucket = buckets.get((latest['term_id'], phase_key), {'events': [], 'users': set()})
        previous_bucket = (
            buckets.get((previous['term_id'], phase_key), {'events': [], 'users': set()})
            if previous else None
        )

        milestone_titles = defaultdict(set)
        milestone_keys = set()
        for bucket in (latest_bucket, previous_bucket):
            if not bucket:
                continue
            for event in bucket['events']:
                milestone_type, milestone_code, title = _milestone_rows(event)
                milestone_keys.add((milestone_type, milestone_code))
                milestone_titles[(milestone_type, milestone_code)].add(title)
        if not milestone_keys:
            continue

        used_cells.add((latest['term_id'], phase_key))
        term_ids.add(latest['term_id'])
        rows = []
        for milestone_type, milestone_code in sorted(
            milestone_keys,
            key=lambda item: (
                item[0], order.get(item, 10_000), item[1],
            ),
        ):
            title = ' / '.join(sorted(
                milestone_titles[(milestone_type, milestone_code)],
            ))
            latest_row = _cell_payload(latest, latest_bucket)
            latest_row.update({
                'milestone_type': milestone_type,
                'type_label': TYPE_LABELS.get(milestone_type, milestone_type),
                'milestone_code': milestone_code,
                'milestone_title': title,
            })
            previous_row = None
            if previous:
                used_cells.add((previous['term_id'], phase_key))
                term_ids.add(previous['term_id'])
                previous_row = _cell_payload(previous, previous_bucket)
                previous_row.update({
                    'milestone_type': milestone_type,
                    'type_label': TYPE_LABELS.get(milestone_type, milestone_type),
                    'milestone_code': milestone_code,
                    'milestone_title': title,
                })

            has_sample = latest_row['event_count'] >= MIN_EVENTS_PER_CELL
            has_previous_sample = bool(
                previous_row and previous_row['event_count'] >= MIN_EVENTS_PER_CELL
            )
            direction = ''
            direction_label = '—'
            intensity_delta = None
            count_delta = None
            if previous_row is None:
                status = 'waiting_previous_term'
                note = '回看期内还没有同类学期可对照'
                waiting_count += 1
            elif not (has_sample and has_previous_sample):
                status = 'thin_sample'
                note = f'任一侧不足 {MIN_EVENTS_PER_CELL} 条，只报数字'
                thin_count += 1
            else:
                status = 'compared'
                compared_count += 1
                count_delta = latest_row['event_count'] - previous_row['event_count']
                intensity_delta = round(
                    latest_row['events_per_100_days'] -
                    previous_row['events_per_100_days'],
                    1,
                )
                if latest_row['events_per_100_days'] > previous_row['events_per_100_days']:
                    direction = 'faster'
                elif latest_row['events_per_100_days'] < previous_row['events_per_100_days']:
                    direction = 'slower'
                else:
                    direction = 'flat'
                direction_label = DIRECTION_LABELS[direction]
                direction_counts[direction] += 1
                note = '方向只按每百天事件强度判断'

            rows.append({
                'latest': latest_row,
                'previous': previous_row,
                'status': status,
                'status_label': {
                    'waiting_previous_term': '等待同类上一学期',
                    'thin_sample': '薄样本',
                    'compared': '已对照',
                }[status],
                'direction': direction,
                'direction_label': direction_label,
                'event_count_delta': count_delta,
                'intensity_delta': intensity_delta,
                'note': note,
            })

        groups.append({
            'phase_key': phase_key,
            'phase_label': latest['phase_label'],
            'term_kind': term_kind,
            'kind_label': latest['kind_label'],
            'latest_term': {
                'name': latest['term_name'],
                'starts_on': latest['term_starts_on'],
            },
            'previous_term': {
                'name': previous['term_name'],
                'starts_on': previous['term_starts_on'],
            } if previous else None,
            'rows': rows,
        })

    groups.sort(key=lambda item: (
        PHASE_ORDER.index(item['phase_key'])
        if item['phase_key'] in PHASE_ORDER else len(PHASE_ORDER),
        item['term_kind'],
    ))
    return {
        'groups': groups,
        'used_cells': used_cells,
        'term_ids': term_ids,
        'compared_count': compared_count,
        'thin_sample_count': thin_count,
        'waiting_count': waiting_count,
        'direction_counts': direction_counts,
    }


def _coverage_gate(audit):
    blockers = []
    if audit['missing_count']:
        blockers.append(f"缺失 {audit['missing_count']} 条")
    if audit['extra_count']:
        blockers.append(f"多余 {audit['extra_count']} 条")
    if audit['fact_mismatch_count']:
        blockers.append(f"事实不一致 {audit['fact_mismatch_count']} 条")
    return {
        'is_clean': not blockers,
        'blockers': blockers,
        'missing_count': audit['missing_count'],
        'extra_count': audit['extra_count'],
        'fact_mismatch_count': audit['fact_mismatch_count'],
        'snapshot_diff_count': audit['snapshot_diff_count'],
    }


def _summary(*, has_events, gate, has_groups, result):
    if not gate['is_clean']:
        return (
            '覆盖审计尚未干净（' + '、'.join(gate['blockers']) +
            '），阶段节奏不输出方向；先人工核对事实台账。'
        )
    if not has_events:
        return (
            '覆盖审计尚未干净（' + '、'.join(gate['blockers']) +
            '），阶段节奏不输出方向；先人工核对事实台账。'
        )
    if not has_groups:
        return (
            '事件没有落入可对照的已结束阶段；无学期、未划分阶段和进行中的事件均已单独计数。'
        )
    compared = result['compared_count']
    if not compared:
        return (
            '已有阶段格子，但同类学期或样本量还不够；薄样本和等待对照的格子只报数字。'
        )
    directions = result['direction_counts']
    return (
        f"可比里程碑 {compared} 项：节奏加快 {directions['faster']} 项、"
        f"放缓 {directions['slower']} 项、持平 {directions['flat']} 项；"
        '方向只按同类学期同一已结束阶段的每百天强度判断。'
    )


def build_contribution_milestone_phase_rhythm(days=30, *, now=None, coverage=None):
    """Build the read-only cross-term milestone rhythm panel.

    ``days`` is accepted for dashboard registry symmetry and ignored. The panel
    always uses a 420-day lookback so the latest two terms of the same kind can
    appear; a 30-day selector cannot contain two opening fortnights.
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    coverage = coverage or build_contribution_milestone_coverage(days=days, now=now)
    gate = _coverage_gate(coverage['audit'])

    base = {
        'period_days': _period_days(days),
        'lookback_days': DEFAULT_LOOKBACK_DAYS,
        'min_events_per_cell': MIN_EVENTS_PER_CELL,
        'phase_order': list(PHASE_ORDER),
        'coverage_gate': gate,
        'groups': [],
        'group_count': 0,
        'term_count': 0,
        'compared_count': 0,
        'thin_sample_count': 0,
        'waiting_count': 0,
        'direction_counts': {'faster': 0, 'slower': 0, 'flat': 0},
        'no_term_event_count': 0,
        'unassigned_phase_event_count': 0,
        'ongoing_phase_event_count': 0,
        'outside_comparison_event_count': 0,
        'assigned_event_count': 0,
        'source_note': (
            '先复用覆盖审计作为门槛，再按事件业务时间归入同类学期同一已结束阶段；'
            '方向只比较每百天事件强度。'
        ),
        'privacy_note': (
            '只输出里程碑与聚合样本，不输出用户、贡献标题、描述或触发贡献明细。'
        ),
    }

    window_start = timezone.make_aware(
        timezone.datetime.combine(
            today - timedelta(days=DEFAULT_LOOKBACK_DAYS),
            timezone.datetime.min.time(),
        )
    )
    events = list(
        ContributionMilestoneEvent.objects.filter(
            reached_at__gte=window_start,
            reached_at__lte=now,
        ).order_by('reached_at', 'id')
    )
    base['has_data'] = bool(events)
    base['event_count'] = len(events)
    base['summary'] = _summary(
        has_events=bool(events), gate=gate, has_groups=False, result=base,
    )
    if not events or not gate['is_clean']:
        return base

    terms, cells = _candidate_phase_cells(
        now=now, lookback_days=DEFAULT_LOOKBACK_DAYS,
    )
    buckets, placement_counts = _assign_events(
        events, terms=terms, cells=cells, today=today,
    )
    selected = _selected_groups(cells)
    comparison = _comparison_rows(selected, buckets)

    selected_keys = comparison['used_cells']
    outside_comparison = placement_counts['outside_comparison']
    outside_comparison += sum(
        len(bucket['events'])
        for key, bucket in buckets.items() if key not in selected_keys
    )
    assigned_count = sum(
        len(bucket['events'])
        for key, bucket in buckets.items() if key in selected_keys
    )

    base.update({
        'groups': comparison['groups'],
        'group_count': len(comparison['groups']),
        'term_count': len(comparison['term_ids']),
        'compared_count': comparison['compared_count'],
        'thin_sample_count': comparison['thin_sample_count'],
        'waiting_count': comparison['waiting_count'],
        'direction_counts': comparison['direction_counts'],
        'no_term_event_count': placement_counts['no_term'],
        'unassigned_phase_event_count': placement_counts['unassigned_phase'],
        'ongoing_phase_event_count': placement_counts['ongoing_phase'],
        'outside_comparison_event_count': outside_comparison,
        'assigned_event_count': assigned_count,
    })
    base['summary'] = _summary(
        has_events=True, gate=gate,
        has_groups=bool(comparison['groups']), result=base,
    )
    return base
