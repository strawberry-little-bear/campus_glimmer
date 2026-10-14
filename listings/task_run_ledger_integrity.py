# -*- coding: utf-8 -*-
"""Audit whether the task-run ledger can support the schedule panels.

调度面板已经把 ``TaskRun`` 当作事实来源，但它们都在读“这行说明什么”，
没有人先读“这行本身能不能读”。正常写入路径会过滤指标、记录失败备注、用
单调时钟算耗时；admin、回填脚本或未来的排障工具却可以绕过这些约定。这个
模块因此只做读数前的一层完整性审计：时间、状态、失败证据和指标形状。

它不修复任何行，也不把未知命令或 dry-run 判成错误。那些是扩展中的正常
形状；真正需要停下来的是未知状态、结束早于开始、明显不可能的耗时，以及
成功运行没有任何可比指标。
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import TaskRun
from .task_run_health import KNOWN_COMMANDS
from .task_runs import METRIC_FIELDS


ISSUE_ROW_LIMIT = 12
FUTURE_CLOCK_TOLERANCE = timedelta(minutes=1)
MIN_DURATION_MISMATCH_MS = 2000
DURATION_MISMATCH_SHARE = 0.10

ISSUE_LABELS = {
    'future_time': '时间在未来',
    'finish_before_start': '结束早于开始',
    'negative_duration': '负耗时',
    'duration_mismatch': '耗时与时间戳严重不一致',
    'invalid_status': '未知状态',
    'missing_failure_note': '失败行缺少备注',
    'invalid_metrics': '指标形状不可读',
}
ISSUE_ORDER = (
    'future_time',
    'finish_before_start',
    'negative_duration',
    'duration_mismatch',
    'invalid_status',
    'missing_failure_note',
    'invalid_metrics',
)
VALID_STATUSES = {'succeeded', 'failed'}


ALLOWED_PERIOD_DAYS = (7, 30, 90, 365)


def _period_bounds(days, now):
    days = max(1, int(days))
    if days not in ALLOWED_PERIOD_DAYS:
        days = 30
    start_date = timezone.localdate(now) - timedelta(days=days - 1)
    start = timezone.make_aware(
        timezone.datetime.combine(start_date, timezone.datetime.min.time()),
    )
    return start, days


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _metric_shape(metrics, *, requires_metric):
    """Return whether a row's metrics are readable, plus diagnostic counts.

    Unknown keys are reported separately and deliberately do not make a row
    unreadable: ``normalise_metrics`` drops unknown keys too, so a new command
    carrying one extra key is an extension reminder rather than corrupt data.
    """
    valid_key_count = 0
    invalid_value_count = 0
    unknown_key_count = 0

    if not isinstance(metrics, dict):
        return False, 1, 0

    for key, value in metrics.items():
        if key not in METRIC_FIELDS:
            unknown_key_count += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            invalid_value_count += 1
        else:
            valid_key_count += 1

    is_readable = invalid_value_count == 0 and (valid_key_count > 0 or not requires_metric)
    return is_readable, invalid_value_count, unknown_key_count


def _time_issues(run, now):
    started_at = run['started_at']
    finished_at = run['finished_at']
    duration_ms = run['duration_ms'] or 0
    issues = []

    future_limit = now + FUTURE_CLOCK_TOLERANCE
    if started_at > future_limit or finished_at > future_limit:
        issues.append('future_time')
    if finished_at < started_at:
        issues.append('finish_before_start')
    if duration_ms < 0:
        issues.append('negative_duration')

    elapsed_ms = (finished_at - started_at).total_seconds() * 1000
    tolerance = max(
        MIN_DURATION_MISMATCH_MS,
        abs(elapsed_ms) * DURATION_MISMATCH_SHARE,
    )
    if abs(duration_ms - elapsed_ms) > tolerance:
        issues.append('duration_mismatch')
    return issues


def _row_issues(run, now):
    issues = _time_issues(run, now)
    if run['status'] not in VALID_STATUSES:
        issues.append('invalid_status')
    if run['status'] == 'failed' and not (run['note'] or '').strip():
        issues.append('missing_failure_note')

    requires_metric = run['status'] == 'succeeded' and run['note'] != 'dry-run'
    metrics_readable, invalid_values, _unknown_keys = _metric_shape(
        run['metrics'], requires_metric=requires_metric,
    )
    if not metrics_readable or invalid_values:
        issues.append('invalid_metrics')
    return issues


def _issue_row(run, issues):
    return {
        'id': run['id'],
        'name': run['name'],
        'started_at': run['started_at'],
        'issue_count': len(issues),
        'issue_keys': issues,
        'issue_labels': [ISSUE_LABELS[key] for key in issues],
    }


def build_task_run_ledger_integrity(days=30, *, now=None):
    """Read one ledger window and report whether its rows are internally valid."""
    now = now or timezone.now()
    start, days = _period_bounds(days, now)

    # The expected window ends at ``now``. Rows that claim to start after that
    # are deliberately fetched as well: excluding them would make the future-
    # timestamp check unfalsifiable.
    runs = list(
        TaskRun.objects.filter(
            Q(started_at__gte=start, started_at__lte=now)
            | Q(started_at__gt=now),
        ).order_by('-started_at').values(
            'id', 'name', 'status', 'started_at', 'finished_at',
            'duration_ms', 'metrics', 'note',
        )
    )

    issue_rows = []
    issue_type_counts = {key: 0 for key in ISSUE_ORDER}
    issue_type_commands = {key: set() for key in ISSUE_ORDER}
    command_stats = {}
    succeeded_total = 0
    failed_total = 0
    dry_run_total = 0
    unknown_command_total = 0
    metric_required_total = 0
    metric_readable_total = 0
    unknown_metric_key_total = 0

    for run in runs:
        name = run['name']
        command = command_stats.setdefault(name, {
            'name': name,
            'run_count': 0,
            'issue_row_count': 0,
            'time_issue_row_count': 0,
            'invalid_status_row_count': 0,
            'missing_failure_note_row_count': 0,
            'metric_issue_row_count': 0,
            'invalid_metric_value_count': 0,
            'unknown_metric_key_count': 0,
            'is_known': name in KNOWN_COMMANDS,
        })

        command['run_count'] += 1
        if not command['is_known']:
            unknown_command_total += 1
        if run['status'] == 'succeeded':
            succeeded_total += 1
        if run['status'] == 'failed':
            failed_total += 1
        if run['note'] == 'dry-run':
            dry_run_total += 1

        requires_metric = run['status'] == 'succeeded' and run['note'] != 'dry-run'
        if requires_metric:
            metric_required_total += 1
        metrics_readable, invalid_values, unknown_keys = _metric_shape(
            run['metrics'], requires_metric=requires_metric,
        )
        if metrics_readable and requires_metric:
            metric_readable_total += 1
        command['invalid_metric_value_count'] += invalid_values
        command['unknown_metric_key_count'] += unknown_keys
        unknown_metric_key_total += unknown_keys
        if invalid_values or not metrics_readable:
            command['metric_issue_row_count'] += 1

        issues = _row_issues(run, now)
        if issues:
            issue_rows.append(_issue_row(run, issues))
            command['issue_row_count'] += 1
            for key in issues:
                issue_type_counts[key] += 1
                issue_type_commands[key].add(name)
            time_keys = {
                'future_time', 'finish_before_start', 'negative_duration',
                'duration_mismatch',
            }
            if time_keys.intersection(issues):
                command['time_issue_row_count'] += 1
            if 'invalid_status' in issues:
                command['invalid_status_row_count'] += 1
            if 'missing_failure_note' in issues:
                command['missing_failure_note_row_count'] += 1

    issue_type_rows = [
        {
            'key': key,
            'label': ISSUE_LABELS[key],
            'row_count': issue_type_counts[key],
            'command_count': len(issue_type_commands[key]),
        }
        for key in ISSUE_ORDER
    ]
    command_rows = sorted(
        command_stats.values(),
        key=lambda row: (
            -row['issue_row_count'], -row['run_count'], row['name'],
        ),
    )

    run_total = len(runs)
    issue_row_total = len(issue_rows)
    issue_rows.sort(key=lambda row: (-row['issue_count'], row['started_at'], row['id']))
    visible_issue_rows = issue_rows[:ISSUE_ROW_LIMIT]

    return {
        'days': days,
        'window_start': start,
        'window_end': now,
        'has_data': run_total > 0,
        'future_clock_tolerance_minutes': int(
            FUTURE_CLOCK_TOLERANCE.total_seconds() // 60,
        ),
        'duration_mismatch_base_ms': MIN_DURATION_MISMATCH_MS,
        'duration_mismatch_share_percent': int(DURATION_MISMATCH_SHARE * 100),
        'issue_row_limit': ISSUE_ROW_LIMIT,
        'summary': {
            'run_total': run_total,
            'succeeded_total': succeeded_total,
            'failed_total': failed_total,
            'dry_run_total': dry_run_total,
            'known_command_total': run_total - unknown_command_total,
            'unknown_command_total': unknown_command_total,
            'issue_row_total': issue_row_total,
            'clean_row_total': run_total - issue_row_total,
            'issue_row_share': _percent(issue_row_total, run_total),
            'metric_required_total': metric_required_total,
            'metric_readable_total': metric_readable_total,
            'metric_readable_share': _percent(
                metric_readable_total, metric_required_total,
            ),
            'unknown_metric_key_total': unknown_metric_key_total,
        },
        'issue_type_rows': issue_type_rows,
        'command_rows': command_rows,
        'issue_rows': visible_issue_rows,
        'issue_row_hidden_total': max(0, issue_row_total - len(visible_issue_rows)),
    }
