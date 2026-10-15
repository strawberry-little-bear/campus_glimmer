# -*- coding: utf-8 -*-
"""出售价格轨迹的只读运营口径。

`ItemPriceChange` 是从收藏降价提醒开始沉淀的出售价格流水。对买家它是一条
提醒；对运营它还能回答供给侧问题：调价发生多少次、方向和幅度是什么形状、
是否集中在少数商品、当前分类下主要出现在哪里。

本模块刻意只读有限字段：不取商品标题和描述，不取卖家用户名，不取通知原文，
也不取收藏人名单。页面与 CSV 都从同一份聚合结果读取，避免下载件变成另一套口径。
"""

from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .models import ItemPriceChange


PERIOD_CHOICES = {7, 30, 90, 365}
MIN_SAMPLE = 12
PERCENT_QUANTUM = Decimal('0.01')
MAGNITUDE_BUCKETS = (
    (Decimal('5'), '0–5%'),
    (Decimal('10'), '5–10%'),
    (Decimal('20'), '10–20%'),
    (None, '20% 以上'),
)
REPEAT_BUCKETS = (
    (1, '调价 1 次', 'exactly_one'),
    (2, '调价 2 次', 'exactly_two'),
    (None, '调价 3 次及以上', 'three_or_more'),
)
UNCATEGORIZED_LABEL = '未分类'


def _round_percent(value):
    return Decimal(value).quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def _share(part, whole):
    """返回百分比；分母为 0 时返回 None，不把缺数伪装成 0%。"""
    return _round_percent(Decimal(part) / Decimal(whole) * 100) if whole else None


def _percentile(values, fraction):
    """最近秩分位，避免不同数据库方言给同一面板不同结果。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = int(round((len(ordered) - 1) * fraction))
    return _round_percent(ordered[rank])


def _bucket_index(ratio):
    for index, (upper, _) in enumerate(MAGNITUDE_BUCKETS):
        if upper is None or ratio < upper:
            return index
    return len(MAGNITUDE_BUCKETS) - 1


def _empty_direction(key, label):
    return {
        'key': key,
        'label': label,
        'count': 0,
        'share': None,
        'median_ratio': None,
        'p90_ratio': None,
        'bucket_counts': [0 for _ in MAGNITUDE_BUCKETS],
    }


def _summary(totals, is_small_sample):
    if not totals['change_count']:
        return '当前周期没有出售价格轨迹，功能上线前的历史编辑不会在这里回填。'
    if is_small_sample:
        return (
            f"当前周期只有 {totals['change_count']} 条出售价格轨迹，"
            f"少于 {MIN_SAMPLE} 条，只报数字，不给方向结论。"
        )
    return (
        f"当前周期共有 {totals['change_count']} 条出售价格轨迹，"
        f"发生在 {totals['item_count']} 件商品、{totals['seller_count']} 个卖家身上。"
    )


def build_price_change_quality(start, end, now=None):
    """聚合 `[start, end)` 内的出售价格轨迹，供页面和 CSV 共用。"""
    now = now or timezone.now()
    rows = list(
        ItemPriceChange.objects.filter(
            created_at__gte=start,
            created_at__lt=end,
        ).values(
            'item_id',
            'old_price',
            'new_price',
            'item__seller_id',
            'item__category_id',
            'item__category__name',
        )
    )

    item_ids = {row['item_id'] for row in rows}
    seller_ids = {
        row['item__seller_id'] for row in rows if row['item__seller_id'] is not None
    }
    directions = {
        'drop': _empty_direction('drop', '降价'),
        'rise': _empty_direction('rise', '上涨'),
    }
    ratios = {'drop': [], 'rise': []}
    categories = {}

    for row in rows:
        old_price = row['old_price']
        new_price = row['new_price']
        if old_price == 0:
            direction_key = None
        elif new_price < old_price:
            direction_key = 'drop'
        else:
            direction_key = 'rise'

        if direction_key:
            direction = directions[direction_key]
            direction['count'] += 1
            ratio = _round_percent(
                abs((new_price - old_price) / old_price) * Decimal(100),
            )
            ratios[direction_key].append(ratio)
            direction['bucket_counts'][_bucket_index(ratio)] += 1

        category_id = row['item__category_id']
        category = categories.setdefault(category_id, {
            'category_id': category_id,
            'name': row['item__category__name'] or UNCATEGORIZED_LABEL,
            'change_count': 0,
            'drop_count': 0,
            'rise_count': 0,
            'zero_base_count': 0,
            'drop_share': None,
            'is_small_sample': True,
        })
        category['change_count'] += 1
        if direction_key == 'drop':
            category['drop_count'] += 1
        elif direction_key == 'rise':
            category['rise_count'] += 1
        else:
            category['zero_base_count'] += 1

    totals = {
        'change_count': len(rows),
        'item_count': len(item_ids),
        'seller_count': len(seller_ids),
        'drop_count': directions['drop']['count'],
        'rise_count': directions['rise']['count'],
        'zero_base_count': sum(
            row['zero_base_count'] for row in categories.values()
        ),
    }
    is_small_sample = totals['change_count'] < MIN_SAMPLE

    # 零元基线保留在总览、重复调价和分类总数里；它没有可解释的方向幅度，
    # 因此不进入涨跌幅分档，也不参与两个方向的中位数与 P90。
    totals['drop_share'] = (
        _share(totals['drop_count'], totals['change_count'])
        if not is_small_sample else None
    )
    totals['rise_share'] = (
        _share(totals['rise_count'], totals['change_count'])
        if not is_small_sample else None
    )

    for direction in directions.values():
        if not is_small_sample:
            direction['share'] = _share(direction['count'], totals['change_count'])
            direction['median_ratio'] = _percentile(
                ratios[direction['key']], 0.5,
            )
            direction['p90_ratio'] = _percentile(ratios[direction['key']], 0.9)

    magnitude_rows = []
    for direction_key in ('drop', 'rise'):
        direction = directions[direction_key]
        for bucket_index, (_, bucket_label) in enumerate(MAGNITUDE_BUCKETS):
            count = direction['bucket_counts'][bucket_index]
            magnitude_rows.append({
                'direction': direction['label'],
                'bucket': bucket_label,
                'count': count,
                'share': (
                    _share(count, direction['count'])
                    if not is_small_sample else None
                ),
            })

    change_counts = defaultdict(int)
    for row in rows:
        change_counts[row['item_id']] += 1
    repeat_values = {'exactly_one': 0, 'exactly_two': 0, 'three_or_more': 0}
    for count in change_counts.values():
        if count == 1:
            repeat_values['exactly_one'] += 1
        elif count == 2:
            repeat_values['exactly_two'] += 1
        else:
            repeat_values['three_or_more'] += 1
    repeat_rows = [
        {
            'key': key,
            'label': label,
            'item_count': repeat_values[key],
        }
        for _, label, key in REPEAT_BUCKETS
    ]

    for category in categories.values():
        category['is_small_sample'] = (
            category['change_count'] < MIN_SAMPLE
        )
        if not category['is_small_sample']:
            category['drop_share'] = _share(
                category['drop_count'], category['change_count'],
            )
    category_rows = sorted(
        categories.values(),
        key=lambda row: (-row['change_count'], row['name'], row['category_id'] or 0),
    )

    return {
        'period_start': start,
        'period_end': end,
        'generated_at': now,
        'has_data': bool(rows),
        'min_sample': MIN_SAMPLE,
        'is_small_sample': is_small_sample,
        'totals': totals,
        'directions': directions,
        'magnitude_rows': magnitude_rows,
        'repeat_rows': repeat_rows,
        'category_rows': category_rows,
        'summary': _summary(totals, is_small_sample),
    }


def build_price_change_quality_panel(days=30, now=None):
    """把看板周期转换成 `[start, now)`，复用同一个核心构建函数。"""
    if days not in PERIOD_CHOICES:
        days = 30
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    start_date = local_now.date() - timedelta(days=days - 1)
    start = timezone.make_aware(datetime.combine(start_date, dt_time.min))
    return build_price_change_quality(start, now, now=now)
