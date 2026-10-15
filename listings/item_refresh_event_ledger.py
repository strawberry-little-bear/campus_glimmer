# -*- coding: utf-8 -*-
"""Read the item-refresh event ledger without changing refresh policy.

``Item.refresh_count`` and ``Item.last_refreshed_at`` are enough for rate
limiting and current ranking, but not for history: one is a total and the other
is overwritten on every accepted refresh. ``ItemRefreshEvent`` now preserves each
accepted write. Before that history is used as an outcome signal, this panel
answers the more basic question—how much of the current counter is actually
backed by event rows, and do the rows agree with the counter?

Historical rows without events are not errors. The ledger starts at this
iteration and deliberately does not invent timestamps. A real consistency problem
is a latest event whose counter does not match the item, or a sequence with
missing ordinals after the ledger exists.
"""

from datetime import datetime, time, timedelta

from django.db.models import Count, Max, Min
from django.utils import timezone

from .models import Item, ItemRefreshEvent


ALLOWED_PERIOD_DAYS = (7, 30, 90, 365)
DEFAULT_PERIOD_DAYS = 30
FUTURE_CLOCK_TOLERANCE = timedelta(minutes=1)
DAILY_ROW_LIMIT = 30


def _period_days(days):
    value = max(1, int(days))
    return value if value in ALLOWED_PERIOD_DAYS else DEFAULT_PERIOD_DAYS


def _period_start(now, days):
    start_date = timezone.localdate(now) - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(start_date, time.min))


def _rate(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _daily_rows(events, *, start, now):
    start_day = timezone.localdate(start)
    end_day = timezone.localdate(now)
    buckets = {}
    for event in events:
        day = timezone.localdate(event.refreshed_at)
        bucket = buckets.setdefault(day, {
            'event_count': 0, 'items': set(), 'sellers': set(),
        })
        bucket['event_count'] += 1
        bucket['items'].add(event.item_id)
        bucket['sellers'].add(event.seller_id)

    rows = []
    day = start_day
    while day <= end_day:
        bucket = buckets.get(day, {'event_count': 0, 'items': set(), 'sellers': set()})
        rows.append({
            'day': day,
            'label': f'{day.month:02d}-{day.day:02d}',
            'event_count': bucket['event_count'],
            'item_count': len(bucket['items']),
            'seller_count': len(bucket['sellers']),
            'bar_height': 4,
        })
        day += timedelta(days=1)

    max_count = max((row['event_count'] for row in rows), default=0)
    for row in rows:
        if max_count:
            row['bar_height'] = max(8, round(row['event_count'] / max_count * 100))
    return rows


def _coverage():
    items = {
        row['id']: row['refresh_count']
        for row in Item.objects.filter(refresh_count__gt=0).values('id', 'refresh_count')
    }
    event_rows = ItemRefreshEvent.objects.filter(
        item_id__in=items,
    ).values('item_id').annotate(
        event_count=Count('id'),
        first_count=Min('refresh_count_after'),
        last_count=Max('refresh_count_after'),
    )
    events_by_item = {row['item_id']: row for row in event_rows}

    full_count = 0
    historical_only_count = 0
    legacy_gap_count = 0
    inconsistent_count = 0
    legacy_missing_total = 0
    latest_mismatch_count = 0
    sequence_gap_count = 0

    for item_id, current_count in items.items():
        row = events_by_item.get(item_id)
        if row is None:
            historical_only_count += 1
            legacy_missing_total += current_count
            continue

        event_count = row['event_count']
        first_count = row['first_count']
        last_count = row['last_count']
        missing_count = max(current_count - event_count, 0)
        latest_matches = last_count == current_count
        sequence_is_contiguous = event_count == last_count - first_count + 1

        if not latest_matches:
            latest_mismatch_count += 1
        if not sequence_is_contiguous:
            sequence_gap_count += 1

        if event_count == current_count and latest_matches and sequence_is_contiguous:
            full_count += 1
        elif missing_count and latest_matches and sequence_is_contiguous:
            legacy_gap_count += 1
            legacy_missing_total += missing_count
        else:
            inconsistent_count += 1

    counted_total = len(items)
    return {
        'counted_item_count': counted_total,
        'full_count': full_count,
        'historical_only_count': historical_only_count,
        'legacy_gap_count': legacy_gap_count,
        'inconsistent_count': inconsistent_count,
        'legacy_missing_total': legacy_missing_total,
        'full_coverage_rate': _rate(full_count, counted_total),
        'latest_mismatch_count': latest_mismatch_count,
        'sequence_gap_count': sequence_gap_count,
    }


def _summary(*, event_count, coverage):
    if coverage['inconsistent_count']:
        return (
            f"有 {coverage['inconsistent_count']} 件商品的事件序列与累计刷新次数不一致，"
            '需要先核对台账；这里不自动修复、不回填。'
        )
    if event_count:
        return (
            f"周期内记录 {event_count} 次真实刷新；"
            f"{coverage['full_count']} 件商品的累计次数已完整落在台账里。"
        )
    if coverage['counted_item_count']:
        return '周期内没有新刷新事件；当前累计次数来自历史字段或更早的刷新。'
    return '还没有可读取的刷新事实；被限流拒绝的请求不会写入事件。'


def build_item_refresh_event_ledger(*, days=DEFAULT_PERIOD_DAYS, now=None):
    """Build the read-only event-ledger health panel."""
    now = now or timezone.now()
    days = _period_days(days)
    start = _period_start(now, days)

    events = list(
        ItemRefreshEvent.objects.filter(
            refreshed_at__gte=start, refreshed_at__lte=now + FUTURE_CLOCK_TOLERANCE,
        ).select_related('item', 'seller').order_by('refreshed_at', 'id')
    )
    future_event_count = ItemRefreshEvent.objects.filter(
        refreshed_at__gt=now + FUTURE_CLOCK_TOLERANCE,
    ).count()
    coverage = _coverage()
    all_daily_rows = _daily_rows(
        events, start=start, now=now + FUTURE_CLOCK_TOLERANCE,
    )
    daily_rows = all_daily_rows[-DAILY_ROW_LIMIT:]

    return {
        'days': days,
        'window_start': start,
        'window_end': now,
        'event_count': len(events),
        'item_count': len({event.item_id for event in events}),
        'seller_count': len({event.seller_id for event in events}),
        'first_event_at': events[0].refreshed_at if events else None,
        'last_event_at': events[-1].refreshed_at if events else None,
        'future_event_count': future_event_count,
        'future_tolerance_minutes': int(FUTURE_CLOCK_TOLERANCE.total_seconds() // 60),
        'coverage': coverage,
        'daily_rows': daily_rows,
        'daily_row_count': len(all_daily_rows),
        'daily_rows_truncated': len(all_daily_rows) > len(daily_rows),
        'daily_row_limit': DAILY_ROW_LIMIT,
        'has_data': bool(events or coverage['counted_item_count']),
        'summary': _summary(event_count=len(events), coverage=coverage),
        'source_note': (
            '事实来源是逐次落库的商品刷新事件；拒绝的 GET、预检失败和行锁后失败都不写入。'
        ),
        'history_note': (
            '台账上线前的历史缺口照实单列，不按最新时间戳伪造事件；'
            '新事件与累计计数不一致才是需要人工核对的数据质量信号。'
        ),
    }


def build_item_refresh_event_ledger_dashboard(*, days):
    """Registry adapter for the shared operations dashboard."""
    return build_item_refresh_event_ledger(days=days)
