# -*- coding: utf-8 -*-
"""Say whether each kind of failure is getting better or worse than it was.

`build_task_run_failure_causes` answers "what did the failures of this period
look like": nine external failures and nine configuration failures are two
different problems, and an operator needs to know which one is on the desk.
That is a question about one window, and every figure in it is a count over
that window. The question that comes next - is the external dependency getting
worse - cannot be answered by reading one window harder, because fifteen
external failures means completely different things depending on where they
came from. Fifteen that were two last period is a dependency falling over, and
the number to act on is the thirteen extra. Fifteen that were fifteen last
period is a dependency that has been quietly failing all along, and acting on
the fifteen is the wrong response: it was never healthy, so what needs fixing
is not a change. A window total cannot tell those two apart, because both are
fifteen.

Cutting the same failures by cause and then by period is what makes the
difference visible. The cause cut alone says which shape of failure there is;
the period cut alone says how many there are; the two together say which shape
is moving. The cross-period health panel already compares totals per command,
and the failure-cause panel already aggregates by shape, but no panel answers
"this shape, now against then", which is the question that decides whether to
open an incident or to leave a long-standing problem alone.

Six boundaries keep the comparison honest.

A run belongs to the period it *started* in, cut by the same local-midnight
bounds every other cross-period panel on the dashboard uses. Two panels that
disagree about which period a run falls in would make the same failure appear
as rising here and as part of a healthy period there, and the reader has no way
to tell which one is lying.

Dry runs are excluded, for the reason every other schedule panel excludes them:
a run that sends nothing cannot vouch for the schedule, and its failure says
nothing about it either.

A cause below the floor is reported as a figure with no direction. Failures are
rarer than runs, so the floors are lower here than the one the health trend
uses, but they exist for the same reason. One floor is on the cause itself,
because a single failure is not a pattern. The other is on the period's total,
because the direction is measured as a share and a share taken over a handful
of failures moves in steps too large to mean anything - a period with one
failure in it has a cause holding a hundred percent of it. Both are deliberately
low enough to let a cause that went from two failures to fifteen be called
rising: that move is what this module exists to report, and a floor above it
would answer "insufficient" to the question the panel was built for. Below
either floor both periods are still reported as figures and the direction is
withheld, the same rule the other cross-period panels follow.

The direction is about failure counts only, and rising is bad news. A cause
that failed more often is a cause getting worse; a cause that failed less
often is a cause recovering. Run counts are never given a direction, because a
busier period is not a healthier one, and a reader who learned that on the
health panel should not have to unlearn it here.

The threshold is relative rather than a fixed number of failures, and it has
to be. The ten-point threshold the other cross-period panels use is a
percentage-point threshold, which only means something for a figure that is
already a percentage; a raw count has no such scale, and applying that number
here would mean a cause going from two failures to eleven was "flat" while the
same cause going from twenty to twenty-nine was "rising" - two moves of nine
failures, read as noise and as a trend. The share of the period's own failures
is the scale-free version of the same question, and the floor keeps a small
denominator from producing a direction out of nothing.

A cause present in only one period is reported as new or gone, with no delta.
There is no earlier figure to subtract from, and inventing one would be
arithmetic on a number nobody measured.

Nothing here decides anything. The module reuses the classifier rather than
carrying a second copy of the exception-name table, because two copies of that
table would drift, and a drift would mean the same failure counted as external
on one panel and as unknown on this one. It does not reclassify a failure, does
not change the category table, does not re-run anything and does not write back
to the ledger. A statistic that moved a schedule would turn the ledger into a
place where the record is shaped by what it reports.

Only the columns this module needs are read, so the cost of the comparison does
not grow with how many runs have accumulated.
"""

from .models import TaskRun
from .task_run_failure_causes import (
    CAUSE_CONFIG,
    CAUSE_DATA,
    CAUSE_EXTERNAL,
    CAUSE_HINTS,
    CAUSE_LABELS,
    CAUSE_ORDER,
    CAUSE_UNKNOWN,
    build_task_run_failure_causes,
    classify_failure_note,
)
from .task_run_trend import TREND_DIRECTIONS, TREND_LABELS, _period_bounds

# How many failures one cause needs in *each* period before the two are
# compared. Failures are rarer than runs, so the floor is lower than the health
# trend's, but it exists for the same reason: a single failure is not a pattern,
# and describing one as a direction would make the panel a place where noise is
# read as signal. Two is deliberately low enough to let a cause that went from
# two failures to fifteen be called rising - that move is the one this module
# exists to report, and a floor above it would answer "insufficient" to the
# question the panel was built for.
MIN_CAUSE_RUNS = 2

# How many failures a period needs in total before any of its causes are
# compared. The direction is measured in a cause's share of the period's
# failures, so the denominator is what has to be thick: a period with one
# failure in it has a cause holding a hundred percent of it, and a share taken
# over one failure moves in steps too large to mean anything. This is the floor
# that stands where the health trend's per-command run floor stands, and it is
# set here rather than on the cause because the cause count is already covered
# above.
MIN_PERIOD_FAILURES = 5

# How far a cause's share of the period's failures has to move before it is
# called rising or falling. The share is used rather than a fixed number of
# failures because the period's own total is the only scale a count has: two
# failures against one is a doubling, and twenty against nineteen is noise, and
# no single number of failures separates those two. The percentage-point
# threshold the other panels use cannot be borrowed here - it is a threshold on
# a figure that is already a percentage, and a raw count is not one.
CAUSE_SHARE_DRIFT = 0.2

# Which way is good for each direction a cause can carry. Rising is the only
# direction that needs spelling out, because a failure count that grew is the
# one that sends somebody to look at a dependency.
DIRECTION_TONES = {
    'rising': 'bad',
    'falling': 'good',
    'flat': 'flat',
}

# The order rows are read in. A direction is what the reader came for, so the
# rows that carry one come first, worst first: a cause that is rising is the one
# somebody has to go and look at, and burying it under rows that merely exist
# would hide the finding the panel exists to report.
_DIRECTION_RANK = {
    'rising': 0,
    'falling': 1,
    'flat': 2,
    'new': 3,
    'gone': 4,
    'insufficient': 5,
}

# Only these columns are read, so the cost does not grow with run count.
PERIOD_FIELDS = ('name', 'status', 'started_at', 'note')


def _cause_buckets(rows):
    """Failures per cause, with the commands and the newest note for each.

    The cause of one run comes from `classify_failure_note`, which is the only
    place the exception-name table lives. Reading it through that function is
    what keeps this panel and the failure-cause panel counting the same failure
    the same way, and it is why this module carries no table of its own.
    """
    buckets = {}
    for row in rows:
        if row['status'] != 'failed':
            continue
        cause = classify_failure_note(row['note'])
        bucket = buckets.setdefault(cause, {
            'cause': cause,
            'count': 0,
            'commands': {},
            'last_failure_at': None,
            'last_note': '',
        })
        bucket['count'] += 1
        bucket['commands'][row['name']] = bucket['commands'].get(row['name'], 0) + 1
        started_at = row['started_at']
        if bucket['last_failure_at'] is None or started_at > bucket['last_failure_at']:
            bucket['last_failure_at'] = started_at
            bucket['last_note'] = row['note'] or ''
    for bucket in buckets.values():
        bucket['command_rows'] = [
            {'name': name, 'count': count}
            for name, count in sorted(
                bucket['commands'].items(), key=lambda item: (-item[1], item[0]),
            )
        ]
        bucket['command_count'] = len(bucket['command_rows'])
        del bucket['commands']
    return buckets


def _direction(current_count, previous_count, *, current_total, previous_total):
    """The direction of one cause, from the two periods' failure counts.

    A cause that exists in only one period has nothing to subtract, so it is
    reported as new or gone rather than as a delta of fifteen. A cause below the
    floor in either period is reported with its direction withheld: the figures
    are still there for the reader, but the panel does not claim to know which
    way it is moving.

    The move is measured in the cause's share of the period's own failures,
    because a raw count has no scale of its own: two failures against one is a
    doubling and twenty against nineteen is noise, and no fixed number of
    failures separates those two. The share is the scale-free version of the
    same question.
    """
    if current_count is None or previous_count is None:
        return 'new' if current_count is not None else 'gone'
    if current_count < MIN_CAUSE_RUNS or previous_count < MIN_CAUSE_RUNS:
        return 'insufficient'
    if current_total < MIN_PERIOD_FAILURES or previous_total < MIN_PERIOD_FAILURES:
        return 'insufficient'
    current_share = current_count / current_total
    previous_share = previous_count / previous_total
    drift = current_share - previous_share
    if drift >= CAUSE_SHARE_DRIFT:
        return 'rising'
    if drift <= -CAUSE_SHARE_DRIFT:
        return 'falling'
    return 'flat'


def _cause_rows(current_buckets, previous_buckets):
    """One row per cause that failed in either period, worst direction first."""
    current_total = sum(bucket['count'] for bucket in current_buckets.values())
    previous_total = sum(bucket['count'] for bucket in previous_buckets.values())
    rows = []
    for cause in CAUSE_ORDER:
        current = current_buckets.get(cause)
        previous = previous_buckets.get(cause)
        if current is None and previous is None:
            continue
        current_count = current['count'] if current else None
        previous_count = previous['count'] if previous else None
        direction = _direction(
            current_count, previous_count,
            current_total=current_total, previous_total=previous_total,
        )

        if current_count is None or previous_count is None:
            # One-sided causes have no delta to show, and showing a blank one
            # would read as "no change" rather than "nothing to compare with".
            delta = None
            delta_display = '—'
        else:
            delta = current_count - previous_count
            delta_display = f'{delta:+d}'

        current_share = (
            round(current_count / current_total * 100, 1)
            if current_count and current_total else None
        )
        previous_share = (
            round(previous_count / previous_total * 100, 1)
            if previous_count and previous_total else None
        )
        bucket = current or previous
        rows.append({
            'cause': cause,
            'label': CAUSE_LABELS[cause],
            'hint': CAUSE_HINTS[cause],
            'current_count': current_count,
            'previous_count': previous_count,
            'current_share': current_share,
            'previous_share': previous_share,
            'current_display': '—' if current_count is None else current_count,
            'previous_display': '—' if previous_count is None else previous_count,
            'delta': delta,
            'delta_display': delta_display,
            'direction': direction,
            'direction_label': TREND_LABELS[direction],
            'tone': DIRECTION_TONES.get(direction, 'flat'),
            'command_count': bucket['command_count'],
            'commands': bucket['command_rows'],
            'last_failure_at': bucket['last_failure_at'],
            'last_note': bucket['last_note'],
        })

    rows.sort(key=lambda row: (
        _DIRECTION_RANK[row['direction']],
        -(row['current_count'] or 0),
        CAUSE_ORDER.index(row['cause']),
    ))
    return rows


def _summary_text(rows, *, current_total, previous_total):
    """One sentence naming the cause that actually moved.

    The sentence leads with the rising cause, because that is the one that
    decides whether somebody opens an incident. A steady cause is named only
    when nothing moved, so a reader who sees no rising row is told that the
    failures were there before rather than left to wonder whether the panel
    found anything at all.
    """
    if not current_total and not previous_total:
        return '两个周期内都没有失败的运行记录。'
    rising = [row for row in rows if row['direction'] == 'rising']
    if rising:
        top = rising[0]
        return (
            f"与上一周期相比，「{top['label']}」的失败次数从 {top['previous_count']} 次"
            f"升到 {top['current_count']} 次，是本期最需要先看的类别。"
        )
    falling = [row for row in rows if row['direction'] == 'falling']
    if falling:
        top = falling[0]
        return (
            f"与上一周期相比，「{top['label']}」的失败次数从 {top['previous_count']} 次"
            f"降到 {top['current_count']} 次，本期没有出现上升的失败类别。"
        )
    if not rows:
        return f'本周期 {current_total} 次失败、上一周期 {previous_total} 次失败，没有可比较的类别。'
    return (
        f'本周期 {current_total} 次失败、上一周期 {previous_total} 次失败，'
        f'各类原因的失败次数都没有明显变化。'
    )


def build_task_run_failure_cause_trend(days=30, *, now=None):
    """Compare each failure cause of this period with the one before it.

    Both windows are cut by the run's own start time using the same
    local-midnight bounds the search trend comparison uses, so this panel
    cannot disagree with the rest of the dashboard about which period a
    failure belongs to. `days` selects the current window and the previous one
    is the equal-length window immediately before it, the same way every other
    cross-period panel on the dashboard reads it.

    Dry runs are excluded for the reason the failure-cause module excludes
    them: a run that sends nothing cannot vouch for anything, and its failure
    says nothing about the schedule either.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start, previous_start = _period_bounds(now, days)

    rows = list(
        TaskRun.objects.filter(started_at__gte=previous_start, started_at__lte=now)
        .exclude(note='dry-run')
        .order_by('-started_at')
        .values(*PERIOD_FIELDS)
    )

    # Attribution is by the start time, never the finish time: a run that hung
    # across the boundary belongs to the period that had to live with it, not
    # to the one that inherited the failure.
    current_rows = [row for row in rows if row['started_at'] >= start]
    previous_rows = [row for row in rows if row['started_at'] < start]

    current_buckets = _cause_buckets(current_rows)
    previous_buckets = _cause_buckets(previous_rows)
    cause_rows = _cause_rows(current_buckets, previous_buckets)
    current_total = sum(bucket['count'] for bucket in current_buckets.values())
    previous_total = sum(bucket['count'] for bucket in previous_buckets.values())

    return {
        'days': days,
        'min_cause_runs': MIN_CAUSE_RUNS,
        'min_period_failures': MIN_PERIOD_FAILURES,
        'cause_share_drift_percent': int(CAUSE_SHARE_DRIFT * 100),
        'window_start': start,
        'window_end': now,
        'previous_window_start': previous_start,
        'causes': cause_rows,
        'total_failed': current_total,
        'previous_total_failed': previous_total,
        'has_data': bool(current_rows or previous_rows),
        'has_direction': any(
            row['direction'] in ('rising', 'falling') for row in cause_rows
        ),
        'summary': _summary_text(
            cause_rows, current_total=current_total, previous_total=previous_total,
        ),
    }