# -*- coding: utf-8 -*-
"""Turn the task run ledger into an answer to "is the schedule healthy".

Recording every run is only useful if something reads the records back. This
module aggregates the ledger per command and surfaces three signals that a
per-run row cannot show on its own: the success rate over a period, how long a
command has gone without a successful run, and how many times it has failed in
a row. A single failed row is noise; a command that has failed five times
running is a broken schedule.

Two boundaries are deliberate. Silence is measured from the last *successful*
run rather than the last run of any kind, because a failing command still runs
and would otherwise hide itself: five consecutive failures would report as
"ran ten minutes ago" and look perfectly healthy. Dry runs are excluded by
default for the same reason the ledger marks them - a run that sends nothing
cannot vouch for the schedule.

Only the columns this module needs are read, so the aggregation cost does not
grow with how many runs have accumulated.
"""

from datetime import timedelta

from django.utils import timezone

from .models import TaskRun


# Commands the scheduler is expected to run. Anything recorded under a name
# that is not in this list is still reported, but it cannot be judged against
# an expected cadence.
KNOWN_COMMANDS = (
    'expire_items',
    'process_order_timeouts',
    'send_lifecycle_reminders',
    'send_operations_digest',
    'send_opportunity_digest',
    'send_saved_search_digest',
    'sync_academic_calendar',
)

# A command is called out as stale once it has not succeeded for this long.
STALE_AFTER = timedelta(days=3)

PERIOD_FIELDS = ('name', 'status', 'started_at', 'finished_at', 'duration_ms', 'metrics', 'note')


def _format_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} 秒'
    if seconds < 3600:
        return f'{seconds // 60} 分钟'
    if seconds < 86400:
        return f'{seconds // 3600} 小时'
    return f'{seconds // 86400} 天'


def _silence_days(last_success_at, now):
    """Days since the last successful run, or None if there never was one."""
    if last_success_at is None:
        return None
    return max((now - last_success_at).total_seconds() / 86400.0, 0.0)


def _command_row(name, runs, now, days):
    """Aggregate the recorded runs of a single command."""
    succeeded = [run for run in runs if run['status'] == 'succeeded']
    failed = [run for run in runs if run['status'] == 'failed']
    total = len(runs)
    last_run_at = runs[0]['started_at'] if runs else None
    last_success_at = succeeded[0]['started_at'] if succeeded else None

    # Consecutive failures, counted back from the most recent run. Stops at the
    # first success, so it means "failing right now" rather than "ever failed".
    consecutive_failures = 0
    for run in runs:
        if run['status'] == 'failed':
            consecutive_failures += 1
        else:
            break

    silence_days = _silence_days(last_success_at, now)
    durations = [run['duration_ms'] for run in succeeded if run['duration_ms'] is not None]

    return {
        'name': name,
        'run_count': total,
        'failed_count': len(failed),
        'success_rate': (len(succeeded) / total) if total else None,
        'last_run_at': last_run_at,
        'last_success_at': last_success_at,
        'last_status': runs[0]['status'] if runs else None,
        'consecutive_failures': consecutive_failures,
        'silence_days': silence_days,
        'silence_label': _format_duration(silence_days * 86400 if silence_days is not None else None),
        'is_stale': silence_days is None or silence_days > STALE_AFTER.total_seconds() / 86400.0,
        'avg_duration_ms': int(sum(durations) / len(durations)) if durations else None,
        'metrics_total': _sum_metrics(succeeded),
    }


def _sum_metrics(rows):
    """Add up each metric field across the given rows."""
    totals = {}
    for row in rows:
        for key, value in (row.get('metrics') or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[key] = totals.get(key, 0) + value
    return totals


def build_task_run_health(days=7, now=None):
    """Aggregate the ledger into per-command health rows plus a summary.

    Dry runs are excluded unless they are the only evidence a command ever
    ran, in which case they are reported but never counted towards a success
    rate - a command that has only ever been dry-run has not proved anything.
    """
    now = now or timezone.now()
    since = now - timedelta(days=days)

    rows = list(
        TaskRun.objects.filter(started_at__gte=since)
        .exclude(note='dry-run')
        .order_by('-started_at')
        .values(*PERIOD_FIELDS)
    )

    by_name = {}
    for row in rows:
        by_name.setdefault(row['name'], []).append(row)

    # Known commands first so the panel has a stable order, then anything
    # recorded under a name the scheduler does not know about yet.
    names = list(KNOWN_COMMANDS) + sorted(set(by_name) - set(KNOWN_COMMANDS))
    commands = []
    for name in names:
        runs = by_name.get(name, [])
        if not runs:
            commands.append(_empty_command_row(name, days))
        else:
            commands.append(_command_row(name, runs, now, days))

    total_runs = len(rows)
    total_failed = sum(1 for row in rows if row['status'] == 'failed')
    stale = [row for row in commands if row['is_stale']]
    failing = [row for row in commands if row['consecutive_failures'] > 0]

    if total_runs == 0:
        summary = '统计周期内没有已记录的运行，先确认调度任务是否已经接入台账。'
    elif stale:
        summary = f'{len(stale)} 个命令超过 {int(STALE_AFTER.total_seconds() / 86400)} 天没有成功运行，需要确认调度是否仍在触发。'
    elif failing:
        summary = f'{len(failing)} 个命令正在连续失败，优先查看最近的失败原因。'
    else:
        summary = f'周期内 {total_runs} 次运行全部正常，失败 {total_failed} 次。'

    recommendations = []
    for row in sorted(failing, key=lambda item: -item['consecutive_failures']):
        recommendations.append(f"{row['name']} 已连续失败 {row['consecutive_failures']} 次，最近一次：{row['last_run_at']:%m-%d %H:%M}")
    for row in sorted(stale, key=lambda item: -(item['silence_days'] or 0)):
        if row['silence_days'] is None:
            recommendations.append(f"{row['name']} 在周期内没有任何运行记录。")
        else:
            recommendations.append(f"{row['name']} 已 {row['silence_label']}没有成功运行。")

    return {
        'days': days,
        'commands': commands,
        'total_runs': total_runs,
        'total_failed': total_failed,
        'success_rate': ((total_runs - total_failed) / total_runs) if total_runs else None,
        'stale_count': len(stale),
        'failing_count': len(failing),
        'never_run': [row for row in commands if row['run_count'] == 0],
        'summary': summary,
        'recommendations': recommendations,
        'stale_after_days': int(STALE_AFTER.total_seconds() / 86400),
    }


def _empty_command_row(name, days):
    """A placeholder row for a command that has no recorded run at all."""
    return {
        'name': name,
        'run_count': 0,
        'failed_count': 0,
        'success_rate': None,
        'last_run_at': None,
        'last_success_at': None,
        'last_status': None,
        'consecutive_failures': 0,
        'silence_days': None,
        'silence_label': '—',
        'is_stale': True,
        'avg_duration_ms': None,
        'metrics_total': {},
    }
