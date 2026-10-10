# -*- coding: utf-8 -*-
"""Say when a command starts running into its own next execution.

Every other figure the dashboard reports about the schedule describes what
already happened: which command failed, which command got slow, which hour was
crowded. All of them are descriptions of the past. This module is the one that
looks forward, and it does so with a fact nobody configured: how long a run
takes compared with how often the command is asked to run.

When a run outlives its own interval, the next one starts before the last one
finished. Nothing in the ledger marks that as wrong. Each row still records
success, still records a duration, and the health panel still reports a healthy
command. What actually happens is that two executions of the same command now
compete for the same connection pool and the same external quota, which makes
each of them slower, which makes the next one start even later, which makes the
one after that slower still. That loop is the mechanism behind most of the
schedule failures this project has had, and no existing panel can see it,
because the panel that would show it is the one that compares a duration with
an interval.

Four boundaries keep this honest.

The interval comes from the ledger, not from a schedule. This project has no
crontab data source, and even where one exists it records what was written
rather than what ran: a run that was delayed by a busy machine, or a catch-up
run after a restart, leaves the interval between two starts longer than the
configured one. The median interval between consecutive starts is the rhythm
the schedule actually kept, which is the only one that can be compared with a
duration that actually happened. It is deliberately a median rather than a
mean, because one missed run doubles a single interval and a mean would report
a schedule that no run ever followed.

Overlap is counted from the ledger rather than inferred from the ratio. A P99
that approaches the interval is a projection, and a projection is not evidence
that two runs ever collided. Two rows are counted as overlapping when the
earlier one's start plus its duration is later than the next one's start, so
every overlap this module reports is two rows that were both recorded, with
timestamps, as having been in flight at the same time. The ratio is reported
next to the count, never instead of it.

Overlap has to be shown to matter before it is called a problem. Two commands
may overlap without any consequence - a command that spends its interval
waiting on something external, and whose duration grows because that thing got
slower, overlaps more and more while nothing degrades. The rows therefore carry
the median duration of runs that overlapped against the median of runs that did
not, and a row is only flagged when the overlapped runs are actually slower.
An overlap with no cost is reported as a figure and left alone.

Nothing here moves anything. The module does not change a schedule, does not
suggest a new minute, does not reorder the seven commands and does not write
back to the ledger. It also never writes a run into the ledger to record an
overlap: an overlap is a relationship between two existing rows, and materialising
it would put a third row into the table whose start time is neither of theirs.

Only the columns this module needs are read, so the cost does not grow with how
many runs have accumulated.
"""

from datetime import timedelta

from django.utils import timezone

from .models import TaskRun
from .task_run_trend import _median, _period_bounds

# How many runs a command needs before its rhythm is reported at all. The
# interval is a median of gaps between consecutive starts, so two runs give one
# gap and no median worth the name. Five runs give four gaps, which is the
# smallest number where the middle gap means anything.
MIN_RUNS_FOR_RHYTHM = 5

# How many overlapping pairs a command needs before the overlap is described as
# a pattern. This floor is one, and deliberately so. An overlap is not a sample
# of a tendency; it is a pair of rows that were both recorded as being in flight
# at the same time, and a run that outlived its own interval is already an event
# rather than a statistic. A floor above one would hide exactly the moment this
# module exists to report - the first run that no longer finished before the
# next one started - so the earliest collision has to stay visible. What keeps
# the panel quiet is not this floor but the slowdown floor below, which asks
# whether the overlap cost anything at all.
MIN_OVERLAP_PAIRS = 1

# How much slower the overlapped runs have to be than the rest before the
# overlap is called costly. The multiple is relative for the same reason every
# other duration threshold in this project is: the seven commands differ by
# orders of magnitude, and a fixed number of milliseconds would mean one thing
# for a digest and something else entirely for an expiry sweep.
OVERLAP_SLOWDOWN = 1.25

# How close to its interval a run's tail has to get before the row says it is
# approaching the interval. The figure is the ratio of the longest run actually
# observed to the interval, so 1.0 means the worst run took as long as the whole
# gap between two starts. Four fifths is where a run that is merely slow starts
# to become a run that leaves no room for the next one.
#
# The longest observed run, not the highest claimed percentile. What decides
# whether a run collides with the next one is the run that took the longest, not
# the run at some rank: a single run that outlived its interval is one real
# overlap, and a percentile smooths exactly that run away. This is also the
# reason a percentile tail would understate the risk here - a command whose
# worst run took nine minutes against a ten minute interval would be reported
# as healthy by a P90 of one hundred milliseconds, which is the shape most
# commands in this project actually have. The maximum is a figure the trend
# panel already reports, so this is not a new statistic invented for this panel.
APPROACHING_INTERVAL = 0.8

# Only these columns are read, so the cost does not grow with run count.
FIELDS = ('name', 'status', 'started_at', 'duration_ms', 'note')


def _intervals(runs):
    """The gaps between consecutive starts, in milliseconds.

    Runs arrive ordered by start time, so the gaps are taken between neighbours
    rather than against a reference point: a gap from the window start would
    measure how much of the window was spent before the first run, which is a
    statement about the window and not about the schedule.
    """
    gaps = []
    for earlier, later in zip(runs, runs[1:]):
        gap = (later['started_at'] - earlier['started_at']).total_seconds() * 1000
        if gap > 0:
            gaps.append(int(gap))
    return gaps


def _overlapping_pairs(runs):
    """Pairs of runs that were in flight at the same time.

    The earlier run's finish is its start plus its own duration. A pair counts
    when the later run started before that moment, so both rows were recorded
    as open simultaneously. Runs with no duration recorded are skipped: a zero
    duration would make the finish equal the start, which is a run that
    finished instantaneously and cannot have overlapped anything.
    """
    pairs = []
    for earlier, later in zip(runs, runs[1:]):
        if earlier['duration_ms'] is None or not earlier['duration_ms']:
            continue
        finish = earlier['started_at'] + timedelta(milliseconds=earlier['duration_ms'])
        if finish > later['started_at']:
            pairs.append((earlier, later))
    return pairs


def _command_row(name, runs):
    """One command: the rhythm it kept, and whether it ran into itself.

    The runs of one command arrive ordered by start time and cover the current
    window only. Everything here is read off those rows, so a command whose runs
    were all delayed by a busy machine is described as having kept a slow
    rhythm, which is what it did.
    """
    gaps = _intervals(runs)
    # A median of fewer gaps than this is not a rhythm, so the figure is withheld
    # rather than reported: a command with two runs has one gap, and one gap is
    # a statement about those two runs rather than about the schedule.
    has_rhythm = len(gaps) >= MIN_RUNS_FOR_RHYTHM - 1
    interval = _median(gaps) if has_rhythm else None
    succeeded = [run for run in runs if run['status'] == 'succeeded']
    durations = [
        run['duration_ms'] for run in succeeded if run['duration_ms'] is not None
    ]
    pairs = _overlapping_pairs(runs)

    overlapped = []
    clean = []
    overlapped_starts = {run['started_at'] for pair in pairs for run in pair}
    for run in succeeded:
        if run['duration_ms'] is None:
            continue
        (overlapped if run['started_at'] in overlapped_starts else clean).append(run['duration_ms'])

    overlapped_median = _median(overlapped)
    clean_median = _median(clean)

    # The tail is the longest run that was actually observed, because the run
    # that decides whether the next start collides is the worst one and not the
    # one at a rank. See the constant's comment for why the percentile is the
    # wrong figure for this comparison.
    tail = max(durations) if durations else None

    tail_ratio = None
    if tail is not None and interval:
        tail_ratio = round(tail / interval, 2)

    overlap_cost = None
    if overlapped_median and clean_median:
        overlap_cost = round(overlapped_median / clean_median, 2)

    return {
        'name': name,
        'run_count': len(runs),
        'succeeded_count': len(succeeded),
        'interval_ms': interval,
        'interval_display': '—' if interval is None else interval,
        'interval_runs': len(gaps),
        'longest_run_ms': tail,
        'longest_run_ratio': tail_ratio,
        'longest_run_ratio_display': '—' if tail_ratio is None else f'{tail_ratio:g}',
        'overlap_pair_count': len(pairs),
        'overlapped_median_ms': overlapped_median,
        'clean_median_ms': clean_median,
        'overlap_cost': overlap_cost,
        'overlap_cost_display': '—' if overlap_cost is None else f'{overlap_cost:g}',
        'last_started_at': runs[-1]['started_at'] if runs else None,
        'has_rhythm': has_rhythm,
        'is_self_overlapping': (
            len(pairs) >= MIN_OVERLAP_PAIRS and overlap_cost is not None
            and overlap_cost >= OVERLAP_SLOWDOWN
        ),
        'is_approaching': (
            tail_ratio is not None and tail_ratio >= APPROACHING_INTERVAL
        ),
    }


def _conclusion(rows, overlapping):
    """One sentence naming the command that is actually running into itself."""
    if not rows:
        return '窗口内还没有已记录的定时任务运行，自我重叠需要先有运行台账。'
    if not overlapping:
        approaching = [row for row in rows if row['is_approaching']]
        if approaching:
            names = '、'.join(row['name'] for row in approaching[:3])
            return (
                f'还没有出现「上一次没跑完下一次就开始」的命令，'
                f'但 {names} 的尾部耗时已经接近自身的运行间隔，值得先看一眼。'
                '此处只报告，不改调度配置。'
            )
        return (
            '各命令最长的一次运行都明显短于自身的运行间隔，'
            '暂时看不出自我重叠。此处只报告，不改调度配置。'
        )
    top = overlapping[0]
    sentence = (
        f"「{top['name']}」已经有 {top['overlap_pair_count']} 对运行在时间上叠在一起，"
        f"且这些运行的耗时中位数是其余运行的 {top['overlap_cost_display']} 倍。"
    )
    sentence += '此处只报告，不改调度配置、不自动挪任务。'
    return sentence


def build_task_run_overlap(days=30, *, now=None):
    """Compare each command's durations with the rhythm it actually kept.

    `days` selects the window and is the dashboard's period selector, so a
    reader who narrowed the view to a week sees a week of runs. `now` is
    injectable so the tests can pin the window to a fixed moment.

    Only the current window is used. There is no previous period here, and the
    reason is that an interval measured across the window start would mix two
    different schedules into one figure: the gap between the last run before the
    window and the first run inside it is a statement about when the window was
    cut, not about the command. The cross-period panels already answer whether
    the schedule is drifting; this one answers whether it collides.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start, _previous_start = _period_bounds(now, days)

    runs = list(
        TaskRun.objects.filter(started_at__gte=start, started_at__lte=now)
        .exclude(note='dry-run')
        .values(*FIELDS)
        .order_by('name', 'started_at')
    )

    by_command = {}
    for run in runs:
        by_command.setdefault(run['name'], []).append(run)

    rows = [_command_row(name, command_runs) for name, command_runs in by_command.items()]
    # The rows that collide sort first, because they are the ones worth reading;
    # within them the most overlaps lead, and the name only breaks ties so two
    # equal rows keep a stable order between refreshes.
    rows.sort(key=lambda row: (
        not row['is_self_overlapping'],
        -row['overlap_pair_count'],
        row['name'],
    ))

    overlapping = [row for row in rows if row['is_self_overlapping']]
    approaching = [row for row in rows if row['is_approaching']]
    return {
        'days': days,
        'min_runs_for_rhythm': MIN_RUNS_FOR_RHYTHM,
        'min_overlap_pairs': MIN_OVERLAP_PAIRS,
        'overlap_slowdown': OVERLAP_SLOWDOWN,
        'approaching_interval': APPROACHING_INTERVAL,
        'window_start': start,
        'window_end': now,
        'rows': rows,
        'overlapping_rows': overlapping,
        'overlapping_count': len(overlapping),
        'approaching_count': len(approaching),
        'has_data': any(row['succeeded_count'] for row in rows),
        'summary': _conclusion(rows, overlapping),
    }