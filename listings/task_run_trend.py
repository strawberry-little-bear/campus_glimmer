# -*- coding: utf-8 -*-
"""Say whether the schedule is doing better or worse than it was.

`build_task_run_health` answers questions about one window: how often did this
command run, is it failing right now, how long has it been since it succeeded.
Those are the questions an operator asks first, and every one of them is a
question about a single period. The question that comes next - is this month
better or worse than last month - cannot be answered by reading one window
harder, because a command that was broken for the first three weeks of a
30-day window and healthy for the last one looks exactly like a command that
was healthy throughout and then broke, when all that is reported is the
average over the whole window.

Two comparisons are missing and they answer different questions. The success
rate across two equal-length windows says whether the schedule is becoming
more or less reliable. The duration of a single run says whether it is becoming
slower, and the health panel cannot say that at all: it reports one mean, and a
mean is the one statistic a slow run cannot move. A digest that takes four
seconds eleven times and four minutes once has a mean of thirty-five seconds,
which describes no run that ever happened. Median, maximum and the most recent
value are reported instead, together with a count of the runs that took several
times longer than the period's own median, so "one run hung" is visible as a
number rather than hidden inside an average.

Four boundaries keep the comparison honest.

A run belongs to the window it *started* in. The ledger records both ends, and
attributing a run to the window it finished in would be wrong in the direction
that matters: a command that hung for two hours near the boundary would be
counted in the next window, so a bad period would be reported as a good one
followed by a slow one. Both windows are cut by one clock, the start, using the
same local-midnight bounds `search_trend` uses, so a run counted in one window
here is counted in the same window everywhere else on the dashboard.

Dry runs are excluded, for the reason the health module excludes them: a run
that sends nothing cannot vouch for the schedule, and its duration measures
nothing about it.

A window with too few runs is not compared. A command that ran three times has
a success rate that moves in steps of a third, so a difference between two such
windows is arithmetic rather than evidence. Below the floor both windows are
still reported as figures and the direction is withheld - the same rule every
other cross-period panel on the dashboard follows.

Run counts are never given a direction. A command that ran twelve times instead
of six had a busier period, not a trend, and labelling it "rising" would teach
the reader to read a volume change as a quality change. Only the success rate
and the duration median carry a direction word.

Nothing here adjusts anything. The comparison does not change `STALE_AFTER`,
does not re-run a failed command, does not reorder the schedule, and does not
write back to the ledger. A statistic that moved a schedule would turn the
ledger into a place where the record is shaped by what it reports.

Only the columns this module needs are read, so the cost of the comparison does
not grow with how many runs have accumulated.
"""

from datetime import timedelta

from django.utils import timezone

from .models import TaskRun
from .search_trend import TREND_DELTA_POINTS, _period_bounds

# Percentage-point thresholds for direction, taken verbatim from the search
# trend comparison. The panels sit on one dashboard and are read side by side;
# a reader who learns that "rising" means ten points on one should not have to
# learn a second number for the other.
TREND_DIRECTIONS = ('rising', 'falling', 'flat', 'new', 'gone', 'insufficient')

TREND_LABELS = {
    'rising': '较上期上升',
    'falling': '较上期下降',
    'flat': '较上期持平',
    'new': '本期新出现',
    'gone': '本期已消失',
    'insufficient': '样本不足',
}

# How many runs a window needs before its figures are compared at all. Below
# this the rate moves in steps too large to call a change, and the panel must
# not become the place where evidence too thin one table over is thick enough
# to read a trend from.
MIN_PERIOD_RUNS = 4

# How far the duration median must move, as a share of the previous median,
# before it is called rising or falling. A rate can be compared in percentage
# points because both sides already are percentages; a duration in milliseconds
# has no such scale, so its threshold has to be relative. A quarter is chosen
# because it is roughly where a student would notice a command taking longer,
# and because a digest that got ten percent slower is noise on a machine that
# is also serving traffic.
DURATION_DRIFT = 0.25

# A run counts as slow when it took at least this multiple of the period's own
# median. The median is used rather than a fixed number of milliseconds because
# the seven commands differ by orders of magnitude, and because a fixed number
# would make the flag drift as the machine does. It is computed inside the
# period it flags, so it says "slow for this command in this window" and
# nothing more.
SLOW_RUN_FACTOR = 3

# The metrics worth comparing, in the order the panel reads them. The success
# rate leads because it is the figure that answers whether the schedule is
# becoming more reliable; the duration figures follow because two periods can
# share a success rate while one of them started hanging.
COMPARED_METRICS = (
    ('run_count', '运行次数', ''),
    ('failed_count', '失败次数', ''),
    ('success_rate', '成功率（%）', '%'),
    ('median_duration_ms', '耗时中位数（毫秒）', 'ms'),
    ('max_duration_ms', '最长耗时（毫秒）', 'ms'),
    ('slow_run_count', '异常慢的运行次数', ''),
)

# Only these metrics are given a direction. A run count that grew is a busier
# period, not a trend, and calling it rising would teach the reader to read a
# volume change as a quality change.
DIRECTION_METRICS = ('success_rate', 'median_duration_ms')

# What a direction means for each metric that carries one. The same word means
# opposite things depending on what moved, so the panel has to be told which
# way is good: a rising success rate is good news and a rising duration is not.
DIRECTION_TONES = {
    ('success_rate', 'rising'): 'good',
    ('success_rate', 'falling'): 'bad',
    ('median_duration_ms', 'rising'): 'bad',
    ('median_duration_ms', 'falling'): 'good',
}

# Only these columns are read, so the cost does not grow with run count.
PERIOD_FIELDS = ('name', 'status', 'started_at', 'duration_ms', 'note')


def _rate(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _median(values):
    """The median, or None when there is nothing to take the middle of."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return int((ordered[middle - 1] + ordered[middle]) / 2)


def _window_figures(runs):
    """Figures for one command's runs inside one window.

    The duration figures are median, maximum and most recent rather than a
    mean, because a mean is the one statistic a hung run cannot move. `runs`
    arrives ordered by descending start time, so the first success is the most
    recent one and no second pass is needed to find it.
    """
    succeeded = [run for run in runs if run['status'] == 'succeeded']
    durations = sorted(
        run['duration_ms'] for run in succeeded if run['duration_ms'] is not None
    )
    median = _median(durations)
    slow = 0
    if median:
        slow = sum(1 for value in durations if value >= median * SLOW_RUN_FACTOR)
    last_duration = None
    for run in succeeded:
        last_duration = run['duration_ms']
        break
    return {
        'run_count': len(runs),
        'failed_count': len(runs) - len(succeeded),
        'success_rate': _rate(len(succeeded), len(runs)),
        'median_duration_ms': median,
        'max_duration_ms': max(durations) if durations else None,
        'last_duration_ms': last_duration,
        'slow_run_count': slow,
    }


def _classify_direction(key, current, previous):
    """The direction of one metric that carries one, or None for the rest."""
    if key not in DIRECTION_METRICS or current is None or previous is None:
        return None
    if key == 'success_rate':
        delta = current - previous
        if delta >= TREND_DELTA_POINTS:
            return 'rising'
        if delta <= -TREND_DELTA_POINTS:
            return 'falling'
        return 'flat'
    if not previous:
        # A previous median of zero carries no scale to drift against.
        return 'insufficient'
    drift = (current - previous) / previous
    if drift >= DURATION_DRIFT:
        return 'rising'
    if drift <= -DURATION_DRIFT:
        return 'falling'
    return 'flat'


def _metric_rows(current, previous, *, has_sample):
    """One row per compared metric, with its delta and direction.

    A metric present in only one of the two windows is reported without a
    delta, because there is no earlier figure to subtract from and inventing
    one would be arithmetic on a number nobody measured.
    """
    rows = []
    for key, label, unit in COMPARED_METRICS:
        current_value = current.get(key)
        previous_value = previous.get(key)
        if current_value is None or previous_value is None:
            delta = None
            delta_display = '—'
        else:
            delta = round(current_value - previous_value, 1)
            delta_display = f'{delta:+.1f}'

        if key in DIRECTION_METRICS:
            direction = _classify_direction(key, current_value, previous_value)
            if not has_sample or direction is None:
                direction = 'insufficient'
            direction_label = TREND_LABELS[direction]
        else:
            direction = None
            direction_label = ''

        rows.append({
            'key': key,
            'label': label,
            'unit': unit,
            'current': current_value,
            'previous': previous_value,
            'delta': delta,
            'delta_display': delta_display,
            'direction': direction,
            'direction_label': direction_label,
            'has_direction': key in DIRECTION_METRICS,
        })
    return rows


def _command_reading(metric_rows):
    """Which metric drove this command's direction, and which way is good.

    The success rate is read before the duration median, because a schedule
    that stopped working and a schedule that got slower need different
    responses and the first one is the more urgent of the two. The tone records
    which way is good for the metric that actually moved, so the panel can
    colour a rising duration as bad news without a second lookup.
    """
    for key in DIRECTION_METRICS:
        row = next((item for item in metric_rows if item['key'] == key), None)
        if row and row['direction'] in ('rising', 'falling'):
            return {
                'driver_key': key,
                'driver_label': row['label'],
                'direction': row['direction'],
                'direction_label': row['direction_label'],
                'delta_display': row['delta_display'],
                'tone': DIRECTION_TONES[(key, row['direction'])],
            }
    return {
        'driver_key': None,
        'driver_label': '',
        'direction': 'flat',
        'direction_label': TREND_LABELS['flat'],
        'delta_display': '',
        'tone': 'flat',
    }


def _conclusion(commands, *, current, previous, has_sample):
    """One sentence naming what actually moved.

    The sentence leads with the command whose success rate moved, because that
    is the figure that answers whether the schedule is becoming more reliable,
    and it is the one a reader acts on. When either window is below the floor
    it says so instead of picking a winner: a delta computed from four runs is
    a coin flip described as a trend.
    """
    if not current['run_count'] and not previous['run_count']:
        return '两个周期内都没有已记录的运行，调度趋势需要先有运行台账。'
    if not has_sample:
        return '任一周期的运行次数达不到门槛，暂时无法与上一周期比较成功率与耗时趋势。'

    moved = [row for row in commands if row['tone'] != 'flat']
    if not moved:
        return '与上一周期相比，各命令的成功率与耗时中位数都没有明显变化。'

    parts = []
    for row in moved[:3]:
        parts.append(f"{row['name']} {row['driver_label']}{row['delta_display']}")
    sentence = '与上一周期相比，' + '、'.join(parts) + '。'
    if current['slow_run_count']:
        sentence += (
            f"本周期有 {current['slow_run_count']} "
            f"次运行耗时达到当期中位数的 "
            f"{SLOW_RUN_FACTOR} 倍以上。"
        )
    return sentence


def build_task_run_trend(days=30, *, now=None):
    """Compare this period's schedule health with the equal-length one before it.

    `days` selects the runs being reported, the same way `build_task_run_health`
    uses it. Both windows are cut by the run's own start time using the same
    local-midnight bounds the search trend comparison uses, so the two panels
    cannot disagree about which period a run belongs to.

    Only commands that ran in at least one of the two windows are returned. A
    command that never ran in either is a question for the health panel, which
    already reports it as stale; repeating it here would fill the table with
    rows whose two windows are both empty and whose only possible direction is
    "insufficient".
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

    current_rows = {}
    previous_rows = {}
    for row in rows:
        # Attribution is by the start time, never the finish time: a run that
        # hung across the boundary belongs to the period that had to live with
        # it, not to the one that inherited the slowness.
        if row['started_at'] >= start:
            current_rows.setdefault(row['name'], []).append(row)
        else:
            previous_rows.setdefault(row['name'], []).append(row)

    commands = []
    for name in sorted(set(current_rows) | set(previous_rows)):
        current = _window_figures(current_rows.get(name, []))
        previous = _window_figures(previous_rows.get(name, []))
        has_sample = (
            current['run_count'] >= MIN_PERIOD_RUNS
            and previous['run_count'] >= MIN_PERIOD_RUNS
        )
        metric_rows = _metric_rows(current, previous, has_sample=has_sample)
        reading = _command_reading(metric_rows)
        commands.append({
            'name': name,
            'current': current,
            'previous': previous,
            'metric_rows': metric_rows,
            'has_sample': has_sample,
            'driver_key': reading['driver_key'],
            'driver_label': reading['driver_label'],
            'direction': reading['direction'],
            'direction_label': reading['direction_label'],
            'delta_display': reading['delta_display'],
            'tone': reading['tone'],
        })

    # The overall figures are read across every command in the window, so a
    # period in which one command stopped running and another doubled is still
    # visible as a change in the totals rather than cancelling out.
    overall_current = _window_figures(
        [row for row in rows if row['started_at'] >= start]
    )
    overall_previous = _window_figures(
        [row for row in rows if row['started_at'] < start]
    )
    has_sample = (
        overall_current['run_count'] >= MIN_PERIOD_RUNS
        and overall_previous['run_count'] >= MIN_PERIOD_RUNS
    )

    return {
        'days': days,
        'min_period_runs': MIN_PERIOD_RUNS,
        'trend_delta_points': TREND_DELTA_POINTS,
        'duration_drift_percent': int(DURATION_DRIFT * 100),
        'slow_run_factor': SLOW_RUN_FACTOR,
        'current_period_start': start,
        'previous_period_start': previous_start,
        'current_period_end': now,
        'current': overall_current,
        'previous': overall_previous,
        'metric_rows': _metric_rows(
            overall_current, overall_previous, has_sample=has_sample,
        ),
        'commands': commands,
        'has_data': bool(rows),
        'has_sample': has_sample,
        'summary': _conclusion(
            commands,
            current=overall_current, previous=overall_previous,
            has_sample=has_sample,
        ),
    }