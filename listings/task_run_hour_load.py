# -*- coding: utf-8 -*-
"""Say which hour the schedule is busy in, not only which command is slow.

Every figure the dashboard already reports about the schedule is cut by
command: health, cross-period trend, failure causes, percentiles. That is the
cut an operator reaches for first, because the first question is "which command
broke". It is also a cut with a hole in it. The seven commands share one disk,
one connection pool and whatever external quota the campus APIs allow, so two
commands that each take four seconds on their own take eleven when they are
configured to start in the same minute. Nothing is wrong with either command,
and no per-command panel can see it: each one looks like it got slower for no
reason, in the same window, and the only thing they have in common is the hour
they were started in.

An hour is the cut that makes that visible, and it is the cut nobody configured
anything by. Crontab lines are written one command at a time, so a schedule
grows by appending minutes, and the hour that ends up crowded is usually an
accident of the order the commands were added in.

Four boundaries keep this from becoming a second schedule.

Hours are local, never UTC. The tests and CI run in UTC and the project timezone
is Asia/Shanghai, eight hours ahead, so a run that starts at two in the morning
locally started at six the previous evening in UTC. Bucketing by UTC would put
congestion in an hour nobody configured a command in, and the panel would be
describing a machine that does not exist. Every hour in this module comes out of
`timezone.localtime`, which is the same clock the rest of the dashboard uses.

An hour with too few runs is not called crowded. Three runs is a coincidence;
two commands that happened to start together is not a queue. The floor is
reported alongside the rows, so a quiet panel says "the floor is three" rather
than leaving the reader to wonder whether an empty panel means no congestion or
not enough data.

A command is called slow in an hour against its own whole-window median, never
against the other commands in that hour. Comparing hours to each other would
flag every hour in which the slowest command ran, which is a ranking of commands
by speed wearing the word congestion. Comparing a command to its own usual run
answers the question the panel exists to ask: this command, in this hour, was
slower than it usually is.

Only the busiest hours are reported. Twenty-four rows of which twenty are
mostly empty is a calendar, not a finding, and a reader who has to scroll past
empty rows stops reading. The cap is reported too, so a reader knows the list is
the top of a distribution rather than the whole of it.

Nothing here adjusts anything. This module does not move a crontab line, does
not suggest a new minute, does not reorder the schedule and does not write back
to the ledger. A statistic that moved a schedule would turn the ledger into a
place where the record is shaped by what it reports.

Only the columns this module needs are read, so the cost does not grow with how
many runs have accumulated.
"""

from django.utils import timezone

from .models import TaskRun
from .task_run_trend import _median, _period_bounds

# How many runs an hour needs before it can be called crowded. Three is
# deliberately above the point where a reader would start to believe a queue was
# measured: two commands starting in the same hour is a coincidence, and three
# is still close to one command that ran three times.
MIN_HOUR_RUNS = 3

# How much slower than its own whole-window median a command has to be in one
# hour before that hour counts as slow for it. The multiple is relative because
# the seven commands differ by orders of magnitude, and a fixed number of
# milliseconds would drift as the machine does. Two is chosen because it is
# roughly where a run starts to be waited through rather than glanced at.
HOUR_SLOWDOWN_FACTOR = 2

# How many hour rows the panel reports. See the module docstring: a full day of
# mostly-empty rows is a calendar, and the hours worth reading are the busy ones.
MAX_HOUR_ROWS = 8

# Only these columns are read, so the cost does not grow with run count.
FIELDS = ('name', 'status', 'started_at', 'duration_ms', 'note')


def _by_hour(runs):
    """Bucket runs by the local clock hour they started in.

    `timezone.localtime` is what makes this local rather than UTC: it converts
    the aware timestamp into the project timezone before the hour is taken, so
    a run that started at 18:00 UTC on the previous evening lands in hour 2 and
    not in hour 18.
    """
    buckets = {}
    for run in runs:
        hour = timezone.localtime(run['started_at']).hour
        buckets.setdefault(hour, []).append(run)
    return buckets


def _command_baseline(runs):
    """Each command's median duration across the whole window.

    The baseline is computed over the window rather than inside one hour,
    because that is the "how this command usually is" figure the hour is
    compared against. It is per command for the same reason: a command that is
    uniformly slow must not make every hour it ran in look crowded.
    """
    by_command = {}
    for run in runs:
        if run['status'] != 'succeeded' or run['duration_ms'] is None:
            continue
        by_command.setdefault(run['name'], []).append(run['duration_ms'])
    return {
        name: _median(durations) for name, durations in by_command.items()
    }


def _slow_commands(runs, baselines):
    """The commands that ran slower in this hour than they usually do.

    A command is named here when its median inside the hour reaches the slowdown
    multiple of its own whole-window median. Commands with no baseline, or with
    no successful timed run in the hour, are not named: there is nothing to
    compare them against, and "slow" would be a word with no measurement behind
    it.
    """
    by_command = {}
    for run in runs:
        by_command.setdefault(run['name'], []).append(run)
    slow = []
    for name, command_runs in by_command.items():
        baseline = baselines.get(name)
        if not baseline:
            continue
        durations = [
            run['duration_ms'] for run in command_runs
            if run['status'] == 'succeeded' and run['duration_ms'] is not None
        ]
        median = _median(durations)
        if median and median >= baseline * HOUR_SLOWDOWN_FACTOR:
            slow.append(name)
    return sorted(slow)

def _hour_row(hour, runs, previous_runs, baselines):
    """One local hour: how busy it was, and whether it was slow for anybody."""
    succeeded = [run for run in runs if run['status'] == 'succeeded']
    durations = [
        run['duration_ms'] for run in succeeded if run['duration_ms'] is not None
    ]
    previous_durations = [
        run['duration_ms'] for run in previous_runs
        if run['status'] == 'succeeded' and run['duration_ms'] is not None
    ]
    slow_commands = _slow_commands(runs, baselines)
    return {
        'hour': hour,
        'label': f'{hour:02d}:00',
        'run_count': len(runs),
        'succeeded_count': len(succeeded),
        'failed_count': len(runs) - len(succeeded),
        'commands': sorted({run['name'] for run in runs}),
        'command_count': len({run['name'] for run in runs}),
        # The hour's own median mixes commands of very different sizes, so it is
        # read next to `slow_commands` rather than on its own. The figure the
        # crowding call rests on is per command, which is what keeps a uniformly
        # slow command from making its hour look congested.
        'median_duration_ms': _median(durations),
        'max_duration_ms': max(durations) if durations else None,
        'is_crowded': len(runs) >= MIN_HOUR_RUNS and bool(slow_commands),
        'slow_commands': slow_commands,
        'previous_run_count': len(previous_runs),
        'previous_median_duration_ms': _median(previous_durations),
    }


def _conclusion(rows, crowded):
    """One sentence naming what the hours actually show."""
    if not rows:
        return '窗口内还没有已记录的定时任务运行，按小时分布需要先有运行台账。'
    if not crowded:
        return (
            f'各小时都没同时满足「至少 {MIN_HOUR_RUNS} 次运行」与'
            f'「有命令比它自身平时慢 {HOUR_SLOWDOWN_FACTOR} 倍」两个条件，'
            '暂时看不出小时级的互相排队。'
        )
    parts = '、'.join(row['label'] for row in crowded[:3])
    sentence = f'有 {len(crowded)} 个小时同时满足运行数与变慢两个条件：{parts}。'
    named = sorted({name for row in crowded for name in row['slow_commands']})
    if named:
        sentence += '涉及的命令：' + '、'.join(named[:4]) + '。'
    sentence += '此处只报告，不改调度配置、不自动挪任务。'
    return sentence

def build_task_run_hour_load(days=30, *, now=None):
    """Group the ledger's runs by the local hour they started in.

    `days` selects the window, the same way the other schedule panels use it, so
    a reader who narrowed the dashboard to a week sees a week of runs. `now` is
    injectable so the tests can pin the window to a fixed moment.

    The previous period is the equal-length window immediately before this one,
    cut by the same local-midnight bounds every other cross-period panel uses,
    so the "上一周期同一小时" column cannot disagree with the trend panel about
    which period a run belongs to.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start, previous_start = _period_bounds(now, days)

    runs = list(
        TaskRun.objects.filter(started_at__gte=previous_start, started_at__lte=now)
        .exclude(note='dry-run')
        .values(*FIELDS)
        .order_by('started_at')
    )

    current_runs = []
    previous_runs = []
    for run in runs:
        # Attribution is by the start time, never the finish time: a run that
        # hung across the period boundary belongs to the period that had to
        # live with it.
        if run['started_at'] >= start:
            current_runs.append(run)
        else:
            previous_runs.append(run)

    baselines = _command_baseline(current_runs)
    current_hours = _by_hour(current_runs)
    previous_hours = _by_hour(previous_runs)

    rows = [
        _hour_row(hour, hour_runs, previous_hours.get(hour, []), baselines)
        for hour, hour_runs in current_hours.items()
    ]
    # Busiest first, because the hours worth reading are the ones something
    # happened in; the hour number only breaks ties, so two hours with the same
    # count keep a stable order.
    rows.sort(key=lambda row: (-row['run_count'], row['hour']))

    crowded = [row for row in rows if row['is_crowded']]
    return {
        'days': days,
        'min_hour_runs': MIN_HOUR_RUNS,
        'slowdown_factor': HOUR_SLOWDOWN_FACTOR,
        'max_hour_rows': MAX_HOUR_ROWS,
        'window_start': start,
        'window_end': now,
        'previous_window_start': previous_start,
        'hours': rows[:MAX_HOUR_ROWS],
        'crowded_hours': crowded[:MAX_HOUR_ROWS],
        'crowded_hour_count': len(crowded),
        'busiest_hour': rows[0]['hour'] if rows else None,
        'has_data': bool(rows),
        'summary': _conclusion(rows, crowded),
    }
