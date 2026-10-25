# -*- coding: utf-8 -*-
"""Explainable viewing order for item reports.

The report queue used to order rows by model defaults: status first, then the
newest report. That treats a fraud report on a still-visible listing in the
same way as a spam report on an already-sold one. This module derives a narrow,
read-only priority from facts a reviewer can check, and never writes it back.
"""

from collections import Counter

from django.core.paginator import Paginator
from django.db.models import (
    BooleanField, Case, Count, IntegerField, OuterRef, Q, Subquery, Value, When,
)
from django.db.models.functions import Coalesce
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
    is_open = report.status in OPEN_REPORT_STATUSES
    if open_counts is not None:
        open_count = open_counts.get(report.item_id, 1 if is_open else 0)
    else:
        open_count = getattr(report, 'open_report_count', None)
        if open_count is None:
            open_count = open_report_counts().get(report.item_id, 1 if is_open else 0)
    return build_report_priority(
        reason=report.reason,
        item_status=report.item.status,
        item_expires_at=report.item.expires_at,
        open_report_count=open_count,
        is_open=is_open,
        now=now,
    )


def rank_report_queryset(queryset, *, now=None):
    """Add the queue's explainable ordering fields in the database.

    The annotations deliberately mirror ``build_report_priority``. Keeping the
    sort in SQL means a paginated queue never has to materialize the whole
    backlog just to find the first ten rows.
    """
    now = now or timezone.now()
    open_count = (
        Report.objects.filter(
            item_id=OuterRef('item_id'),
            status__in=OPEN_REPORT_STATUSES,
        )
        .order_by()
        .values('item_id')
        .annotate(total=Count('id'))
        .values('total')
    )
    ranked = queryset.annotate(
        open_report_count=Coalesce(
            Subquery(open_count, output_field=IntegerField()),
            Value(0),
            output_field=IntegerField(),
        ),
        item_is_exposed=Case(
            When(
                Q(item__status='available') & (
                    Q(item__expires_at__isnull=True)
                    | Q(item__expires_at__gt=now)
                ),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        ),
    ).annotate(
        report_open_rank=Case(
            When(status__in=OPEN_REPORT_STATUSES, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ),
        report_priority_rank=Case(
            When(Q(item_is_exposed=True, reason='scam'), then=Value(0)),
            When(Q(reason='scam'), then=Value(1)),
            When(Q(item_is_exposed=True, reason='inappropriate'), then=Value(1)),
            When(
                Q(status__in=OPEN_REPORT_STATUSES, open_report_count__gte=2),
                then=Value(1),
            ),
            default=Value(2),
            output_field=IntegerField(),
        ),
    )
    return ranked.order_by(
        'report_open_rank', 'report_priority_rank', '-created_at', '-pk',
    )


def report_priority_summary(*, now=None):
    """Count the complete open backlog without loading individual reports."""
    ranked = rank_report_queryset(
        Report.objects.filter(status__in=OPEN_REPORT_STATUSES), now=now,
    ).order_by()
    totals = ranked.values('report_priority_rank').annotate(total=Count('pk'))
    by_rank = {row['report_priority_rank']: row['total'] for row in totals}
    return {
        'priority_counts': {
            key: by_rank.get(rank, 0) for key, rank in PRIORITY_ORDER.items()
        },
        'open_total': sum(by_rank.values()),
        'priority_labels': PRIORITY_LABELS,
    }


def build_report_rows(reports, *, now=None):
    """Decorate just the already-paginated reports for the template."""
    now = now or timezone.now()
    rows = []
    for report in reports:
        priority = build_report_priority_for_report(report, now=now)
        feedback = getattr(report, 'feedback', None)
        rows.append({
            'report': report,
            **priority,
            'outcome_feedback': feedback.choice if feedback else None,
            'outcome_feedback_label': (
                OUTCOME_FEEDBACK_LABELS.get(feedback.choice) if feedback else None
            ),
        })
    return rows


def build_report_page(queryset, *, page_number, per_page=10, now=None):
    """Return a database-sorted page plus whole-backlog priority totals."""
    now = now or timezone.now()
    page_obj = Paginator(
        rank_report_queryset(queryset, now=now),
        per_page,
    ).get_page(page_number)
    summary = report_priority_summary(now=now)
    return {
        'page_obj': page_obj,
        'rows': build_report_rows(page_obj.object_list, now=now),
        **summary,
    }


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
