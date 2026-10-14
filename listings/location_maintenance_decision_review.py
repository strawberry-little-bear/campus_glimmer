# -*- coding: utf-8 -*-
"""校园地点维护决策的只读回看。

维护队列回答“当前哪些信号有匹配决策”；本模块继续沿同一组信号指纹，把匹配到的决策按
首次留痕时间拆开，并单独观察仍为暂缓的地点。系统没有历史队列快照，也没有决策编辑审计
表，所以这里不计算跨周期处理率，不给升降方向，只报告当前快照里的决策沉淀与暂缓停留。
"""

from django.utils import timezone

from .analytics import PERIOD_CHOICES
from .location_maintenance_queue import (
    OUTCOME_LABELS,
    OUTCOME_SPECS,
    _collect_location_entries,
    _decision_map,
)
from .search_trend import _period_bounds

DEFERRED_ROW_LIMIT = 10
COHORT_SPECS = (
    ('current', '本周期首次决策', '按决策 created_at 落入当前运营周期'),
    ('previous', '上一周期首次决策', '按决策 created_at 落入上一等长周期'),
    ('earlier', '更早首次决策', '早于上一周期起点，仍在支撑当前复核状态'),
)
COHORT_LABELS = {key: label for key, label, _ in COHORT_SPECS}
COHORT_INDEX = {key: index for index, (key, _, _) in enumerate(COHORT_SPECS)}


def _age_days(moment, now):
    """Count complete local calendar days, never returning a negative age."""
    return max(0, (timezone.localdate(now) - timezone.localdate(moment)).days)


def _cohort_key(created_at, current_start, previous_start):
    if created_at >= current_start:
        return 'current'
    if created_at >= previous_start:
        return 'previous'
    return 'earlier'


def _share(numerator, denominator):
    if not denominator:
        return None
    return round(numerator * 100.0 / denominator, 1)


def build_location_maintenance_decision_review(days=30, *, now=None):
    """Read decision sedimentation and deferred dwell for the current queue."""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    days = days if days in allowed_days else 30
    now = now or timezone.now()
    current_start, previous_start = _period_bounds(now, days)

    _, _, entries = _collect_location_entries(now)
    decisions = _decision_map(entries)

    reviewed = []
    cohort_counts = {key: 0 for key, _, _ in COHORT_SPECS}
    outcome_counts = {key: 0 for key, _ in OUTCOME_SPECS}
    outcome_cohort_counts = {
        outcome: {cohort: 0 for cohort, _, _ in COHORT_SPECS}
        for outcome, _ in OUTCOME_SPECS
    }

    for entry in entries:
        decision = decisions.get((entry['id'], entry['signal_fingerprint']))
        if decision is None:
            continue

        reviewed.append((entry, decision))
        cohort = _cohort_key(
            decision.created_at, current_start, previous_start,
        )
        cohort_counts[cohort] += 1
        outcome_counts[decision.outcome] += 1
        outcome_cohort_counts[decision.outcome][cohort] += 1

    queued_total = len(entries)
    reviewed_total = len(reviewed)
    deferred_total = outcome_counts['deferred']

    first_decision_rows = [{
        'key': key,
        'label': label,
        'description': description,
        'decision_total': cohort_counts[key],
        'queue_share': _share(cohort_counts[key], queued_total),
        'outcome_totals': {
            outcome: outcome_cohort_counts[outcome][key]
            for outcome, _ in OUTCOME_SPECS
        },
    } for key, label, description in COHORT_SPECS]

    outcome_age_rows = []
    for outcome, outcome_label in OUTCOME_SPECS:
        for cohort, cohort_label, _ in COHORT_SPECS:
            outcome_age_rows.append({
                'outcome': outcome,
                'outcome_label': outcome_label,
                'cohort': cohort,
                'cohort_label': cohort_label,
                'decision_total': outcome_cohort_counts[outcome][cohort],
            })

    deferred_rows = []
    for entry, decision in reviewed:
        if decision.outcome != 'deferred':
            continue
        deferred_rows.append({
            'name': entry['name'],
            'priority': entry['priority'],
            'priority_label': entry['priority_label'],
            'reason_labels': entry['reason_labels'],
            'signal_fingerprint': entry['signal_fingerprint'],
            'decision_created_at': decision.created_at,
            'decision_updated_at': decision.updated_at,
            'first_decision_age_days': _age_days(decision.created_at, now),
            'last_update_age_days': _age_days(decision.updated_at, now),
        })
    deferred_rows.sort(key=lambda row: (
        -row['last_update_age_days'],
        -row['first_decision_age_days'],
        row['name'],
    ))

    outcome_rows = [{
        'outcome': outcome,
        'outcome_label': outcome_label,
        'cohort_totals': {
            cohort: outcome_cohort_counts[outcome][cohort]
            for cohort, _, _ in COHORT_SPECS
        },
        'decision_total': outcome_counts[outcome],
    } for outcome, outcome_label in OUTCOME_SPECS]

    return {
        'has_data': bool(entries),
        'days': days,
        'current_period_start': current_start,
        'previous_period_start': previous_start,
        'summary': {
            'queued_total': queued_total,
            'unreviewed_total': queued_total - reviewed_total,
            'reviewed_total': reviewed_total,
            'reviewed_share': _share(reviewed_total, queued_total),
            'first_decision_current_total': cohort_counts['current'],
            'first_decision_previous_total': cohort_counts['previous'],
            'first_decision_earlier_total': cohort_counts['earlier'],
            'deferred_total': deferred_total,
        },
        'first_decision_rows': first_decision_rows,
        'outcome_age_rows': outcome_age_rows,
        'outcome_rows': outcome_rows,
        'deferred_rows': deferred_rows[:DEFERRED_ROW_LIMIT],
        'deferred_row_limit': DEFERRED_ROW_LIMIT,
        'deferred_row_hidden_total': max(
            0, len(deferred_rows) - DEFERRED_ROW_LIMIT,
        ),
    }
