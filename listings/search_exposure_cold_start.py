# -*- coding: utf-8 -*-
"""新商品从发布到首次搜索曝光的只读冷启动队列。

这个模块不从点击率倒推搜索好坏。它先回到供给侧：一件商品发布之后，
有没有在可观察窗口内进入搜索结果页，以及第一次进入用了多久。未曝光可能
来自低搜索量、分类冷门或学期节奏，所以这里只报形状，不调排序、不置顶。
"""

from collections import defaultdict
from datetime import datetime, time, timedelta
from math import ceil

from django.utils import timezone

from .models import Item, SearchClick, SearchImpression


ITEM_ROW_LIMIT = 20
OBSERVATION_HOURS = 48

LATENCY_BUCKETS = (
    ('within_1_hour', '1 小时内', 1),
    ('within_6_hours', '1—6 小时', 6),
    ('within_24_hours', '6—24 小时', 24),
    ('within_3_days', '1—3 天', 72),
    ('over_3_days', '超过 3 天', None),
)


def _period_start(now, days):
    """Use the operations dashboard's local-midnight boundary."""
    today = timezone.localdate(now)
    return timezone.make_aware(
        datetime.combine(today - timedelta(days=days - 1), time.min)
    )


def _latency_bucket(hours):
    for _key, label, limit in LATENCY_BUCKETS:
        if limit is None or hours <= limit:
            return label
    return LATENCY_BUCKETS[-1][1]


def _nearest_rank(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = ceil(percentile * len(ordered)) - 1
    return round(ordered[index], 1)


def _share(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _item_shape(row):
    if not row['has_exposure']:
        return '无曝光但有点击' if row['has_click'] else '观察 48 小时后未曝光'
    if row['early_impression_count']:
        return '有早于发布的曝光；主统计取发布后的首曝'
    if row['early_click_count']:
        return '有早于首曝的点击；不算首曝后点击'
    return '发布后正常首曝'


def _build_item_rows(items, impressions_by_item, clicks_by_item):
    rows = []
    for item in items:
        impressions = impressions_by_item.get(item.id, [])
        clicks = clicks_by_item.get(item.id, [])
        valid_impressions = [
            impression for impression in impressions
            if impression['created_at'] >= item.created_at
        ]
        early_impressions = [
            impression for impression in impressions
            if impression['created_at'] < item.created_at
        ]
        first_exposure = valid_impressions[0]['created_at'] if valid_impressions else None
        first_exposure_hours = (
            (first_exposure - item.created_at).total_seconds() / 3600
            if first_exposure is not None else None
        )
        later_clicks = [
            click for click in clicks
            if first_exposure is not None and click['created_at'] >= first_exposure
        ]
        early_clicks = [
            click for click in clicks
            if first_exposure is not None and click['created_at'] < first_exposure
        ]
        row = {
            'item_id': item.id,
            'category_name': item.category.name,
            'trade_mode_label': item.get_trade_mode_display(),
            'status_label': item.get_status_display(),
            'created_at': item.created_at,
            'first_exposure_at': first_exposure,
            'first_exposure_hours': first_exposure_hours,
            'latency_label': (
                _latency_bucket(first_exposure_hours)
                if first_exposure_hours is not None else '未首曝'
            ),
            'has_exposure': first_exposure is not None,
            'has_click': bool(clicks),
            'has_click_after_exposure': bool(later_clicks),
            'early_impression_count': len(early_impressions),
            'early_click_count': len(early_clicks),
        }
        row['shape'] = _item_shape(row)
        rows.append(row)
    rows.sort(key=lambda row: (
        0 if row['has_exposure'] else 1,
        -(row['first_exposure_hours'] if row['first_exposure_hours'] is not None else float('inf')),
        row['created_at'],
        row['item_id'],
    ))
    return rows


def _build_latency_rows(item_rows):
    cohort_count = len(item_rows)
    counts = {label: 0 for _key, label, _limit in LATENCY_BUCKETS}
    unexposed_count = 0
    for row in item_rows:
        if row['has_exposure']:
            counts[row['latency_label']] += 1
        else:
            unexposed_count += 1
    output = [
        {
            'label': label,
            'count': counts[label],
            'share': _share(counts[label], cohort_count),
        }
        for _key, label, _limit in LATENCY_BUCKETS
    ]
    output.append({
        'label': '观察 48 小时后未曝光',
        'count': unexposed_count,
        'share': _share(unexposed_count, cohort_count),
    })
    return output


def _build_category_rows(item_rows):
    grouped = defaultdict(list)
    for row in item_rows:
        grouped[row['category_name']].append(row)
    rows = []
    for category_name, rows_in_category in grouped.items():
        exposed_hours = [
            row['first_exposure_hours'] for row in rows_in_category
            if row['first_exposure_hours'] is not None
        ]
        exposed_count = len(exposed_hours)
        cohort_count = len(rows_in_category)
        rows.append({
            'category_name': category_name,
            'cohort_count': cohort_count,
            'exposed_count': exposed_count,
            'unexposed_count': cohort_count - exposed_count,
            'clicked_after_exposure_count': sum(
                row['has_click_after_exposure'] for row in rows_in_category
            ),
            'median_first_exposure_hours': _nearest_rank(exposed_hours, 0.5),
            'p90_first_exposure_hours': _nearest_rank(exposed_hours, 0.9),
            'unexposed_share': _share(cohort_count - exposed_count, cohort_count),
        })
    rows.sort(key=lambda row: (
        -row['unexposed_count'], -row['cohort_count'],
        row['category_name'],
    ))
    return rows


def build_search_exposure_cold_start(days=30, *, now=None):
    """Build the read-only listing-to-first-search-exposure cohort report."""

    moment = timezone.now() if now is None else now
    period_start = _period_start(moment, days)
    observation_cutoff = moment - timedelta(hours=OBSERVATION_HOURS)
    observed_items = list(
        Item.objects.select_related('category')
        .filter(created_at__gte=period_start, created_at__lt=moment)
        .order_by('created_at', 'id')
    )
    cohort_items = [
        item for item in observed_items if item.created_at < observation_cutoff
    ]
    cohort_ids = [item.id for item in cohort_items]
    impressions_by_item = defaultdict(list)
    clicks_by_item = defaultdict(list)
    if cohort_ids:
        for impression in (
            SearchImpression.objects
            .filter(item_id__in=cohort_ids, created_at__lt=moment)
            .order_by('item_id', 'created_at', 'id')
            .values('id', 'item_id', 'created_at')
        ):
            impressions_by_item[impression['item_id']].append(impression)
        for click in (
            SearchClick.objects
            .filter(item_id__in=cohort_ids, created_at__lt=moment)
            .order_by('item_id', 'created_at', 'id')
            .values('id', 'item_id', 'created_at')
        ):
            clicks_by_item[click['item_id']].append(click)

    item_rows = _build_item_rows(cohort_items, impressions_by_item, clicks_by_item)
    category_rows = _build_category_rows(item_rows)
    exposed_hours = [
        row['first_exposure_hours'] for row in item_rows
        if row['first_exposure_hours'] is not None
    ]
    exposed_count = len(exposed_hours)
    cohort_count = len(cohort_items)
    median_hours = _nearest_rank(exposed_hours, 0.5)
    p90_hours = _nearest_rank(exposed_hours, 0.9)

    if not observed_items:
        summary = '统计周期内还没有新发布商品。'
    elif not cohort_items:
        summary = (
            f'周期内有 {len(observed_items)} 件新商品，但都未满 '
            f'{OBSERVATION_HOURS} 小时观察期，先不判断冷启动。'
        )
    else:
        median_text = f'{median_hours:g} 小时' if median_hours is not None else '暂无'
        summary = (
            f'可观察队列 {cohort_count} 件：{exposed_count} 件已首曝，'
            f'{cohort_count - exposed_count} 件观察 {OBSERVATION_HOURS} 小时后仍未曝光；'
            f'首曝中位等待 {median_text}。'
        )

    return {
        'summary': summary,
        'has_data': bool(observed_items),
        'period_days': days,
        'period_start': period_start,
        'observation_cutoff': observation_cutoff,
        'observation_hours': OBSERVATION_HOURS,
        'item_row_limit': ITEM_ROW_LIMIT,
        'item_rows': item_rows,
        'item_page_rows': item_rows[:ITEM_ROW_LIMIT],
        'item_total_count': len(item_rows),
        'latency_rows': _build_latency_rows(item_rows),
        'category_rows': category_rows,
        'metrics': {
            'observed_item_count': len(observed_items),
            'immature_item_count': len(observed_items) - cohort_count,
            'cohort_item_count': cohort_count,
            'exposed_item_count': exposed_count,
            'unexposed_item_count': cohort_count - exposed_count,
            'clicked_after_exposure_item_count': sum(
                row['has_click_after_exposure'] for row in item_rows
            ),
            'early_impression_count': sum(
                row['early_impression_count'] for row in item_rows
            ),
            'early_click_count': sum(row['early_click_count'] for row in item_rows),
            'click_without_exposure_item_count': sum(
                not row['has_exposure'] and row['has_click'] for row in item_rows
            ),
            'median_first_exposure_hours': median_hours,
            'p90_first_exposure_hours': p90_hours,
        },
        'period_note': (
            f'队列截止在当前时刻前 {OBSERVATION_HOURS} 小时；最新发布只登记为未成熟，不进入冷启动判断。'
        ),
        'boundary_note': (
            '只读冷启动观察：不调搜索排序、不自动置顶、不给商品打分，也不触发卖家提醒。'
            '清单不输出标题、描述、价格、卖家、搜索词或用户。'
        ),
    }
