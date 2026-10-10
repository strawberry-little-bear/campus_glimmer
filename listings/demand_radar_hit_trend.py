# -*- coding: utf-8 -*-
"""Ask whether the demand radar is getting more trustworthy over time.

`build_demand_radar_outcomes` answers a single-number question: of the tasks
raised in this window, what share closed their gap. That share is the only
figure that says whether the radar is worth believing, and a single number
cannot say whether it is improving. A radar at 50% hit rate is either a radar
being tuned or a radar that never worked, and those two need different
responses - one needs patience, the other needs the scoring revisited before
any more supply is moved.

This module therefore splits the same tasks into two equal-length windows
using the same local-midnight bounds `search_trend` uses, and reports the hit
rate of each alongside the median convergence magnitude. Direction is read
from the difference between the two, with the same vocabulary the other
cross-period panels use, because they sit on one dashboard and a reader who
learns that "rising" means ten points on one panel should not have to learn a
second number for another.

The comparison is built on task creation time rather than on the window in
which each task was judged. A task is scored on searches that happen after it
exists, so a task created at the end of a period has almost all of its
observation window in the next one. Attributing such a task to the period its
result landed in would let a quiet month inherit the results of a busy one and
would move the rate whenever the scheduler did. Both sides are therefore cut
by one clock, the moment the decision was made.

Three boundaries keep the comparison honest.

The hit rate is computed only over tasks with usable evidence. Tasks whose
baseline or observation window held too few searches are insufficient-sample
cases in `build_demand_radar_outcomes` and are held out of the denominator
here too. Counting them as failures would punish a period for a topic nobody
searched for; counting them as successes would flatter it. The count of held
out tasks is reported next to the rate so a reader can see how much evidence
the rate stands on.

A period with too few judged tasks is not compared. Three tasks produce a hit
rate that moves in steps of a third, so a change between them is arithmetic
rather than evidence. The same floor the alert builder uses for the hit-rate
alert is reused here, which keeps the panel and the alert from disagreeing
about when the figure is meaningful.

Nothing here adjusts anything. The score weights in `demand_radar` are left
exactly as they are. Feeding the hit rate back into the opportunity score
would let the radar learn to stop reporting gaps it cannot prove it fixed,
which is the opposite of what the figure is for; a radar that quietly
downgrades its own misses is worse than one that reports them. This module
reports the trend so a human can decide whether to revisit the weights.
"""
from datetime import timedelta

from django.utils import timezone

from .models import DemandOpportunityTask
from .demand_radar_outcome import (
    DEFAULT_WINDOW_DAYS, build_demand_radar_outcomes, build_task_outcome,
)
from .search_trend import TREND_DELTA_POINTS, _period_bounds

# Percentage-point thresholds for direction, taken from the search trend
# comparison so every cross-period panel on the dashboard speaks one language.
TREND_DIRECTIONS = ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient')

TREND_LABELS = {
    'rising': '较上期上升',
    'falling': '较上期下降',
    'flat': '较上期持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

# How many judged tasks a period needs before its hit rate is compared at all.
# Below this the rate moves in steps too large to call a change, and the panel
# must not become the place where evidence too thin for the alert is thick
# enough to read a trend from. The alert builder uses the same floor.
MIN_JUDGED_TASKS = 3

# The metrics worth comparing, in the order the panel reads them. The hit rate
# leads because it is the figure that answers whether the radar is worth
# believing; the median magnitude follows because two periods can share a hit
# rate while one closes its gaps decisively and the other barely.
COMPARED_METRICS = (
    ('hit_rate', '闭环命中率（%）'),
    ('median_delta_points', '中位收敛幅度（百分点）'),
    ('judged_count', '有证据的任务数'),
    ('task_count', '创建任务数'),
    ('converged_count', '缺口收敛'),
    ('flat_count', '基本持平'),
    ('diverged_count', '缺口扩大'),
    ('insufficient_count', '样本不足'),
    ('supply_added_total', '累计新增供给'),
)

METRIC_UNITS = {
    'hit_rate': '%',
    'median_delta_points': 'pt',
    'judged_count': '',
    'task_count': '',
    'converged_count': '',
    'flat_count': '',
    'diverged_count': '',
    'insufficient_count': '',
    'supply_added_total': '',
}

# Only these outcomes are given a direction, because they are the ones whose
# change carries a verdict. The counts are context for the rate, and reading
# a direction off "how many tasks were created" would describe the calendar
# rather than the radar.
DIRECTION_METRICS = ('hit_rate', 'median_delta_points')


def _rate(converged, judged):
    return round(converged / judged * 100, 1) if judged else None


def _summarise(rows):
    """Aggregate the per-task verdicts into the figures this panel compares.

    The same arithmetic `build_demand_radar_outcomes` uses, applied to the rows
    that survived the window filter below. It is repeated here rather than
    imported because that function takes its window as a number of days back
    from "now", and a period bounded by local midnight cannot be expressed that
    way - the two boundaries would drift by however far into the day "now"
    happens to fall.
    """
    scored = [row for row in rows if row['outcome'] != 'pending']
    converged = sum(row['outcome'] == 'converged' for row in scored)
    insufficient = sum(row['outcome'] == 'insufficient' for row in scored)
    judged = len(scored) - insufficient
    deltas = [row['delta_points'] for row in scored if row['delta_points'] is not None]
    median_delta = None
    if deltas:
        ordered = sorted(deltas)
        middle = len(ordered) // 2
        median_delta = (
            ordered[middle]
            if len(ordered) % 2
            else round((ordered[middle - 1] + ordered[middle]) / 2, 1)
        )
    return {
        'task_count': len(rows),
        'pending_count': sum(row['outcome'] == 'pending' for row in rows),
        'judged_count': judged,
        'converged_count': converged,
        'flat_count': sum(row['outcome'] == 'flat' for row in scored),
        'diverged_count': sum(row['outcome'] == 'diverged' for row in scored),
        'insufficient_count': insufficient,
        'hit_rate': _rate(converged, judged),
        'median_delta_points': median_delta,
        'supply_added_total': sum(max(0, row['supply_delta']) for row in scored),
    }


def _window_figures(*, start, end, window_days, limit):
    """Score the tasks created inside one exact window.

    The task set is fetched with a span that certainly covers the window and
    then narrowed in Python by each task's own creation instant, which is the
    only way to honour a boundary set at local midnight.

    Each task is judged as of its own maturity moment rather than as of the
    window's end. A task's evidence is fixed the moment its observation window
    closes: searches before that moment are baseline, searches after it belong
    to a market the task no longer describes. Judging at the window's end
    instead would mark every task whose window had not yet closed as pending,
    which silently empties the previous period of exactly the tasks that were
    created earliest in it - the ones a reader would most want compared.

    The moment used is therefore `created_at + window_days` and nothing else:
    the same moment for a task in this period and for a task in the previous
    one, so neither window's boundary can decide how much evidence a task is
    allowed to have. A task whose observation window has not closed by then
    stays pending and is counted as such rather than dropped.
    """
    span_days = max(1, (end - start).days) + 1
    report = build_demand_radar_outcomes(
        days=span_days,
        now=end,
        limit=limit,
        **({'window_days': window_days} if window_days else {}),
    )
    window = window_days or DEFAULT_WINDOW_DAYS
    rows = []
    for row in report.get('rows', []):
        created_at = row.get('created_at')
        if created_at is None or not (start <= created_at <= end):
            continue
        if row['outcome'] == 'pending':
            rows.append(_rejudge_row(row, window=window))
        else:
            rows.append(row)
    figures = _summarise(rows)
    figures['start'] = start
    figures['end'] = end
    figures['rows'] = rows
    return figures


def _rejudge_row(row, *, window):
    """Score a task the outcome module left pending, as of its own maturity.

    `build_task_outcome` marks a task pending while its observation window is
    still open, which is right on the live panel: nobody wants to read a
    verdict on evidence that has not arrived yet. Here the question is what a
    closed period looked like, so the task is re-scored at the moment its own
    observation window closed. That moment belongs to the task, not to the
    period, so a task created early in a window is not left pending just
    because the window itself had not finished when it was read.

    A task whose observation window still has not closed by that moment stays
    pending on purpose: it is counted, not quietly dropped, and the panel says
    how many are still waiting.
    """
    # 任务自己的观察期结束时刻就是判定时刻，与周期边界无关。
    # 这样上一周期里最早创建的任务不会因为窗口还没关而被丢出分母。
    judged_at = row['created_at'] + timedelta(days=window)
    task = DemandOpportunityTask.objects.filter(pk=row['task_id']).first()
    if task is None:
        return row
    fresh = build_task_outcome(task, now=judged_at, window_days=window)
    # The panel compares tasks created inside one window, so the attribution
    # must not move while the evidence does.
    fresh['created_at'] = row['created_at']
    return fresh


def _classify_delta(delta_points):
    if delta_points is None:
        return 'insufficient'
    if delta_points >= TREND_DELTA_POINTS:
        return 'rising'
    if delta_points <= -TREND_DELTA_POINTS:
        return 'falling'
    return 'flat'


def _metric_rows(current, previous, *, has_sample):
    """One row per compared metric, with its direction.

    A metric that did not exist in the previous window is reported as new and
    one that vanished is reported as gone, in the same language the other
    panels use, but neither is given a delta: there is no earlier value to
    subtract from, and inventing one would be arithmetic on a number nobody
    measured.
    """
    rows = []
    for key, label in COMPARED_METRICS:
        current_value = current.get(key)
        previous_value = previous.get(key)
        delta = None
        delta_display = '—'
        if current_value is None and previous_value is None:
            direction = 'insufficient'
        elif previous_value is None:
            direction = 'new'
        elif current_value is None:
            direction = 'gone'
        else:
            delta = round(current_value - previous_value, 1)
            delta_display = f'{delta:+.1f}'
            direction = _classify_delta(delta)
        rows.append({
            'key': key,
            'label': label,
            'unit': METRIC_UNITS.get(key, ''),
            'current': current_value,
            'previous': previous_value,
            'delta': delta,
            'delta_display': delta_display,
            'direction': direction,
            'direction_label': TREND_LABELS[direction],
            'is_rate_metric': key in DIRECTION_METRICS,
        })
    if not has_sample:
        for row in rows:
            if row['direction'] in ('rising', 'falling', 'flat'):
                row['direction'] = 'insufficient'
                row['direction_label'] = TREND_LABELS['insufficient']
    return rows


def _conclusion(metric_rows, *, current, previous, has_sample):
    """One sentence naming what actually moved.

    The sentence leads with the hit rate rather than the raw task count,
    because a period with more tasks can hold the same hit rate without the
    radar getting any better. When either window is below the floor it says so
    instead of picking a winner: a delta computed from three tasks is a coin
    flip described as a trend.
    """
    if not has_sample:
        if not current['task_count'] and not previous['task_count']:
            return '两个周期内都没有创建跟进任务，命中率趋势需要先有任务发生。'
        return '有证据的任务数不足，暂时无法与上一周期比较命中率趋势。'

    moved = [
        row for row in metric_rows
        if row['direction'] in ('rising', 'falling') and row['is_rate_metric']
    ]
    if not moved:
        return '与上一周期相比，闭环命中率与中位收敛幅度都没有明显变化。'

    parts = []
    for row in moved[:3]:
        parts.append(f"{row['label']}{row['delta_display']}")
    return '与上一周期相比，' + '，'.join(parts) + '。'


def build_demand_radar_hit_trend(
    days=30, *, now=None, window_days=None, limit=100,
):
    """Compare this period's radar hit rate with the previous one.

    `days` selects the tasks being reported, the same way
    `build_demand_radar_outcomes` uses it: how far back they were created.
    `window_days` is passed straight through to control each task's own
    measurement span, and is left at the outcome module's default when not
    given so the two panels cannot drift apart.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start, previous_start = _period_bounds(now, days)
    previous_end = start - timedelta(microseconds=1)

    current = _window_figures(
        start=start, end=now, window_days=window_days, limit=limit,
    )
    # The previous window is measured as of its own end, not as of now, so a
    # task that has since matured does not change what the earlier period
    # looked like when it was closed.
    previous = _window_figures(
        start=previous_start, end=previous_end,
        window_days=window_days, limit=limit,
    )

    # 两个周期都要达到任务数门槛才给方向。只一边够时不给方向：那样等于
    # 用一个没有对照的周期去解释另一个，读起来像趋势，实际是缺口。
    has_sample = (
        current['judged_count'] >= MIN_JUDGED_TASKS
        and previous['judged_count'] >= MIN_JUDGED_TASKS
    )
    metric_rows = _metric_rows(current, previous, has_sample=has_sample)

    return {
        'days': days,
        'window_days': window_days,
        'min_judged_tasks': MIN_JUDGED_TASKS,
        'trend_delta_points': TREND_DELTA_POINTS,
        'current_period_start': start,
        'previous_period_start': previous_start,
        'current_period_end': now,
        'current': current,
        'previous': previous,
        'metric_rows': metric_rows,
        'has_data': bool(current['task_count'] or previous['task_count']),
        'has_sample': has_sample,
        'summary': _conclusion(
            metric_rows, current=current, previous=previous, has_sample=has_sample,
        ),
    }
