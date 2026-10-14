# -*- coding: utf-8 -*-
"""Read whether search impressions and clicks can still be paired.

``SearchImpression`` is the denominator behind search click-through and
position curves; ``SearchClick`` is the numerator. Existing reports correctly
drop rows without a search query or a usable position, but they do not show how
many rows were dropped or whether a click can still be tied to an earlier
exposure of the same result.

This module is that guardrail. It is deliberately not another search-quality
score: an orphan click can come from a bookmark, a deleted search row, a hand
written URL, or the migration window when clicks existed before impressions.
Those all need different actions, so the report keeps their shapes separate and
does not turn them into a verdict about ranking.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import SearchClick, SearchImpression


SEARCH_PAGE_SIZE = 12
LATENCY_BUCKETS = (
    ('within_5_minutes', '5 分钟内', timedelta(minutes=5)),
    ('within_1_hour', '5—60 分钟', timedelta(hours=1)),
    ('within_1_day', '1—24 小时', timedelta(days=1)),
    ('within_7_days', '1—7 天', timedelta(days=7)),
    ('over_7_days', '超过 7 天', None),
)


def _period_start(now, days):
    """Use the operations dashboard's local-midnight boundary."""
    today = timezone.localdate(now)
    return timezone.make_aware(
        datetime.combine(today - timedelta(days=days - 1), time.min)
    )


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 1) if denominator else None


def _calendar_days(start, now):
    first = timezone.localdate(start)
    last = timezone.localdate(now)
    return (last - first).days + 1


def _latency_bucket(delay):
    for _key, label, limit in LATENCY_BUCKETS:
        if limit is None or delay <= limit:
            return label
    return LATENCY_BUCKETS[-1][1]


def _group_candidates(rows):
    """Index impressions by the exact pair a click can be attributed to."""
    grouped = {}
    for row in rows:
        grouped.setdefault((row['search_query_id'], row['item_id']), []).append(row)
    for history in grouped.values():
        history.sort(key=lambda row: row['created_at'])
    return grouped


def _pair_clicks(clicks, grouped):
    """Classify each click without inventing a request-level identity.

    A click pairs with the earliest earlier impression of the same query and
    item. User identity is intentionally not compared: a link can be reopened
    after a login change or shared with another student.
    """
    matched_count = 0
    position_match_count = 0
    position_mismatch_count = 0
    before_pair_count = 0
    never_paired_count = 0
    paired_impression_ids = set()
    latency_counts = {label: 0 for _key, label, _limit in LATENCY_BUCKETS}

    for click in clicks:
        query_id = click['search_query_id']
        if query_id is None:
            # Missing-query clicks are counted in the field table. They cannot
            # be paired because the pair key itself is gone, not because the
            # click happened too early.
            continue

        history = grouped.get((query_id, click['item_id']), [])
        earlier = [row for row in history if row['created_at'] <= click['created_at']]
        if not earlier:
            if history:
                before_pair_count += 1
            else:
                never_paired_count += 1
            continue

        first = earlier[0]
        matched_count += 1
        paired_impression_ids.add(first['id'])
        latency_counts[_latency_bucket(click['created_at'] - first['created_at'])] += 1
        if click['position'] > 0:
            if any(row['position'] == click['position'] for row in earlier):
                position_match_count += 1
            else:
                position_mismatch_count += 1

    return {
        'matched_click_count': matched_count,
        'position_match_count': position_match_count,
        'position_mismatch_count': position_mismatch_count,
        'before_pair_count': before_pair_count,
        'never_paired_count': never_paired_count,
        'paired_impression_ids': paired_impression_ids,
        'latency_counts': latency_counts,
    }


def build_search_impression_pairing_quality(*, days=30, now=None):
    """Build a read-only pairing report for search impressions and clicks."""
    now = now or timezone.now()
    start = _period_start(now, days)
    impressions = list(
        SearchImpression.objects
        .filter(created_at__gte=start, created_at__lt=now)
        .order_by('created_at')
        .values('id', 'search_query_id', 'item_id', 'user_id', 'position', 'created_at')
    )
    clicks = list(
        SearchClick.objects
        .filter(created_at__gte=start, created_at__lt=now)
        .order_by('created_at')
        .values('id', 'search_query_id', 'item_id', 'user_id', 'position', 'created_at')
    )
    future_impression_count = SearchImpression.objects.filter(
        created_at__gt=now,
    ).count()
    future_click_count = SearchClick.objects.filter(created_at__gt=now).count()

    click_query_ids = {
        row['search_query_id'] for row in clicks
        if row['search_query_id'] is not None
    }
    click_item_ids = {row['item_id'] for row in clicks}
    candidate_impressions = []
    if click_query_ids and click_item_ids:
        candidate_impressions = list(
            SearchImpression.objects
            .filter(
                search_query_id__in=click_query_ids,
                item_id__in=click_item_ids,
                created_at__lte=now,
            )
            .values('id', 'search_query_id', 'item_id', 'position', 'created_at')
        )
    grouped = _group_candidates(candidate_impressions)
    pairing = _pair_clicks(clicks, grouped)

    impression_missing_query_count = sum(
        row['search_query_id'] is None for row in impressions
    )
    click_missing_query_count = sum(
        row['search_query_id'] is None for row in clicks
    )
    anonymous_impression_count = sum(row['user_id'] is None for row in impressions)
    anonymous_click_count = sum(row['user_id'] is None for row in clicks)
    zero_position_click_count = sum(row['position'] <= 0 for row in clicks)
    matched_click_count = pairing['matched_click_count']
    unmatched_click_count = len(clicks) - matched_click_count
    paired_impression_count = len(pairing['paired_impression_ids'])

    impression_dates = {
        timezone.localdate(row['created_at']) for row in impressions
    }
    click_dates = {timezone.localdate(row['created_at']) for row in clicks}
    calendar_days = _calendar_days(start, now)

    summary = {
        'total_impressions': len(impressions),
        'total_clicks': len(clicks),
        'anonymous_impressions': anonymous_impression_count,
        'anonymous_clicks': anonymous_click_count,
        'impression_missing_query_count': impression_missing_query_count,
        'click_missing_query_count': click_missing_query_count,
        'zero_position_click_count': zero_position_click_count,
        'matched_click_count': matched_click_count,
        'matched_click_share': _rate(matched_click_count, len(clicks)),
        'unmatched_click_count': unmatched_click_count,
        'never_paired_click_count': pairing['never_paired_count'],
        'click_before_pair_count': pairing['before_pair_count'],
        'position_match_count': pairing['position_match_count'],
        'position_mismatch_count': pairing['position_mismatch_count'],
        'paired_impression_count': paired_impression_count,
        'paired_impression_share': _rate(paired_impression_count, len(impressions)),
        'future_impression_count': future_impression_count,
        'future_click_count': future_click_count,
        'calendar_days': calendar_days,
        'impression_active_days': len(impression_dates),
        'click_active_days': len(click_dates),
        'either_active_days': len(impression_dates | click_dates),
        'zero_days': max(calendar_days - len(impression_dates | click_dates), 0),
    }

    latency_rows = []
    for _key, label, _limit in LATENCY_BUCKETS:
        count = pairing['latency_counts'][label]
        latency_rows.append({
            'label': label,
            'count': count,
            'share': _rate(count, matched_click_count),
        })

    field_rows = (
        {
            'key': 'anonymous_impression',
            'label': '匿名或已注销访客的曝光',
            'count': anonymous_impression_count,
            'status': '合法形状',
            'is_anomaly': False,
            'note': 'user 为空不是缺失；搜索页对未登录访客照常渲染。',
        },
        {
            'key': 'anonymous_click',
            'label': '匿名或已注销访客的点击',
            'count': anonymous_click_count,
            'status': '合法形状',
            'is_anomaly': False,
            'note': '链接可以在登录状态变化后被再次打开，不比较两次 user。',
        },
        {
            'key': 'impression_missing_query',
            'label': '曝光缺搜索记录',
            'count': impression_missing_query_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': '搜索记录删除后外键置空；这类行进不了搜索关联分母。',
        },
        {
            'key': 'click_missing_query',
            'label': '点击缺搜索记录',
            'count': click_missing_query_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': '点击保留但归因键丢失；也不并入「从未配对」。',
        },
        {
            'key': 'zero_position_click',
            'label': '点击位置为 0',
            'count': zero_position_click_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': '当前结果页生成的是从 1 开始的位置；0 会被位置面板排除。',
        },
        {
            'key': 'never_paired_click',
            'label': '点击找不到同搜索同商品曝光',
            'count': pairing['never_paired_count'],
            'status': '只报形状',
            'is_anomaly': True,
            'note': '历史点击、手写链接、数据迁移都可能长成这样，不判成因。',
        },
        {
            'key': 'click_before_pair',
            'label': '点击早于同组最早曝光',
            'count': pairing['before_pair_count'],
            'status': '只报形状',
            'is_anomaly': True,
            'note': '同组曝光存在但都在点击之后，不能把点击归给未来的曝光。',
        },
        {
            'key': 'future_time',
            'label': '时间在未来',
            'count': future_impression_count + future_click_count,
            'status': '需要核对',
            'is_anomaly': True,
            'note': (
                f'曝光 {future_impression_count} 行、点击 {future_click_count} 行；'
                '均不进入周期主统计。'
            ),
        },
    )

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(
            impressions or clicks or future_impression_count or future_click_count
        ),
        'summary': summary,
        'metrics': {
            'search_page_size': SEARCH_PAGE_SIZE,
            'latency_bucket_count': len(LATENCY_BUCKETS),
        },
        'field_rows': field_rows,
        'latency_rows': latency_rows,
        'note': (
            '本面板量的是搜索分子和分母能否配对，不是搜索效果。点击允许匹配周期外更早的同搜索同商品曝光；'
            '延迟从最早可配对曝光算起。缺搜索记录、零位置、未配对和时间异常只报形状，不判用户行为或写入失败。'
        ),
    }
