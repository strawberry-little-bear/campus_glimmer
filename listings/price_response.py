# -*- coding: utf-8 -*-
"""Compare search signals immediately before and after a sale-price change.

A price change explains what the buyer is being asked to pay. Whether that new
number also coincided with exposure, clicks, favorites, or orders is a separate
observational question. This module keeps those questions separate: it reads
one immutable ``ItemPriceChange`` row and the timestamped event tables used by
search, then reports both windows side by side.

The module is intentionally read-only. It does not score sellers, recommend a
better price, rewrite ranking, or write an analytics result back to a business
table. It also does not read ``BrowsingHistory.view_count``: that counter is a
per-user/per-item aggregate rather than a sequence of view events, so splitting
it across a price change would create precision that cannot be audited.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import Favorite, ItemPriceChange, Order, SearchClick, SearchImpression
from .price_change_quality import MAGNITUDE_BUCKETS


OBSERVATION_WINDOW = timedelta(hours=72)
MIN_GROUP_CHANGES = 12
DIRECTIONS = (
    ('drop', '降价'),
    ('rise', '上涨'),
    ('zero', '零元基线'),
)


def _period_start(now, days):
    """Use the same local-midnight boundary as the operations dashboard."""
    today = timezone.localdate(now)
    return timezone.make_aware(
        datetime.combine(today - timedelta(days=days - 1), time.min),
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


def _direction(row):
    old_price = row['old_price']
    new_price = row['new_price']
    if old_price == 0:
        return 'zero', None
    if new_price < old_price:
        direction = 'drop'
    else:
        direction = 'rise'
    ratio = abs(new_price - old_price) / old_price * 100
    return direction, ratio


def _bucket_label(ratio):
    for upper, label in MAGNITUDE_BUCKETS:
        if upper is None or ratio < upper:
            return label
    return MAGNITUDE_BUCKETS[-1][1]


def _empty_record(row):
    changed_at = row['created_at']
    return {
        'id': row['id'],
        'item_id': row['item_id'],
        'changed_at': changed_at,
        'before_start': changed_at - OBSERVATION_WINDOW,
        'after_end': changed_at + OBSERVATION_WINDOW,
        'direction': row['_direction'],
        'bucket': row['_bucket'],
        'before_impressions': 0,
        'after_impressions': 0,
        'before_clicks': 0,
        'after_clicks': 0,
        'before_favorites': 0,
        'after_favorites': 0,
        'before_orders': 0,
        'after_orders': 0,
    }


def _has_adjacent_change(row, history):
    for other in history.get(row['item_id'], ()):
        if other['id'] == row['id']:
            continue
        before_start = row['created_at'] - OBSERVATION_WINDOW
        after_end = row['created_at'] + OBSERVATION_WINDOW
        if before_start <= other['created_at'] < after_end:
            return True
    return False


def _load_adjacent_history(rows):
    """Read neighboring changes so one overlapping response is not counted twice."""
    if not rows:
        return {}
    item_ids = {row['item_id'] for row in rows}
    starts = [row['created_at'] - OBSERVATION_WINDOW for row in rows]
    ends = [row['created_at'] + OBSERVATION_WINDOW for row in rows]
    history = {item_id: [] for item_id in item_ids}
    adjacent_rows = ItemPriceChange.objects.filter(
        item_id__in=item_ids,
        created_at__gte=min(starts),
        created_at__lt=max(ends),
    ).values('id', 'item_id', 'created_at')
    for row in adjacent_rows:
        history.setdefault(row['item_id'], []).append(row)
    return history


def _classify_candidates(rows, now):
    summary = {
        'observed_changes': len(rows),
        'eligible_changes': 0,
        'immature_changes': 0,
        'no_full_pre_window_changes': 0,
        'adjacent_change_changes': 0,
        'invalid_timestamp_changes': 0,
        'invalid_price_changes': 0,
        'non_sale_changes': 0,
    }
    eligible = []
    history = _load_adjacent_history(rows)

    for row in rows:
        changed_at = row['created_at']
        item_created_at = row['item__created_at']
        before_start = changed_at - OBSERVATION_WINDOW
        after_end = changed_at + OBSERVATION_WINDOW

        if row['item__trade_mode'] != 'sale':
            summary['non_sale_changes'] += 1
        elif changed_at is None or item_created_at is None or changed_at <= item_created_at:
            summary['invalid_timestamp_changes'] += 1
        elif row['old_price'] is None or row['new_price'] is None or row['old_price'] < 0 or row['new_price'] < 0:
            summary['invalid_price_changes'] += 1
        elif after_end > now:
            summary['immature_changes'] += 1
        elif item_created_at > before_start:
            summary['no_full_pre_window_changes'] += 1
        elif _has_adjacent_change(row, history):
            summary['adjacent_change_changes'] += 1
        else:
            direction, ratio = _direction(row)
            eligible.append({
                **row,
                '_direction': direction,
                '_bucket': None if direction == 'zero' else _bucket_label(ratio),
            })

    summary['eligible_changes'] = len(eligible)
    return summary, eligible


def _load_events(records):
    if not records:
        return
    by_item = {}
    for record in records.values():
        by_item.setdefault(record['item_id'], []).append(record)

    starts = [record['before_start'] for record in records.values()]
    ends = [record['after_end'] for record in records.values()]
    common = {
        'item_id__in': by_item.keys(),
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
        for event in rows:
            for record in by_item.get(event['item_id'], ()):
                occurred_at = event['created_at']
                if record['before_start'] <= occurred_at < record['changed_at']:
                    prefix = 'before'
                elif record['changed_at'] <= occurred_at < record['after_end']:
                    prefix = 'after'
                else:
                    continue
                record[f'{prefix}_{signal}'] += 1


def _group(*, key, direction, bucket, label, records):
    changes = len(records)
    before_impressions = sum(row['before_impressions'] for row in records)
    after_impressions = sum(row['after_impressions'] for row in records)
    before_clicks = sum(row['before_clicks'] for row in records)
    after_clicks = sum(row['after_clicks'] for row in records)

    return {
        'key': key,
        'direction': direction,
        'bucket': bucket,
        'label': label,
        'changes': changes,
        'before_impressions': before_impressions,
        'after_impressions': after_impressions,
        'after_median_impressions': _median(
            [row['after_impressions'] for row in records],
        ),
        'before_click_rate': _rate(before_clicks, before_impressions),
        'after_click_rate': _rate(after_clicks, after_impressions),
        'before_favorites': sum(row['before_favorites'] for row in records),
        'after_favorites': sum(row['after_favorites'] for row in records),
        'before_orders': sum(row['before_orders'] for row in records),
        'after_orders': sum(row['after_orders'] for row in records),
        'has_sample': changes >= MIN_GROUP_CHANGES,
    }


def _build_groups(records):
    direction_records = {key: [] for key, _ in DIRECTIONS}
    magnitude_records = {
        (direction_key, bucket_label): []
        for direction_key in ('drop', 'rise')
        for _, bucket_label in MAGNITUDE_BUCKETS
    }
    for record in records.values():
        direction_records[record['direction']].append(record)
        if record['direction'] in ('drop', 'rise'):
            magnitude_records[(record['direction'], record['bucket'])].append(record)

    direction_rows = [
        _group(
            key=key, direction=label, bucket='—', label=label,
            records=direction_records[key],
        )
        for key, label in DIRECTIONS
    ]
    magnitude_rows = [
        _group(
            key=f'{direction_key}-{index}', direction=direction_label,
            bucket=bucket_label, label=f'{direction_label} · {bucket_label}',
            records=magnitude_records[(direction_key, bucket_label)],
        )
        for direction_key, direction_label in (('drop', '降价'), ('rise', '上涨'))
        for index, (_, bucket_label) in enumerate(MAGNITUDE_BUCKETS)
    ]
    overall = _group(
        key='all', direction='全部', bucket='—', label='全部',
        records=list(records.values()),
    )
    return direction_rows, magnitude_rows, overall


def build_price_response(*, days=30, now=None):
    """Aggregate the search response around price changes for the page and CSV."""
    now = now or timezone.now()
    start = _period_start(now, days)
    candidate_rows = list(
        ItemPriceChange.objects.filter(
            created_at__gte=start,
            created_at__lt=now,
        ).values(
            'id',
            'item_id',
            'old_price',
            'new_price',
            'created_at',
            'item__created_at',
            'item__trade_mode',
        ).order_by('item_id', 'created_at', 'id'),
    )
    summary, eligible_rows = _classify_candidates(candidate_rows, now)
    records = {row['id']: _empty_record(row) for row in eligible_rows}
    _load_events(records)
    direction_rows, magnitude_rows, overall = _build_groups(records)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'window_hours': int(OBSERVATION_WINDOW.total_seconds() // 3600),
        'min_group_changes': MIN_GROUP_CHANGES,
        'has_data': bool(candidate_rows),
        'summary': summary,
        'direction_rows': direction_rows,
        'magnitude_rows': magnitude_rows,
        'overall': overall,
        'exclusion_rows': (
            ('周期内观察到调价', summary['observed_changes'], '先全部登记，再看观察条件'),
            ('当前非出售商品', summary['non_sale_changes'], '单独计数，不进入出售价格对照'),
            ('时间异常', summary['invalid_timestamp_changes'], '单独计数，不猜测正确时间'),
            ('价格异常', summary['invalid_price_changes'], '单独计数，不猜测价格意图'),
            ('未满观察窗口', summary['immature_changes'], '不用被截断的后窗做对照'),
            ('无完整前窗', summary['no_full_pre_window_changes'], '不把新发布商品混进对照组'),
            ('相邻调价重叠', summary['adjacent_change_changes'], '避免同一事件被两次调价重复吸收'),
            ('成熟且可对照', summary['eligible_changes'], '进入方向表；幅度表只使用有基线的调价'),
        ),
        'note': (
            '本面板只描述调价前后的共同变化；后窗高于前窗不代表调价带来因果提升，'
            '也不形成卖家定价评分或自动改价建议。'
        ),
    }
