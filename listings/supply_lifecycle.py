# -*- coding: utf-8 -*-
"""Aggregate the listing lifecycle across the whole platform for operators.

The seller-facing diagnostics in ``lifecycle_diagnostics`` answer "what should
I do with this listing". This module answers the operator question that
follows: is the supply on the platform being maintained at all?

Counting only new listings hides the most common failure mode of a campus
marketplace. A listing that was published once and never touched again looks
identical to a freshly published one in every "new items" chart, yet it is the
one nobody browses any more. So this module reads the same freshness, expiry
and refresh signals, but aggregates them platform-wide instead of per seller.

Three decisions keep the numbers defensible:

1. The platform-wide level distribution is computed once in Python from the
   same ordered rules the seller sees. Reusing ``_level_for`` guarantees an
   operator and a seller looking at the same listing agree on its label.
2. Seller coverage is measured over *sellers with something to maintain*,
   not over all registered users. A user who never published anything has no
   lifecycle to maintain, and counting them would dilute the number into
   meaninglessness.
3. Refresh usage is reported as a distribution rather than an average. The
   interesting question is not "how many refreshes on average" but "how many
   sellers have already exhausted their monthly quota and therefore cannot
   promote anything any more".
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .lifecycle_diagnostics import (
    ATTENTION_LEVELS,
    LEVELS,
    LEVEL_LABELS,
    MAX_REFRESH_PER_MONTH,
    STALE_DAYS,
    _level_for,
    _refresh_state,
)
from .models import BrowsingHistory, Favorite, Item, Order


# A listing counts as "recently engaged" if any of these signals happened in
# the window. It is deliberately a weak test: the point is to separate
# "somebody looked at it" from "nobody looked at it", not to grade demand.
ENGAGEMENT_WINDOW_DAYS = 14

# Sellers are grouped by how many listings they still have to maintain. The
# buckets are coarse because the question is structural ("is this person a
# power seller or a one-off"), not precise.
SELLER_LOAD_BUCKETS = (
    (1, 1, '仅 1 件在管'),
    (2, 3, '2-3 件在管'),
    (4, 8, '4-8 件在管'),
    (9, None, '9 件以上在管'),
)

def _level_counts(items, *, now):
    """Count listings per lifecycle level using the seller-facing rules.

    Levels are only meaningful for listings that are still on display;
    terminal states collapse into ``archived`` by design, so the caller can
    report "already finished" without inventing extra buckets.
    """
    counts = {key: 0 for key, _label, _tone in LEVELS}
    for item in items:
        level, _label, _tone, _reasons, _actions = _level_for(item, now=now)
        counts[level] += 1
    return counts


def _engagement_map(item_ids, *, now):
    """Which listings were actually touched by someone in the recent window.

    Every signal is one grouped query so the aggregation cost does not grow
    with the number of listings on the platform.
    """
    if not item_ids:
        return set()
    window_start = now - timedelta(days=ENGAGEMENT_WINDOW_DAYS)
    browsed = set(
        BrowsingHistory.objects.filter(
            item_id__in=item_ids, last_viewed_at__gte=window_start,
        ).values_list('item_id', flat=True)
    )
    favourited = set(
        Favorite.objects.filter(
            item_id__in=item_ids, created_at__gte=window_start,
        ).values_list('item_id', flat=True)
    )
    ordered = set(
        Order.objects.filter(
            item_id__in=item_ids, created_at__gte=window_start,
        ).values_list('item_id', flat=True)
    )
    return browsed | favourited | ordered


def _seller_rows(available_items, *, now):
    """Per-seller maintenance load for listings that are still on display.

    ``exhausted_refresh`` is the flag that matters operationally: a seller who
    has used up the monthly refresh quota has no way left to promote a listing
    they genuinely improved, which is the case where a human should look.
    """
    engaged = _engagement_map([item.pk for item in available_items], now=now)
    sellers = {}
    for item in available_items:
        row = sellers.setdefault(item.seller_id, {
            'seller_id': item.seller_id,
            'active_count': 0,
            'attention_count': 0,
            'stale_count': 0,
            'unengaged_count': 0,
            'refreshed_in_window_count': 0,
            'exhausted_refresh': False,
        })
        row['active_count'] += 1
        level, _label, _tone, _reasons, _actions = _level_for(item, now=now)
        if level in ATTENTION_LEVELS:
            row['attention_count'] += 1
        if item.freshness_age_days >= STALE_DAYS:
            row['stale_count'] += 1
        if item.pk not in engaged:
            row['unengaged_count'] += 1
        can_refresh, _reason = _refresh_state(item, now=now)
        if item.refresh_count and not can_refresh:
            row['refreshed_in_window_count'] += 1
        if item.refresh_count >= MAX_REFRESH_PER_MONTH:
            row['exhausted_refresh'] = True
    return list(sellers.values()), engaged

def build_supply_lifecycle(days=30, *, now=None):
    """Platform-wide listing lifecycle health for the operations dashboard.

    ``days`` only affects the period-scoped counters (new listings, refreshes
    performed in the period). The level distribution and seller load describe
    the current state of the supply, which has no period.
    """
    now = now or timezone.now()
    period_start = now - timedelta(days=days)

    available_items = list(
        Item.objects.filter(
            status='available',
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now),
        ).select_related('seller')
    )

    counts = _level_counts(available_items, now=now)
    seller_rows, engaged = _seller_rows(available_items, now=now)

    total_available = len(available_items)
    attention_count = sum(
        counts[level] for level in ATTENTION_LEVELS
    )

    # 超过 45 天没有任何互动信号的商品，是最典型的“发布完就没人管”。
    # engagement 集合已经由 _seller_rows 一次性查好，这里直接复用。
    stale_unengaged = sum(
        1
        for item in available_items
        if item.freshness_age_days >= STALE_DAYS and item.pk not in engaged
    )

    period_new = Item.objects.filter(
        created_at__gte=period_start, created_at__lte=now,
    ).count()
    period_refreshes = Item.objects.filter(
        last_refreshed_at__gte=period_start, last_refreshed_at__lte=now,
    ).count()
    period_refreshed_listings = Item.objects.filter(
        last_refreshed_at__gte=period_start, last_refreshed_at__lte=now,
    ).values('seller').distinct().count()

    sellers_with_listings = len(seller_rows)
    sellers_keeping_up = sum(1 for row in seller_rows if not row['attention_count'])

    level_rows = [
        {
            'key': key,
            'label': LEVEL_LABELS[key],
            'count': counts[key],
            'share': round(counts[key] / total_available * 100, 1) if total_available else 0,
        }
        for key, _label, _tone in LEVELS
        if key != 'archived'
    ]

    seller_load_rows = []
    for low, high, label in SELLER_LOAD_BUCKETS:
        if high is None:
            matching = [row for row in seller_rows if row['active_count'] >= low]
        else:
            matching = [
                row for row in seller_rows
                if low <= row['active_count'] <= high
            ]
        if not matching:
            continue
        seller_load_rows.append({
            'label': label,
            'seller_count': len(matching),
            'active_count': sum(row['active_count'] for row in matching),
            'attention_count': sum(row['attention_count'] for row in matching),
            'unengaged_count': sum(row['unengaged_count'] for row in matching),
            'exhausted_seller_count': sum(1 for row in matching if row['exhausted_refresh']),
        })

    # 刷新额度已用尽的卖家：他们想维护也没有工具了，是最值得运营介入的一群。
    exhausted_sellers = [row for row in seller_rows if row['exhausted_refresh']]
    # 在管商品全部缺乏维护的卖家：不是不会用，是没人告诉他们。
    neglected_sellers = [
        row for row in seller_rows
        if row['active_count'] and row['attention_count'] == row['active_count']
    ]

    return {
        'period_days': days,
        'summary': {
            'available_items': total_available,
            'attention_items': attention_count,
            'attention_share': round(attention_count / total_available * 100, 1) if total_available else 0,
            'stale_unengaged': stale_unengaged,
            'period_new': period_new,
            'period_refreshes': period_refreshes,
            'period_refreshed_sellers': period_refreshed_listings,
            'sellers_with_listings': sellers_with_listings,
            'sellers_keeping_up': sellers_keeping_up,
            'seller_coverage': round(sellers_keeping_up / sellers_with_listings * 100, 1) if sellers_with_listings else 0,
            'exhausted_sellers': len(exhausted_sellers),
            'neglected_sellers': len(neglected_sellers),
        },
        'level_rows': level_rows,
        'seller_load_rows': seller_load_rows,
        'recommendations': _build_recommendations(
            total_available=total_available,
            attention_count=attention_count,
            stale_unengaged=stale_unengaged,
            sellers_with_listings=sellers_with_listings,
            sellers_keeping_up=sellers_keeping_up,
            exhausted_sellers=len(exhausted_sellers),
            neglected_sellers=len(neglected_sellers),
        ),
    }

def _build_recommendations(
    *, total_available, attention_count, stale_unengaged, sellers_with_listings,
    sellers_keeping_up, exhausted_sellers, neglected_sellers,
):
    """Turn the counters into the small number of actions an operator can take.

    Every recommendation states the threshold that triggered it, so the panel
    stays honest: when nothing crosses a threshold the list is empty instead of
    being padded with generic advice.
    """
    rows = []
    if total_available and attention_count / total_available >= 0.2:
        rows.append(
            f'当前在售商品中有 {attention_count} 件需要卖家做决策，占比已超过 20%，'
            '建议在首页或推送里给一批「即将到期」的商品增加曝光，同时引导卖家补充图片与描述。'
        )
    if stale_unengaged >= 10:
        rows.append(
            f'有 {stale_unengaged} 件商品发布超过 45 天且最近两周没有任何浏览、收藏或预约，'
            '属于典型的“发布后无人维护”，可以通过站内提醒联系卖家，或在下沉列表里做一次集中清理。'
        )
    if sellers_with_listings and sellers_keeping_up / sellers_with_listings < 0.6:
        rows.append(
            f'有在售商品的 {sellers_with_listings - sellers_keeping_up} 位卖家名下至少有一件商品需要处理，'
            '建议把生命周期诊断入口做得更显眼，而不是只放在「我的发布」里。'
        )
    if exhausted_sellers >= 5:
        rows.append(
            f'有 {exhausted_sellers} 位卖家本月的刷新额度已经用完，'
            '如果其中包含质量较好的商品，可以考虑单独放宽额度或改用专题位推荐。'
        )
    if neglected_sellers >= 3:
        rows.append(
            f'有 {neglected_sellers} 位卖家名下所有在售商品都处于需要处理的状态，'
            '更适合一次性触达（例如汇总提醒），而不是逐件发通知。'
        )
    return rows
