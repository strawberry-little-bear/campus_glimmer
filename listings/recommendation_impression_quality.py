# -*- coding: utf-8 -*-
"""Read the shape of homepage recommendation impression rows.

``RecommendationImpression`` made "a recommendation card was rendered" an
on-disk fact. The write is deliberately non-fatal: if ``bulk_create`` fails,
the homepage still renders and only a log line is emitted. This module cannot
count those failures because they never become business rows.

What it can honestly measure is the quality of the rows that did land: whether
their reason keys are still explainable, how zero scores and anonymous visitors
are distributed, whether positions obey the current one-screen contract, and
how continuously the log covers the selected period.

The model has no render/request batch identifier, so this module never infers
how many cards a screen should have written. Repeats are also not errors: seeing
the same item again is a separate impression, not a row to collapse.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import RecommendationImpression
from .recommendations import REASONS, REASON_LABELS


HOME_POSITION_LIMIT = 8
MIN_REASON_IMPRESSIONS = 24


def _period_start(now, days):
    """Use the same local-midnight boundary as the operations dashboard."""
    today = timezone.localdate(now)
    return timezone.make_aware(
        datetime.combine(today - timedelta(days=days - 1), time.min)
    )


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _longest_gap_hours(rows):
    """Longest observable gap between landed rows, not a write-failure test."""
    if len(rows) < 2:
        return None
    times = [row['created_at'] for row in rows]
    longest = max(
        (right - left).total_seconds()
        for left, right in zip(times, times[1:])
    )
    return round(longest / 3600, 1)


def _calendar_days(start, now):
    first = timezone.localdate(start)
    last = timezone.localdate(now)
    return (last - first).days + 1


def _repeat_shape(rows):
    """Describe logged-in repeat impressions without collapsing them.

    Anonymous rows are intentionally excluded. ``user_id`` being null can mean
    an anonymous visitor or an account deleted later; neither provides a stable
    identity to compare, so grouping them as ``(null, item)`` would invent a
    person who never existed.
    """
    logged_rows = [row for row in rows if row['user_id'] is not None]
    grouped = {}
    for row in logged_rows:
        key = (row['user_id'], row['item_id'])
        grouped[key] = grouped.get(key, 0) + 1

    repeat_pairs = sum(1 for count in grouped.values() if count > 1)
    repeat_rows = sum(count - 1 for count in grouped.values() if count > 1)
    return {
        'logged_in_impressions': len(logged_rows),
        'repeat_pairs': repeat_pairs,
        'repeat_rows': repeat_rows,
        'repeat_share': _rate(repeat_rows, len(logged_rows)),
    }


def _build_reason_rows(rows):
    """Fixed current reasons, plus aggregate buckets for old/blank keys."""
    counts = {key: 0 for key in REASONS}
    unknown_count = 0
    unknown_keys = set()
    missing_count = 0

    for row in rows:
        key = (row['reason_key'] or '').strip()
        if not key:
            missing_count += 1
        elif key in counts:
            counts[key] += 1
        else:
            unknown_count += 1
            unknown_keys.add(key)

    total = len(rows)
    result = []
    for key in REASONS:
        count = counts[key]
        result.append({
            'key': key,
            'label': REASON_LABELS[key],
            'count': count,
            'share': _rate(count, total),
            'is_small_sample': count < MIN_REASON_IMPRESSIONS,
        })

    for key, label, count in (
        ('unknown', '未收录理由', unknown_count),
        ('missing', '空理由', missing_count),
    ):
        result.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _rate(count, total),
            'is_small_sample': count < MIN_REASON_IMPRESSIONS,
        })

    return result, unknown_count, len(unknown_keys), missing_count


def build_recommendation_impression_quality(*, days=30, now=None):
    """Build a read-only shape report for homepage recommendation impressions."""
    now = now or timezone.now()
    start = _period_start(now, days)
    period_rows = list(
        RecommendationImpression.objects
        .filter(created_at__gte=start, created_at__lt=now)
        .order_by('created_at')
        .values('id', 'user_id', 'item_id', 'reason_key', 'score', 'position', 'created_at')
    )
    future_count = RecommendationImpression.objects.filter(
        created_at__gt=now,
    ).count()

    reason_rows, unknown_count, unknown_key_count, missing_reason_count = (
        _build_reason_rows(period_rows)
    )
    anonymous_count = sum(1 for row in period_rows if row['user_id'] is None)
    zero_score_count = sum(1 for row in period_rows if row['score'] == 0)
    invalid_position_count = sum(
        1 for row in period_rows
        if row['position'] < 1 or row['position'] > HOME_POSITION_LIMIT
    )
    repeat = _repeat_shape(period_rows)
    active_dates = {
        timezone.localdate(row['created_at']) for row in period_rows
    }
    calendar_days = _calendar_days(start, now)
    active_days = len(active_dates)
    longest_gap_hours = _longest_gap_hours(period_rows)

    summary = {
        'total_impressions': len(period_rows),
        'anonymous_impressions': anonymous_count,
        'anonymous_share': _rate(anonymous_count, len(period_rows)),
        'missing_reason_count': missing_reason_count,
        'unknown_reason_count': unknown_count,
        'unknown_reason_key_count': unknown_key_count,
        'zero_score_count': zero_score_count,
        'invalid_position_count': invalid_position_count,
        'future_count': future_count,
        'calendar_days': calendar_days,
        'active_days': active_days,
        'zero_days': max(calendar_days - active_days, 0),
        'longest_gap_hours': longest_gap_hours,
        'first_seen_at': period_rows[0]['created_at'] if period_rows else None,
        'last_seen_at': period_rows[-1]['created_at'] if period_rows else None,
    }
    summary.update(repeat)

    field_rows = (
        {
            'key': 'anonymous',
            'label': '匿名或已注销访客',
            'count': anonymous_count,
            'status': '合法形状',
            'is_anomaly': False,
            'note': '首页对未登录访客也渲染推荐卡，user 为空不是缺失。',
        },
        {
            'key': 'missing_reason',
            'label': '空理由 key',
            'count': missing_reason_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': '无法按当前推荐文案归组；只计数，不展示原始输入。',
        },
        {
            'key': 'unknown_reason',
            'label': '未收录理由',
            'count': unknown_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': f'共 {unknown_key_count} 种 key 不在当前 REASONS 内；不输出原始 key。',
        },
        {
            'key': 'zero_score',
            'label': '推荐分数为 0',
            'count': zero_score_count,
            'status': '只报形状',
            'is_anomaly': False,
            'note': '低分可以为 0，默认值也可以是 0，本模块不把它判成缺失。',
        },
        {
            'key': 'invalid_position',
            'label': f'展示位置不在 1—{HOME_POSITION_LIMIT}',
            'count': invalid_position_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': '只代表当前首页一次渲染最多八张的写入契约，不推断每屏缺几张。',
        },
        {
            'key': 'future_time',
            'label': '时间在未来',
            'count': future_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': '单列且不进入周期主统计；主统计只读到当前时刻。',
        },
    )

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(period_rows or future_count),
        'summary': summary,
        'metrics': {
            'home_position_limit': HOME_POSITION_LIMIT,
            'min_reason_impressions': MIN_REASON_IMPRESSIONS,
        },
        'reason_rows': reason_rows,
        'field_rows': field_rows,
        'note': (
            '写入失败只进日志、不进业务表，这里无法数出失败次数；面板只读已经落库的行。'
            '表里没有渲染批次或请求 ID，因此不按每屏八张倒推少写几张，也不把曝光量低、'
            '时间间隙或零分读成推荐效果差。'
        ),
    }
