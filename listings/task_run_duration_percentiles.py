# -*- coding: utf-8 -*-
"""Say where a command spends its time, not just how long it usually takes.

`build_task_run_trend` reports a median, a maximum and a count of runs that
took several times the median. Those three figures answer "did the typical run
get slower", and they answer it well, but they share a blind spot: the tail. A
command whose fiftieth run takes four seconds and whose ninety-ninth takes
forty has a median of four seconds in every single window, so the median shows
nothing at all while the schedule window is quietly being missed. The maximum
is worse than useless here, because it is one observation - a maximum of forty
seconds says the worst case happened, not how often anything close to it
happens.

Percentiles are what a tail is measured with, and the reason to use three of
them rather than one is that they answer different questions. P50 is the
typical run and moves when the command itself got slower. P90 is the run a
student waits through once in a while and moves when the command started
hitting an external dependency that is usually fast. P99 is the run that eats
the scheduling window, and it is the figure that decides whether the command
should be split, rate limited or moved to a quieter hour. Reporting only the
median would make all three look the same.

Four boundaries keep this module from becoming a ranking.

Runs are attributed by when they started, exactly as the cross-period trend
module does, so a run that hangs across the boundary is not counted in the
window it finished in and reported as "the next period got slower".

Failed runs are excluded from the percentiles. A failure can exit in
milliseconds or hang until the scheduler kills it, so including them would
make one number describe two unrelated things - how often a command breaks and
how slow it is when it works. The count of failures is still reported next to
the percentiles, so the reader can see that the percentiles describe the
successful runs only.

A percentile is only claimed when there are enough runs to compute one. With
nine runs, a nearest-rank P99 *is* the maximum: the rank rounds up to the last
observation, and calling it a percentile would let a reader believe a tail was
measured when only one run was seen. The sample floor is reported per figure,
so a figure that cannot be computed says so instead of quietly showing the
maximum under a percentile label.

The percentile is nearest-rank rather than interpolated. Interpolation
produces a value that no run ever took - with eleven runs the interpolated P90
lands between the ninth and tenth observations - and this module exists to
describe runs that actually happened. Nearest-rank is also stable when runs
are added, which matters because the ledger keeps growing.

Only these columns are read, so the cost does not grow with run count.
"""

from .models import TaskRun
from .task_run_trend import _period_bounds

# The percentiles reported, in the order the panel reads them. Fifty leads
# because it is the figure a reader already knows how to read; ninety and
# ninety-nine follow because they are the ones this module exists to add.
PERCENTILES = (
    (50, 'P50'),
    (90, 'P90'),
    (99, 'P99'),
)

# How many runs a percentile needs before it is claimed. The nearest-rank
# method puts P90 at the ninth of ten runs and P99 at the last of a hundred,
# so a percentile is honest when there are roughly a hundred runs for every
# rank below it. Ten runs support P50 (the fifth of five) and P90 (the ninth
# of ten) but not P99, which would be the maximum wearing a percentile label.
# The floor is therefore per figure rather than one number for the whole row.
MIN_RUNS_FOR_PERCENTILE = {
    50: 5,
    90: 10,
    99: 100,
}

# Only these columns are read, so the cost does not grow with run count.
FIELDS = ('name', 'status', 'started_at', 'duration_ms', 'note')


def _percentile(values, rank):
    """The nearest-rank percentile, or None when there is not enough to claim it.

    Nearest-rank takes the observation at the smallest index whose share of the
    sample is at least the requested rank, so the result is always a value some
    run actually took. With ten values the rank-90 observation is the ninth,
    because nine of ten is ninety percent and eight of ten is not.
    """
    if not values:
        return None
    if len(values) < MIN_RUNS_FOR_PERCENTILE[rank]:
        return None
    ordered = sorted(values)
    index = max(0, -(-len(ordered) * rank // 100) - 1)
    return ordered[index]


def _tail_share(percentiles):
    """How far above the typical run the worst *claimed* percentile sits.

    The share is measured from the highest percentile that could actually be
    claimed, not from a fixed P99. A command with nine runs has no honest P99 -
    the nearest-rank P99 of nine runs is the maximum - so comparing against a
    P99 that is not there would report no tail at all for a command whose
    ninetieth run is three times its median, and the tail is exactly what this
    module exists to show. P90 is the best tail available at that sample size,
    and it is the one that gets compared.

    The share is relative to P50 rather than a difference in milliseconds,
    because the seven commands differ by orders of magnitude and "P99 is 900 ms
    above P50" means nothing without knowing what P50 was.
    """
    claimed = [
        value for value in percentiles.values() if value is not None
    ]
    if len(claimed) < 2 or not percentiles.get('P50'):
        return None
    p50 = percentiles['P50']
    worst = max(claimed)
    return round((worst - p50) / p50, 2)


def _command_row(name, runs):
    """The percentile figures for one command inside one window."""
    succeeded = [run for run in runs if run['status'] == 'succeeded']
    durations = [
        run['duration_ms'] for run in succeeded if run['duration_ms'] is not None
    ]
    percentiles = {
        label: _percentile(durations, rank) for rank, label in PERCENTILES
    }
    return {
        'name': name,
        'run_count': len(runs),
        'succeeded_count': len(succeeded),
        'failed_count': len(runs) - len(succeeded),
        'percentiles': percentiles,
        'sample_enough': {
            label: len(durations) >= MIN_RUNS_FOR_PERCENTILE[rank]
            for rank, label in PERCENTILES
        },
        'tail_share': _tail_share(percentiles),
    }


def build_task_run_duration_percentiles(days=30, *, now=None):
    """Group the ledger's durations into per-command percentiles.

    `days` selects the window and is deliberately the dashboard's period
    selector, so a reader who narrowed the view to a week sees a week of runs
    and not a month of them. `now` is injectable so the tests can pin the
    window to a fixed moment.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start, _previous_start = _period_bounds(now, days)
    runs = list(
        TaskRun.objects.filter(started_at__gte=start, started_at__lte=now)
        .exclude(note='dry-run')
        .values(*FIELDS)
        .order_by('name', '-started_at')
    )
    by_command = {}
    for run in runs:
        by_command.setdefault(run['name'], []).append(run)
    rows = [_command_row(name, command_runs) for name, command_runs in by_command.items()]
    # A command with no claimed tail sorts last rather than first: the rows
    # that carry a tail are the ones worth reading first.
    rows.sort(key=lambda row: (row['tail_share'] is None, -(row['tail_share'] or 0), row['name']))
    return {
        'days': days,
        'min_runs_for_p99': MIN_RUNS_FOR_PERCENTILE[99],
        'window_start': start,
        'window_end': now,
        'rows': rows,
        'has_data': any(row['succeeded_count'] for row in rows),
    }
