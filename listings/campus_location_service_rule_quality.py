# -*- coding: utf-8 -*-
"""校园开放规则配置的只读审计。

每周时段和单日例外本身已经有统一的解释器；这个模块只把配置形状摆出来：
哪些地点没有登记、哪些例外挂在停用地点、哪些替换例外没有被写成可执行时段、
哪些跨午夜窗口会在次日例外处被截断。它们都不是数据库错误，也不触发修复。
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from django.utils import timezone

from .models import (
    CampusLocation,
    CampusLocationServiceException,
    CampusLocationServiceWindow,
)

RULE_ROW_LIMIT = 20

SIGNAL_ORDER = {
    'inactive_location_exception': 1,
    'replacement_without_window': 2,
    'replacement_overlap': 3,
    'midnight_window_truncated': 4,
    'unregistered_location': 5,
}
SIGNAL_LABELS = {
    'inactive_location_exception': '例外挂在停用地点',
    'replacement_without_window': '替换例外没有时段',
    'replacement_overlap': '替换时段重叠',
    'midnight_window_truncated': '跨午夜被次日例外截断',
    'unregistered_location': '启用地点未登记时段',
}


def _weekly_window_interval(window):
    """Materialise a weekly window so cross-midnight spans sort correctly."""
    base_day = date(2026, 1, 5) + timedelta(days=window.weekday - 1)
    start = datetime.combine(base_day, window.start_time)
    end_day = base_day if window.end_time > window.start_time else base_day + timedelta(days=1)
    end = datetime.combine(end_day, window.end_time)
    return start, end


def _has_overlap(intervals):
    ordered = sorted(intervals)
    return any(
        next_start < current_end
        for (_, current_end), (next_start, _) in zip(ordered, ordered[1:])
    )


def _weekly_overlap_or_adjacent_groups(windows):
    grouped = defaultdict(list)
    for window in windows:
        grouped[(window.location_id, window.weekday)].append(window)

    group_count = 0
    for same_day_windows in grouped.values():
        intervals = sorted(
            _weekly_window_interval(window) for window in same_day_windows
        )
        connected = any(
            next_start <= current_end
            for (_, current_end), (next_start, _) in zip(intervals, intervals[1:])
        )
        if connected:
            group_count += 1
    return group_count


def _next_weekday(weekday):
    return weekday % 7 + 1


def _exception_config(exception):
    if exception.exception_type == CampusLocationServiceException.REPLACEMENT:
        intervals = '、'.join(
            window.display_text for window in exception.exception_windows.all()
        )
        interval_text = intervals if intervals else '未配置当天时段'
        return f'{exception.effective_date:%Y-%m-%d} 替换：{exception.reason}（{interval_text}）'
    return f'{exception.effective_date:%Y-%m-%d} 临时关闭：{exception.reason}'


def _add_rule_row(rows, *, location, signal, config, reason, effective_date=None):
    rows.append({
        'location_id': location.id,
        'location_name': location.name,
        'location_active_label': '启用' if location.is_active else '停用',
        'effective_date': effective_date,
        'signal': signal,
        'signal_label': SIGNAL_LABELS[signal],
        'config': config,
        'reason': reason,
    })


def _build_rows(locations, windows, exceptions):
    active_windows = [window for window in windows if window.is_active]

    exceptions_by_location = defaultdict(list)
    next_day_exceptions = defaultdict(list)
    for exception in exceptions:
        exceptions_by_location[exception.location_id].append(exception)
        next_day_exceptions[
            (exception.location_id, exception.effective_date.isoweekday())
        ].append(exception)

    rows = []
    for location in locations:
        location_windows = [
            window for window in active_windows
            if window.location_id == location.id
        ]
        if location.is_active and not location_windows:
            _add_rule_row(
                rows,
                location=location,
                signal='unregistered_location',
                config='未登记任何启用每周时段',
                reason='这不一定是错误；若地点确实全天协商开放，可以维持现状，只需确认档案口径。',
            )

        if not location.is_active:
            for exception in exceptions_by_location[location.id]:
                _add_rule_row(
                    rows,
                    location=location,
                    signal='inactive_location_exception',
                    config=_exception_config(exception),
                    reason='地点已停用但例外仍启用；请确认它是在解释历史，还是应随档案一起关闭。',
                    effective_date=exception.effective_date,
                )

        if not location.is_active:
            continue

        for exception in exceptions_by_location[location.id]:
            if exception.exception_type != CampusLocationServiceException.REPLACEMENT:
                continue
            exception_windows = list(exception.exception_windows.all())
            if not exception_windows:
                _add_rule_row(
                    rows,
                    location=location,
                    signal='replacement_without_window',
                    config=_exception_config(exception),
                    reason='替换例外没有任何当天时段时会被解释为全天关闭；请确认这是想表达的含义。',
                    effective_date=exception.effective_date,
                )
            elif _has_overlap([
                (window.start_time, window.end_time)
                for window in exception_windows
            ]):
                _add_rule_row(
                    rows,
                    location=location,
                    signal='replacement_overlap',
                    config=_exception_config(exception),
                    reason='重叠时段会被统一评估器合并；功能仍可用，只是后台维护时更容易看错。',
                    effective_date=exception.effective_date,
                )

        for window in location_windows:
            if window.end_time > window.start_time:
                continue
            clipped_exceptions = next_day_exceptions[
                (location.id, _next_weekday(window.weekday))
            ]
            for exception in clipped_exceptions:
                _add_rule_row(
                    rows,
                    location=location,
                    signal='midnight_window_truncated',
                    config=(
                        f'{window.display_text} → 次日 '
                        f'{exception.effective_date:%Y-%m-%d} {_exception_config(exception)}'
                    ),
                    reason='统一评估器会把跨午夜时段截断到午夜，不会延伸进带例外的次日。',
                    effective_date=exception.effective_date,
                )

    rows.sort(key=lambda row: (
        row['location_name'],
        row['effective_date'] or date.min,
        SIGNAL_ORDER[row['signal']],
        row['config'],
        row['location_id'],
    ))
    return rows


def _build_location_rows(locations, windows, exceptions, rule_rows):
    signal_counts = defaultdict(int)
    for row in rule_rows:
        signal_counts[row['location_id']] += 1

    active_windows_by_location = defaultdict(int)
    inactive_windows_by_location = defaultdict(int)
    for window in windows:
        if window.is_active:
            active_windows_by_location[window.location_id] += 1
        else:
            inactive_windows_by_location[window.location_id] += 1

    active_exceptions_by_location = defaultdict(int)
    for exception in exceptions:
        active_exceptions_by_location[exception.location_id] += 1

    relevant_ids = set(active_windows_by_location)
    relevant_ids.update(inactive_windows_by_location)
    relevant_ids.update(active_exceptions_by_location)
    relevant_ids.update(location.id for location in locations if location.is_active)

    location_by_id = {location.id: location for location in locations}
    rows = []
    for location_id in relevant_ids:
        location = location_by_id[location_id]
        active_window_count = active_windows_by_location[location_id]
        rows.append({
            'location_id': location_id,
            'location_name': location.name,
            'location_active_label': '启用' if location.is_active else '停用',
            'active_window_count': active_window_count,
            'inactive_window_count': inactive_windows_by_location[location_id],
            'active_exception_count': active_exceptions_by_location[location_id],
            'is_registered': location.is_active and active_window_count > 0,
            'signal_count': signal_counts[location_id],
        })
    rows.sort(key=lambda row: (
        -row['signal_count'],
        -row['active_window_count'],
        -row['active_exception_count'],
        row['location_name'],
        row['location_id'],
    ))
    return rows


def build_campus_location_service_rule_quality(days=30, *, now=None):
    """Build the read-only service-rule configuration audit."""

    del days
    moment = timezone.now() if now is None else now
    today = timezone.localtime(moment).date()

    locations = list(CampusLocation.objects.order_by('name', 'id'))
    windows = list(
        CampusLocationServiceWindow.objects.select_related('location').order_by(
            'location__name', 'weekday', 'start_time', 'end_time',
        )
    )
    exceptions = list(
        CampusLocationServiceException.objects.select_related('location')
        .prefetch_related('exception_windows')
        .order_by('effective_date', 'location__name', 'id')
    )

    active_locations = [location for location in locations if location.is_active]
    active_windows = [window for window in windows if window.is_active]
    inactive_windows = [window for window in windows if not window.is_active]
    cross_midnight_windows = [
        window for window in active_windows
        if window.end_time <= window.start_time
    ]

    registered_location_ids = {
        window.location_id for window in active_windows
        if window.location.is_active
    }
    unregistered_locations = [
        location for location in active_locations
        if location.id not in registered_location_ids
    ]

    active_exceptions = [exception for exception in exceptions if exception.is_active]
    inactive_exceptions = [exception for exception in exceptions if not exception.is_active]
    past_exceptions = [
        exception for exception in active_exceptions
        if exception.effective_date < today
    ]
    today_exceptions = [
        exception for exception in active_exceptions
        if exception.effective_date == today
    ]
    future_exceptions = [
        exception for exception in active_exceptions
        if exception.effective_date > today
    ]
    closed_exceptions = [
        exception for exception in active_exceptions
        if exception.exception_type == CampusLocationServiceException.CLOSED
    ]
    replacement_exceptions = [
        exception for exception in active_exceptions
        if exception.exception_type == CampusLocationServiceException.REPLACEMENT
    ]

    rule_rows = _build_rows(locations, windows, active_exceptions)
    location_rows = _build_location_rows(
        locations, windows, active_exceptions, rule_rows,
    )
    signal_counts = {signal: 0 for signal in SIGNAL_LABELS}
    for row in rule_rows:
        signal_counts[row['signal']] += 1
    signal_total = len(rule_rows)

    if not locations and not windows and not exceptions:
        summary = '当前还没有校园地点或开放规则可审计。'
    elif signal_total:
        summary = (
            f'{len(active_locations)} 个启用地点中，{len(unregistered_locations)} 个未登记启用每周时段；'
            f'当前开放配置共有 {signal_total} 条信号需要人工确认含义。'
        )
    else:
        summary = (
            f'{len(active_locations)} 个启用地点均已登记每周时段，当前没有需要确认的开放规则信号。'
        )

    return {
        'summary': summary,
        'has_data': bool(locations or windows or exceptions),
        'period_note': '本面板审计当前规则配置，页面顶部的统计周期参数不会改变它的分母。',
        'rule_row_limit': RULE_ROW_LIMIT,
        'rule_rows': rule_rows,
        'rule_page_rows': rule_rows[:RULE_ROW_LIMIT],
        'rule_total_count': signal_total,
        'metrics': {
            'location_total': len(locations),
            'active_location_count': len(active_locations),
            'inactive_location_count': len(locations) - len(active_locations),
            'registered_location_count': len(registered_location_ids),
            'unregistered_location_count': len(unregistered_locations),
            'active_window_count': len(active_windows),
            'inactive_window_count': len(inactive_windows),
            'cross_midnight_window_count': len(cross_midnight_windows),
            'weekly_overlap_or_adjacent_group_count': _weekly_overlap_or_adjacent_groups(
                active_windows,
            ),
            'active_exception_count': len(active_exceptions),
            'inactive_exception_count': len(inactive_exceptions),
            'past_exception_count': len(past_exceptions),
            'today_exception_count': len(today_exceptions),
            'future_exception_count': len(future_exceptions),
            'closed_exception_count': len(closed_exceptions),
            'replacement_exception_count': len(replacement_exceptions),
            'signal_total': signal_total,
            **signal_counts,
        },
        'location_rows': location_rows,
        'boundary_note': (
            '只读配置审计：未登记、重叠和截断都可以是刻意选择；'
            '本面板不合并规则、不改地点开关、不补例外时段，也不触发通知。'
        ),
    }
