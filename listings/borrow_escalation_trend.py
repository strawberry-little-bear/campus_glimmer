# -*- coding: utf-8 -*-
"""Compare the borrow escalation queue with the period immediately before it.

`build_borrow_escalation_report` is a snapshot: how many borrows are open right
now, and how long the longest of them has been overdue. A snapshot cannot
answer the question an operator asks after a month of watching it - is this
getting worse, or is it the same as it was. Two identical snapshots one month
apart can hide a queue that has quietly doubled, and a snapshot that looks
alarming can be a single stubborn order that has been sitting there since the
term started. Direction lives in the difference between two equal-length
windows, not in either of them.

This module therefore reads the current period and the equal-length period
immediately before it - the same two windows `search_trend` uses, bounded by
the same local midnight, so a borrow counted here is counted there too.

A case is counted in the period its borrow was placed in, and the numerator is
read through that same borrow. Both sides of every rate are keyed to one clock;
that is the whole reason the borrow side is read first and the escalation rows
are attached to it. Keying the numerator to the escalation row instead would
pair an order placed at the end of one period with an escalation raised in the
next, and the rate would move whenever the scheduler did.

Four boundaries keep the comparison honest.

The denominator contains borrows that were never escalated. This is the same
rule `borrow_risk` uses and it matters more here, not less: a period in which
only escalated borrows were counted would show a 100% escalation rate in both
periods, the delta would be zero, and the panel would report perfect stability
on a queue that had tripled.

The ladder cannot reach back. `escalate_overdue_borrows` walks a lookback
window, so an order that aged past it before any run saw it stays un-escalated
for good and lands in the denominator as "never escalated". That is reported as
a low rate, not as a healthy queue, and it is the one way this panel can
understate a period - which is the direction an operator can afford.

Close duration is measured from the rung that actually happened, never from the
due date. An order that sat at level one for a week before anyone moved it is
not an order the operator spent a week on, and counting it that way would make
a period look slower than it was whenever the queue was quiet enough to let
cases age.

How overdue the still-open cases are is deliberately not compared. That figure
is the age of whatever happens to be open, so the previous period is always the
older one and the metric would fall every single time - a trend manufactured by
the calendar rather than by the queue. It belongs to the snapshot panel, which
answers "how bad is it right now"; what a cohort can honestly say about
duration is how long its closed cases took.

Sparse periods are reported as insufficient rather than compared. Five borrows
is the floor `borrow_risk` uses and it is reused verbatim: a cross-period delta
computed from three borrows is noise, and the panel must not become the place
where evidence that was too thin one table over is thin enough here.

Nothing here adjusts anything. The comparison reports; it does not move
`ESCALATION_SCHEDULE`, it does not reorder the governance queue, and it does
not mark a borrower. Escalation timing is a policy about how long a classmate's
item may be missing, and it must not shift because of a statistic.
"""

from datetime import timedelta

from django.utils import timezone

from .borrow_escalation import DEFAULT_LOOKBACK_DAYS
from .borrow_risk import MIN_SAMPLE_SIZE, _rate
from .models import BorrowReturnEscalation, Order
from .search_trend import TREND_DELTA_POINTS, _period_bounds

# Percentage-point thresholds for direction, taken verbatim from the search
# trend comparison. The two panels sit on the same dashboard and are read side
# by side; a reader who learns that "rising" means ten points on one should not
# have to learn a second number for the other.
TREND_DIRECTIONS = ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient')

TREND_LABELS = {
    'rising': '较上期上升',
    'falling': '较上期下降',
    'flat': '较上期持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

# The metrics worth comparing, in the order the panel reads them. Each entry
# names the field on both periods' figures and the label the table uses. Every
# one of them is a property of the cohort itself: how much of it there was, how
# much of it needed the ladder, and how long the closed part took. Nothing here
# is measured against today, because a number measured against today says more
# about today than about the period it claims to describe.
COMPARED_METRICS = (
    ('borrow_count', '周期内借用'),
    ('escalated_count', '发生过升级'),
    ('escalation_rate', '升级率（%）'),
    ('level_three_count', '拖到第三级'),
    ('level_three_rate', '第三级率（%）'),
    ('resolved_count', '周期内闭环'),
    ('average_close_days', '平均闭环时长（天）'),
    ('max_close_days', '最慢闭环（天）'),
)


def _classify_delta(delta_points):
    """Reuse the search trend thresholds so both panels speak one language."""
    if delta_points is None:
        return 'insufficient'
    if delta_points >= TREND_DELTA_POINTS:
        return 'rising'
    if delta_points <= -TREND_DELTA_POINTS:
        return 'falling'
    return 'flat'


def _window_figures(*, start, end):
    """Aggregate one window's borrows and whatever happened to them.

    Borrows are read from the order side so that never-escalated orders stay in
    the denominator, and the escalation rows are attached through the borrow.
    Both sides of every rate below are therefore cut by the same pair of
    instants, which is what makes the rate comparable with the next window's.
    """
    borrows = list(
        Order.objects
        .filter(
            item__trade_mode='borrow',
            created_at__gte=start,
            created_at__lte=end,
        )
        .select_related('item', 'item__category')
        .values(
            'id', 'status', 'return_due_at',
            'item__category__name',
        )
    )
    escalations = {
        row['order_id']: row
        for row in BorrowReturnEscalation.objects.filter(
            order__item__trade_mode='borrow',
            order__created_at__gte=start,
            order__created_at__lte=end,
        ).values(
            'order_id', 'escalation_level', 'resolved_at', 'last_escalated_at',
        )
    }

    escalated_count = 0
    level_three_count = 0
    resolved_count = 0
    close_durations = []
    for borrow in borrows:
        escalation = escalations.get(borrow['id'])
        if not escalation:
            continue
        escalated_count += 1
        if escalation['escalation_level'] >= 3:
            level_three_count += 1
        resolved_at = escalation['resolved_at']
        if resolved_at is None:
            continue
        resolved_count += 1
        # From the rung that actually happened, the same way borrow_risk does
        # it: an order that waited a week at level one is not an order the
        # operator spent a week on.
        started_at = escalation['last_escalated_at'] or borrow['return_due_at']
        if started_at:
            close_durations.append(
                max((resolved_at - started_at).total_seconds(), 0.0) / 86400
            )

    return {
        'borrow_count': len(borrows),
        'escalated_count': escalated_count,
        'escalation_rate': _rate(escalated_count, len(borrows)),
        'level_three_count': level_three_count,
        'level_three_rate': _rate(level_three_count, len(borrows)),
        'resolved_count': resolved_count,
        'average_close_days': (
            round(sum(close_durations) / len(close_durations), 1)
            if close_durations else None
        ),
        'max_close_days': (
            round(max(close_durations), 1) if close_durations else None
        ),
    }


def _metric_rows(current, previous):
    """One row per compared metric, with its delta and direction.

    A metric present in only one of the two windows is reported as new or gone
    rather than given a delta, because there is no earlier figure to subtract
    from. That is the same language the search trend comparison uses, and it
    matters here for the close-duration figures in particular: a period in which
    nothing closed has no average, and a delta from zero would read as an
    improvement rather than as "we do not know yet".
    """
    rows = []
    for key, label in COMPARED_METRICS:
        current_value = current[key]
        previous_value = previous[key]
        if current_value is None and previous_value is None:
            direction = 'insufficient'
            delta = None
            delta_display = '—'
        elif previous_value is None:
            direction = 'new'
            delta = None
            delta_display = '—'
        elif current_value is None:
            direction = 'gone'
            delta = None
            delta_display = '—'
        else:
            delta = round(current_value - previous_value, 1)
            delta_display = f'{delta:+.1f}'
            direction = _classify_delta(delta)
        rows.append({
            'key': key,
            'label': label,
            'current': current_value,
            'previous': previous_value,
            'delta': delta,
            'delta_display': delta_display,
            'direction': direction,
            'direction_label': TREND_LABELS[direction],
        })
    return rows


def _conclusion(metric_rows, *, current, previous, has_sample):
    """One sentence naming what actually moved.

    The sentence leads with the escalation rate rather than the raw count,
    because a period with twice the borrows will have twice the escalations
    without anything getting worse. When either window is below the floor it
    says so instead of picking a winner: a delta computed from three borrows is
    a coin flip described as a trend.
    """
    if not has_sample:
        if not current['borrow_count'] and not previous['borrow_count']:
            return '两个周期内都没有借用订单，时效趋势需要先有借用发生。'
        return '周期内借用单量不足，暂时无法与上一周期比较催收时效。'

    moved = [
        row for row in metric_rows
        if row['direction'] in ('rising', 'falling') and row['key'] != 'borrow_count'
    ]
    if not moved:
        return '与上一周期相比，升级率、第三级率和闭环时长都没有明显变化。'

    parts = []
    for row in moved[:3]:
        parts.append(f"{row['label']}{row['delta_display']}")
    return '与上一周期相比，' + '，'.join(parts) + '。'


def build_borrow_escalation_trend(
    *, days=DEFAULT_LOOKBACK_DAYS, now=None,
):
    """Compare this period's borrow escalation with the previous one."""
    now = now or timezone.now()
    days = max(1, int(days))
    start, previous_start = _period_bounds(now, days)

    current = _window_figures(start=start, end=now)
    previous = _window_figures(
        start=previous_start, end=start - timedelta(microseconds=1),
    )
    metric_rows = _metric_rows(current, previous)
    has_sample = (
        current['borrow_count'] >= MIN_SAMPLE_SIZE
        and previous['borrow_count'] >= MIN_SAMPLE_SIZE
    )

    return {
        'days': days,
        'min_sample_size': MIN_SAMPLE_SIZE,
        'trend_delta_points': TREND_DELTA_POINTS,
        'current_period_start': start,
        'previous_period_start': previous_start,
        'current_period_end': now,
        'current': current,
        'previous': previous,
        'metric_rows': metric_rows,
        'has_data': bool(current['borrow_count'] or previous['borrow_count']),
        'has_sample': has_sample,
        'summary': _conclusion(
            metric_rows, current=current, previous=previous, has_sample=has_sample,
        ),
    }
