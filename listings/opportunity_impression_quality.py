# -*- coding: utf-8 -*-
"""Audit the rows that became the opportunity-feed denominator.

``OpportunityImpression`` records that an opportunity was rendered. Its write is
intentionally non-fatal: a failed ``bulk_create`` leaves the page usable and only
writes a log line. Such failures never become business rows, so this module
cannot count them and does not try to infer a missing number per screen.

What it can read is the shape of landed rows: explainable kinds, kind/target
agreement, legal repeats, same-timestamp repeats that deserve a manual check,
future timestamps outside clock skew, and natural-day coverage.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import OpportunityImpression


FUTURE_TOLERANCE = timedelta(minutes=1)
DAILY_ROW_LIMIT = 30
KIND_ROWS = (
    ('demand', '校园求购'),
    ('lost_found', '失物招领'),
)


def _period_start(now, days):
    """Use the same local-midnight boundary as the operations dashboard."""
    today = timezone.localdate(now)
    return timezone.make_aware(
        datetime.combine(today - timedelta(days=days - 1), time.min)
    )


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _target_key(row):
    """Identify a target without reading its title, content, or URL."""
    if row['demand_id'] is not None:
        return ('demand', row['demand_id'])
    if row['lost_found_post_id'] is not None:
        return ('lost_found', row['lost_found_post_id'])
    return ('missing', None)


def _kind_shape(rows):
    """Keep current kinds fixed, then aggregate historical/invalid keys."""
    counts = {key: 0 for key, _label in KIND_ROWS}
    unknown_count = 0
    unknown_key_count = 0
    missing_count = 0
    unknown_keys = set()

    for row in rows:
        key = (row['kind'] or '').strip()
        if not key:
            missing_count += 1
        elif key in counts:
            counts[key] += 1
        else:
            unknown_count += 1
            unknown_keys.add(key)

    unknown_key_count = len(unknown_keys)
    total = len(rows)
    result = []
    for key, label in KIND_ROWS:
        count = counts[key]
        result.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _rate(count, total),
            'status': '当前类型',
            'is_anomaly': False,
            'note': '当前机会流写入路径使用的类型。',
        })
    for key, label, count, status, is_anomaly, note in (
        (
            'unknown', '未收录类型', unknown_count, '需要核对', True,
            f'共 {unknown_key_count} 种 key 不在当前类型清单内；不输出原始类型值。',
        ),
        (
            'missing', '空类型', missing_count, '需要核对', True,
            '无法把这一行归入求购或失物招领；只计数，不展示原始输入。',
        ),
    ):
        result.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _rate(count, total),
            'status': status,
            'is_anomaly': is_anomaly,
            'note': note,
        })
    return result, unknown_count, unknown_key_count, missing_count


def _target_consistency(row):
    """Classify one landed row; the database normally prevents malformed rows."""
    kind = (row['kind'] or '').strip()
    demand_id = row['demand_id']
    lost_id = row['lost_found_post_id']
    if kind == 'demand' and demand_id is not None and lost_id is None:
        return 'demand_target'
    if kind == 'lost_found' and lost_id is not None and demand_id is None:
        return 'lost_found_target'
    if demand_id is None and lost_id is None:
        return 'no_target'
    return 'target_mismatch'


def _target_rows(rows):
    counts = {
        'demand_target': 0,
        'lost_found_target': 0,
        'target_mismatch': 0,
        'no_target': 0,
    }
    for row in rows:
        counts[_target_consistency(row)] += 1

    total = len(rows)
    definitions = (
        (
            'demand_target', '求购类型指向求购', 'demand 且只有求购外键',
            '合法形状', False, '与当前写入路径和数据库约束一致。',
        ),
        (
            'lost_found_target', '失物类型指向失物', 'lost_found 且只有失物外键',
            '合法形状', False, '与当前写入路径和数据库约束一致。',
        ),
        (
            'target_mismatch', '类型与目标不匹配', '有目标但方向不一致',
            '需要核对', True, '只报聚合行数；不输出标题、内容或目标链接。',
        ),
        (
            'no_target', '没有任何目标', '两个目标外键都为空',
            '需要核对', True, '无法对应到一条机会；不删除、不回填。',
        ),
    )
    return [
        {
            'key': key,
            'label': label,
            'definition': definition,
            'count': counts[key],
            'share': _rate(counts[key], total),
            'status': status,
            'is_anomaly': is_anomaly,
            'note': note,
        }
        for key, label, definition, status, is_anomaly, note in definitions
    ]


def _repeat_shape(rows):
    """Describe repeats without collapsing legal impressions."""
    grouped = defaultdict(int)
    for row in rows:
        grouped[(row['user_id'], _target_key(row))] += 1

    repeat_pairs = sum(1 for count in grouped.values() if count > 1)
    repeat_rows = sum(count - 1 for count in grouped.values() if count > 1)
    same_time = defaultdict(int)
    for row in rows:
        same_time[(row['user_id'], _target_key(row), row['shown_at'])] += 1
    same_timestamp_pairs = sum(
        1 for count in same_time.values() if count > 1
    )
    same_timestamp_rows = sum(
        count - 1 for count in same_time.values() if count > 1
    )
    return {
        'first_seen_pairs': len(grouped),
        'repeat_pairs': repeat_pairs,
        'repeat_rows': repeat_rows,
        'repeat_share': _rate(repeat_rows, len(rows)),
        'same_timestamp_pairs': same_timestamp_pairs,
        'same_timestamp_rows': same_timestamp_rows,
    }


def _longest_gap_hours(rows):
    """The longest observable gap between rows, not a write-failure test."""
    if len(rows) < 2:
        return None
    longest = max(
        (right['shown_at'] - left['shown_at']).total_seconds()
        for left, right in zip(rows, rows[1:])
    )
    return round(longest / 3600, 1)


def _daily_rows(start, horizon, rows):
    """Build local-day rows and retain the most recent fixed number of days."""
    by_date = defaultdict(lambda: {
        'impressions': 0,
        'users': set(),
        'targets': set(),
        'repeat_rows': 0,
    })
    day_groups = defaultdict(lambda: defaultdict(int))
    for row in rows:
        day = timezone.localdate(row['shown_at'])
        by_date[day]['impressions'] += 1
        by_date[day]['users'].add(row['user_id'])
        by_date[day]['targets'].add(_target_key(row))
        day_groups[day][(row['user_id'], _target_key(row))] += 1

    for day, groups in day_groups.items():
        by_date[day]['repeat_rows'] = sum(
            count - 1 for count in groups.values() if count > 1
        )

    first_day = timezone.localdate(start)
    last_day = timezone.localdate(horizon)
    all_days = []
    cursor = first_day
    while cursor <= last_day:
        data = by_date.get(cursor)
        all_days.append({
            'date': cursor,
            'has_data': data is not None,
            'impressions': data['impressions'] if data else 0,
            'user_count': len(data['users']) if data else 0,
            'target_count': len(data['targets']) if data else 0,
            'repeat_rows': data['repeat_rows'] if data else 0,
        })
        cursor += timedelta(days=1)

    retained = all_days[-DAILY_ROW_LIMIT:]
    return retained, len(all_days) - len(retained)


def build_opportunity_impression_quality(*, days=30, now=None):
    """Build a read-only quality report for opportunity-feed impressions."""
    now = now or timezone.now()
    start = _period_start(now, days)
    horizon = now + FUTURE_TOLERANCE
    period_rows = list(
        OpportunityImpression.objects
        .filter(shown_at__gte=start, shown_at__lte=horizon)
        .order_by('shown_at')
        .values('user_id', 'kind', 'demand_id', 'lost_found_post_id', 'shown_at')
    )
    future_count = OpportunityImpression.objects.filter(
        shown_at__gt=horizon,
    ).count()

    kind_rows, unknown_kind_count, unknown_kind_key_count, missing_kind_count = (
        _kind_shape(period_rows)
    )
    target_rows = _target_rows(period_rows)
    repeat = _repeat_shape(period_rows)
    active_dates = {
        timezone.localdate(row['shown_at']) for row in period_rows
    }
    first_day = timezone.localdate(start)
    last_day = timezone.localdate(horizon)
    calendar_days = (last_day - first_day).days + 1
    active_days = len(active_dates)
    daily_rows, daily_rows_truncated = _daily_rows(start, horizon, period_rows)

    summary = {
        'total_impressions': len(period_rows),
        'user_count': len({row['user_id'] for row in period_rows}),
        'target_count': len({
            _target_key(row) for row in period_rows
            if _target_key(row)[0] != 'missing'
        }),
        'first_seen_at': period_rows[0]['shown_at'] if period_rows else None,
        'last_seen_at': period_rows[-1]['shown_at'] if period_rows else None,
        'unknown_kind_count': unknown_kind_count,
        'unknown_kind_key_count': unknown_kind_key_count,
        'missing_kind_count': missing_kind_count,
        'future_count': future_count,
        'calendar_days': calendar_days,
        'active_days': active_days,
        'zero_days': max(calendar_days - active_days, 0),
        'longest_gap_hours': _longest_gap_hours(period_rows),
    }
    summary.update(repeat)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(period_rows or future_count),
        'summary': summary,
        'summary_metrics': {
            'future_tolerance_minutes': int(
                FUTURE_TOLERANCE.total_seconds() / 60
            ),
            'daily_row_limit': DAILY_ROW_LIMIT,
        },
        'kind_rows': kind_rows,
        'target_rows': target_rows,
        'daily_rows': daily_rows,
        'daily_row_count': len(daily_rows),
        'daily_rows_truncated': daily_rows_truncated,
        'daily_row_limit': DAILY_ROW_LIMIT,
        'note': (
            '写入失败只进日志、不进业务表，这里数不出失败次数；面板只读已经落库的行。'
            '同一用户、同一机会在不同时刻再次展示是合法独立展示；同一时刻的多行只提示核对，'
            '不自动删除。表里没有请求或渲染批次 ID，因此不按一屏条数倒推缺失。'
        ),
    }
