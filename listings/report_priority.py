# -*- coding: utf-8 -*-
"""Explainable viewing order for item reports.

The report queue used to order rows by model defaults: status first, then the
newest report. That treats a fraud report on a still-visible listing in the
same way as a spam report on an already-sold one. This module derives a narrow,
read-only priority from facts a reviewer can check, and never writes it back.
"""

from collections import Counter

from django.db.models import Count
from django.utils import timezone

from .models import Report, ReportOutcomeFeedback


OPEN_REPORT_STATUSES = frozenset({'pending', 'reviewing'})

PRIORITY_ORDER = {
    'urgent': 0,
    'high': 1,
    'normal': 2,
}
PRIORITY_LABELS = {
    'urgent': '优先处理',
    'high': '较高关注',
    'normal': '常规处理',
}
OUTCOME_FEEDBACK_LABELS = dict(ReportOutcomeFeedback.CHOICE_CHOICES)


def _is_exposed(item_status, expires_at, now):
    return item_status == 'available' and (
        expires_at is None or expires_at > now
    )


def build_report_priority(
    *, reason, item_status, item_expires_at, open_report_count=1,
    is_open=True, now=None,
):
    """Return a report's viewing priority and the signals behind it.

    The result is deliberately not a score. Three buckets are enough for a
    human queue, and named signals let a reviewer disagree with the rule after
    looking at the actual report.
    """
    now = now or timezone.now()
    exposed = _is_exposed(item_status, item_expires_at, now)
    signals = []

    if reason == 'scam':
        signals.append('疑似诈骗')
        priority = 'urgent' if exposed else 'high'
    elif reason == 'inappropriate' and exposed:
        signals.append('不当内容仍在售')
        priority = 'high'
    else:
        priority = 'normal'

    if exposed and reason == 'scam':
        signals.append('商品仍在售')

    if is_open and open_report_count >= 2:
        signals.append('多人举报同一商品')
        priority = 'urgent' if priority == 'urgent' else 'high'

    if not signals:
        signals.append('常规治理信号')

    return {
        'priority_key': priority,
        'priority_label': PRIORITY_LABELS[priority],
        'priority_rank': PRIORITY_ORDER[priority],
        'priority_signals': signals,
        'open_report_count': open_report_count if is_open else None,
        'item_is_exposed': exposed,
    }


def open_report_counts():
    """Count currently open reports per item without exposing any user."""
    return dict(
        Report.objects.filter(status__in=OPEN_REPORT_STATUSES).values(
            'item_id',
        ).annotate(total=Count('id')).values_list('item_id', 'total')
    )


def build_report_priority_for_report(report, *, open_counts=None, now=None):
    now = now or timezone.now()
    counts = open_counts if open_counts is not None else open_report_counts()
    is_open = report.status in OPEN_REPORT_STATUSES
    return build_report_priority(
        reason=report.reason,
        item_status=report.item.status,
        item_expires_at=report.item.expires_at,
        open_report_count=counts.get(report.item_id, 1 if is_open else 0),
        is_open=is_open,
        now=now,
    )


def build_report_queue(queryset, *, now=None, summary_queryset=None):
    """Decorate and sort a report queryset for the staff queue.

    ``summary_queryset`` lets the page keep the priority distribution tied to
    the whole open backlog even when the visitor has selected one status.
    """
    now = now or timezone.now()
    counts = open_report_counts()
    reports = list(queryset)
    feedback_choices = dict(
        ReportOutcomeFeedback.objects.filter(
            report_id__in=[report.pk for report in reports],
        ).values_list('report_id', 'choice')
    ) if reports else {}
    decorated = []
    for report in reports:
        priority = build_report_priority_for_report(report, open_counts=counts, now=now)
        outcome_feedback = feedback_choices.get(report.pk)
        decorated.append({
            'report': report,
            **priority,
            'outcome_feedback': outcome_feedback,
            'outcome_feedback_label': (
                OUTCOME_FEEDBACK_LABELS.get(outcome_feedback) if outcome_feedback else None
            ),
        })

    decorated.sort(key=lambda row: (
        0 if row['report'].status in OPEN_REPORT_STATUSES else 1,
        row['priority_rank'],
        -row['report'].created_at.timestamp(),
        -row['report'].pk,
    ))

    summary_rows = decorated
    if summary_queryset is not None:
        summary_rows = [
            {
                'report': report,
                **build_report_priority_for_report(report, open_counts=counts, now=now),
            }
            for report in summary_queryset
        ]
    open_rows = [
        row for row in summary_rows
        if row['report'].status in OPEN_REPORT_STATUSES
    ]
    priority_counts = Counter(row['priority_key'] for row in open_rows)

    return {
        'rows': decorated,
        'priority_counts': {key: priority_counts.get(key, 0) for key in PRIORITY_ORDER},
        'open_total': len(open_rows),
        'priority_labels': PRIORITY_LABELS,
    }
