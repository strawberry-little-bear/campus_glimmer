# -*- coding: utf-8 -*-
"""Compare search signals immediately before and after a listing refresh.

A refresh writes only two facts: the latest refresh time and a cumulative
counter. Whether it brought the item back into search results is a separate
question, and the event tables used by search are the right place to answer it.
Every rendered result row and every result-page click has an event timestamp.

The module deliberately uses one item's latest refresh as its unit. Earlier
refreshes are no longer observable without inventing a history table, so they
are neither reconstructed nor silently treated as absent. On that latest
refresh, the cumulative counter is also its ordinal.

Browsing history is intentionally not used: its row is a per-user/per-item
aggregate with first time, last time, and a total count, not a sequence of
view events. Splitting that aggregate across a refresh would look precise while
being unfalsifiable.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import Favorite, Item, Order, SearchClick, SearchImpression


OBSERVATION_WINDOW = timedelta(hours=72)
MIN_GROUP_ITEMS = 12
REFRESH_ORDINALS = (1, 2, 3, 4)
ORDINAL_LABELS = {
    1: '第 1 次',
    2: '第 2 次',
    3: '第 3 次',
    4: '第 4 次',
}


def _period_start(now, days):
    """Use the same local-midnight boundary as the operations dashboard."""
    today = timezone.localdate(now)
    return timezone.make_aware(
        datetime.combine(today - timedelta(days=days - 1), time.min)
    )


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _empty_records(item_rows):
    return {
        row['id']: {
            'before_start': row['last_refreshed_at'] - OBSERVATION_WINDOW,
            'refresh_at': row['last_refreshed_at'],
            'after_end': row['last_refreshed_at'] + OBSERVATION_WINDOW,
            'before_impressions': 0,
            'after_impressions': 0,
            'before_clicks': 0,
            'after_clicks': 0,
            'before_favorites': 0,
            'after_favorites': 0,
            'before_orders': 0,
            'after_orders': 0,
        }
        for row in item_rows
    }


def _apply_events(records, rows):
    for row in rows:
        record = records.get(row['item_id'])
        if record is None:
            continue
        occurred_at = row['created_at']
        if record['before_start'] <= occurred_at < record['refresh_at']:
            prefix = 'before'
        elif record['refresh_at'] <= occurred_at < record['after_end']:
            prefix = 'after'
        else:
            continue
        record[f'{prefix}_{row["signal"]}'] += 1


def _load_events(records):
    if not records:
        return
    item_ids = records.keys()
    starts = [record['before_start'] for record in records.values()]
    ends = [record['after_end'] for record in records.values()]
    common = {
        'item_id__in': item_ids,
        'created_at__gte': min(starts),
        'created_at__lt': max(ends),
    }
    event_sets = (
        (SearchImpression.objects.filter(**common), 'impressions'),
        (SearchClick.objects.filter(**common), 'clicks'),
        (Favorite.objects.filter(**common), 'favorites'),
        (Order.objects.filter(**common), 'orders'),
    )
    for queryset, signal in event_sets:
        rows = queryset.values('item_id', 'created_at')
        _apply_events(records, [{'signal': signal, **row} for row in rows])


def _build_group(key, label, records):
    refreshes = len(records)
    before_impressions = sum(row['before_impressions'] for row in records)
    after_impressions = sum(row['after_impressions'] for row in records)
    before_clicks = sum(row['before_clicks'] for row in records)
    after_clicks = sum(row['after_clicks'] for row in records)

    return {
        'key': key,
        'label': label,
        'refreshes': refreshes,
        'before_impressions': before_impressions,
        'after_impressions': after_impressions,
        'after_median_impressions': _median(
            [row['after_impressions'] for row in records]
        ),
        'before_clicks': before_clicks,
        'after_clicks': after_clicks,
        'before_click_rate': _rate(before_clicks, before_impressions),
        'after_click_rate': _rate(after_clicks, after_impressions),
        'before_favorites': sum(row['before_favorites'] for row in records),
        'after_favorites': sum(row['after_favorites'] for row in records),
        'before_orders': sum(row['before_orders'] for row in records),
        'after_orders': sum(row['after_orders'] for row in records),
        'has_sample': refreshes >= MIN_GROUP_ITEMS,
    }


def build_refresh_response(*, days=30, now=None):
    """Build the read-only before/after search response table."""
    now = now or timezone.now()
    days = max(1, int(days))
    start = _period_start(now, days)

    candidate_rows = list(
        Item.objects.filter(
            last_refreshed_at__gte=start,
            last_refreshed_at__lte=now,
        ).values(
            'id',
            'created_at',
            'last_refreshed_at',
            'refresh_count',
        ).order_by('id')
    )

    grouped_rows = {ordinal: [] for ordinal in REFRESH_ORDINALS}
    summary = {
        'observed_refreshes': len(candidate_rows),
        'eligible_refreshes': 0,
        'immature_refreshes': 0,
        'no_full_pre_window_refreshes': 0,
        'invalid_timestamp_refreshes': 0,
        'unexpected_ordinal_refreshes': 0,
    }

    for row in candidate_rows:
        refresh_at = row['last_refreshed_at']
        if refresh_at <= row['created_at']:
            summary['invalid_timestamp_refreshes'] += 1
        elif refresh_at + OBSERVATION_WINDOW > now:
            summary['immature_refreshes'] += 1
        elif row['created_at'] > refresh_at - OBSERVATION_WINDOW:
            summary['no_full_pre_window_refreshes'] += 1
        elif row['refresh_count'] not in grouped_rows:
            summary['unexpected_ordinal_refreshes'] += 1
        else:
            summary['eligible_refreshes'] += 1
            grouped_rows[row['refresh_count']].append(row)

    eligible_rows = [
        row for rows in grouped_rows.values() for row in rows
    ]
    records = _empty_records(eligible_rows)
    _load_events(records)
    record_values = list(records.values())

    rows = [
        _build_group(
            f'ordinal-{ordinal}',
            ORDINAL_LABELS[ordinal],
            [records[row['id']] for row in grouped_rows[ordinal]],
        )
        for ordinal in REFRESH_ORDINALS
    ]
    overall = _build_group('all', '全部次序', record_values)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'window_hours': OBSERVATION_WINDOW.total_seconds() // 3600,
        'min_group_items': MIN_GROUP_ITEMS,
        'has_data': bool(candidate_rows),
        'summary': summary,
        'overall': overall,
        'rows': rows,
        'note': (
            '分析单位是商品当前可观察到的最近一次刷新；前后各看 72 小时。'
            '搜索曝光、点击、收藏和预约只按发生时间归窗，本表只描述共同变化，'
            '不把后窗高于前窗解释为刷新带来的因果提升。'
        ),
    }

