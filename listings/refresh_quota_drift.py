# -*- coding: utf-8 -*-
"""刷新额度为什么是一次性的：商品刷新上限的口径分歧。

`refresh_item`（`views.py` 2115 行起）做的三件事：把 `last_refreshed_at` 推到
当前时刻、把 `refresh_count` 加一、让商品回到列表前排。它有一条限流
`_refresh_state`，判据三条，其中第二条是 `refresh_count >= 4`。

问题在第二条和它的文案之间。文案写的是「**本月**刷新次数已达上限（4 次）」，
常量叫 `MAX_REFRESH_PER_MONTH`，`supply_lifecycle` 的英文 docstring 说
"monthly quota"——而 `refresh_count` 是一个从不清零的累计计数器，模型里的
help_text 只写「记录卖家主动刷新次数」，项目里没有任何一处按月归零。

所以上限实际生效的口径是：**这件商品从发布到现在，一辈子最多刷新 4 次**。
两条规则从来不一致过，而说的是按月的那一方（文案、常量名、英文注释）全都
站在同一侧，被执行的只有累计。开学时发布的一件商品，到下学期就再也没法刷
新了，而它在文案里被告知的是「本月额度已用完，下个月还有」。

本模块把这个分歧量出来。它回答的核心问题只有一个：**「本月 4 次」这个说法，
对多少商品来说是空的**——一件商品已经用尽额度，且它最后一次刷新距今超过
30 天，那它这个月一次都没刷过，却被告知本月额度已用完。这批商品就是口径分歧
的真实代价，也是文案说谎的确切范围。

为什么先做看板而不是先改规则，见 `docs/refresh-quota-drift-design.md`。简短
版：改口径要么加一张刷新历史表，要么加一个「上次清零时间」字段，两者都要迁
移；而且改了之后所有历史累计已达 4 次的商品会同时获得一次刷新机会，上限对
存量商品会失效一轮。先有数再决定该不该动手。

有一个事实让本模块能报出一个精确的周期数：限流判据是 `refresh_count >= 4`，
而 `refresh_count` 只会被 `refresh_item` 加一，所以它**最多只能到 4，永远不可
能到 5**。于是一件商品 `refresh_count == 4` 时，它的 `last_refreshed_at` 就是
第 4 次刷新、也就是撞上额度墙的那一刻。凭这一点，「本周期内有多少件商品撞墙」
是一个精确计数，不是估计——这也是 `supply_lifecycle` 报不出来的一个数，那边
只报「周期内刷新过多少件」，分不清是第 1 次还是第 4 次。

和 `supply_lifecycle` 的分工：那边报的是「供给侧整体维护得好不好」，报的是
分布和汇总；这边报的是「刷新这一条限流本身对不对」，报的是口径。两边都会
数到「额度已用尽」这个状态，但那边的数只是汇总里的一格，这边把口径分歧拆开。

周期参数只影响「本周期撞墙数」这一个数。额度本身是一个**当前状态**，和
`supply_lifecycle` 的等级分布同一性质——它没有周期，按周期切会把一件上个月
撞墙、这个月仍然刷不动的商品算成两半。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .lifecycle_diagnostics import (
    ATTENTION_LEVELS,
    LEVEL_LABELS,
    MAX_REFRESH_PER_MONTH,
    MIN_REFRESH_GAP_HOURS,
    STALE_DAYS,
    _level_for,
    _refresh_state,
)
from .models import Item

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 「最后一次刷新距今超过多少天」才算「本月额度」这个说法对它失效。取 30 天
# 是自然月的近似，和项目里其余周期口径同一量级；它不是自然月的精确边界，
# 面板上要写清这一点。写 28 会把月初刷新的商品也划进来，写 62 会把上月刷过
# 的商品漏掉——30 是这两个错误之间最不偏的一个。
QUOTA_MONTH_DAYS = 30

# 一组至少要这么多件商品，占比才给方向。和其余模块同一个量级。
MIN_MEASURED_ITEMS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 清单最多列这么多条。清单是给「现在去核实」用的。
DRIFT_LIST_LIMIT = 20

# 「距上次刷新的间隔」分档。按天切，不按小时：这条限流本身是 24 小时量级，
# 而判断口径分歧要看的尺度是月。第一档上界写 0.5 天而不是 None——「不到一天」
# 是 24 小时间隔真正卡住的那批，写成 None 会把所有超过一天的都吸进来。
SINCE_REFRESH_BUCKETS = (
    (0.5, '不到 1 天'),
    (7, '1-7 天'),
    (30, '7-30 天'),
    (90, '30-90 天'),
    (None, '90 天以上'),
)

# 已进入交易或已下架的状态。这些商品的生命周期已经结束，「还能不能刷新」对
# 它们没有意义——但它们同样可能额度用尽，所以必须单独数出来，不能混进分母。
ENDED_STATUSES = ('sold', 'reserved', 'expired')


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _days_between(later, earlier):
    return (later - earlier).total_seconds() / 86400.0


def _bucket_index(days, buckets):
    for index, (limit, _label) in enumerate(buckets):
        if limit is None or days < limit:
            return index
    return len(buckets) - 1


def _fetch_items(*, refreshed_after=None, refreshed_before=None):
    """取全部商品。额度是当前状态，不按发布周期切——见模块 docstring。"""
    queryset = Item.objects.select_related('seller')
    if refreshed_after is not None:
        queryset = queryset.filter(last_refreshed_at__gte=refreshed_after)
    if refreshed_before is not None:
        queryset = queryset.filter(last_refreshed_at__lte=refreshed_before)
    return queryset.order_by('created_at', 'pk')


def _quota_state(item, *, now):
    """一件商品的刷新额度状态，逐条说清判据。

    返回四个布尔：额度是否已用尽、是否被累计口径吃掉、是否卡在 24 小时间隔上、
    是否还能刷。四个都是独立的——一件商品可以同时卡在间隔上而额度未用尽，
    这两条限流在用户眼里长得一模一样（都是「你现在不能刷新」），但运营该改的
    是完全不同的东西。
    """
    exhausted = item.refresh_count >= MAX_REFRESH_PER_MONTH
    can_refresh, _reason = _refresh_state(item, now=now)

    # 卡在 24 小时间隔上：现在不能刷，但额度没用尽，且距上次刷新不足一天。
    # 判据必须带上「额度未用尽」——一件既用尽额度又刚刷过的商品，它卡的是额度，
    # 不是间隔。少了这个条件，两批会被混成一个数。
    on_cooldown = (
        not can_refresh
        and not exhausted
        and item.last_refreshed_at is not None
        and item.last_refreshed_at > now - timedelta(hours=MIN_REFRESH_GAP_HOURS)
    )

    # 被累计口径吃掉：额度用尽，且最后一次刷新距今超过一个月。此时「本月 4 次」
    # 这句文案对它就是空的——它这个月一次都没刷过。从未刷新过的商品不会进这里：
    # 它的 refresh_count 是 0，压根没到「用尽」。
    quota_drifted = bool(
        exhausted
        and item.last_refreshed_at is not None
        and _days_between(now, item.last_refreshed_at) > QUOTA_MONTH_DAYS
    )
    return exhausted, quota_drifted, on_cooldown, can_refresh


def _is_drifted(item, *, now):
    return _quota_state(item, now=now)[1]


def _build_quota_stats(items, *, now):
    """额度用尽的总量，以及其中被累计口径吃掉的份额。

    分母是**仍在展示**的商品。已下架、已成交的商品虽然也可能额度用尽，但它们
    的生命周期已经结束，「还能不能刷新」对它们没有意义，单独数出来不混进来。
    """
    live = [item for item in items if item.status == 'available']
    ended = [item for item in items if item.status in ENDED_STATUSES]

    exhausted_live = [
        item for item in live if _quota_state(item, now=now)[0]
    ]
    exhausted_ended = sum(
        1 for item in ended
        if item.refresh_count >= MAX_REFRESH_PER_MONTH
    )
    drifted = [item for item in exhausted_live if _is_drifted(item, now=now)]

    return {
        'live_count': len(live),
        'ended_count': len(ended),
        'exhausted_live_count': len(exhausted_live),
        'exhausted_ended_count': exhausted_ended,
        'exhausted_fresh_count': len(exhausted_live) - len(drifted),
        'exhausted_share': _percent(len(exhausted_live), len(live)),
        'drifted_count': len(drifted),
        'drifted_share': _percent(len(drifted), len(exhausted_live)),
        'drifted_share_of_live': _percent(len(drifted), len(live)),
    }


def _build_wall_stats(items, *, now, period_start):
    """本周期内有多少件商品撞上额度墙。

    这个数之所以精确，理由在模块 docstring 里：`refresh_count` 最多只能到 4，
    所以 `refresh_count == 4` 时 `last_refreshed_at` 就是撞墙那一刻，不需要历史表。

    分母取「本周期内刷新过的商品」。一件商品本周期一次都没刷过，它不可能在
    本周期撞墙，放进分母只会把比率稀释小——而稀释小的方向和运营的直觉相反。
    """
    refreshed_in_period = [
        item for item in items
        if item.last_refreshed_at is not None
        and period_start <= item.last_refreshed_at <= now
    ]
    hit_wall = [
        item for item in refreshed_in_period
        if item.refresh_count >= MAX_REFRESH_PER_MONTH
    ]
    return {
        'refreshed_in_period': len(refreshed_in_period),
        'hit_wall_count': len(hit_wall),
        'hit_wall_share': _percent(len(hit_wall), len(refreshed_in_period)),
        'is_small_sample': len(refreshed_in_period) < MIN_MEASURED_ITEMS,
    }


def _build_since_refresh_stats(items, *, now):
    """额度用尽的商品，距上次刷新有多久了。

    只收额度用尽且刷新过的商品。从未刷新过的商品没有「距上次刷新」可言，
    它的 refresh_count 是 0，压根不进这一档——把它的间隔记成「自发布起」会
    让 90 天以上那一档虚高，而那一档的运营含义（文案在说谎）会被稀释。
    """
    exhausted_with_refresh = [
        item for item in items
        if item.refresh_count >= MAX_REFRESH_PER_MONTH and item.last_refreshed_at
    ]
    days = [
        max(_days_between(now, item.last_refreshed_at), 0.0)
        for item in exhausted_with_refresh
    ]
    buckets = [
        {'label': label, 'count': 0, 'share': None}
        for _limit, label in SINCE_REFRESH_BUCKETS
    ]
    for value in days:
        buckets[_bucket_index(value, SINCE_REFRESH_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(days))
    # 超过一个月的那些档，就是「本月 4 次」这句文案说谎的范围。单独点出来，
    # 免得运营自己把后三档加一遍。
    over_month = sum(row['count'] for row in buckets[3:])
    return {
        'measured_count': len(days),
        'bucket_rows': buckets,
        'over_month_count': over_month,
        'over_month_share': _percent(over_month, len(days)),
    }


def _build_cooldown_stats(items, *, now):
    """卡在 24 小时间隔上的商品：另一条限流，它是对的，但要和额度分开报。

    这条限流按相对时间算，不吃累计口径，所以它不会犯「本月」这个错。问题是
    用户看到的两句提示长得一模一样——一句是真的本月已用完（实际是一辈子用完），
    一句是真的今天刷过了。运营要改的是前一句，报成同一个数就不知道该改什么。
    """
    live = [item for item in items if item.status == 'available']
    on_cooldown = [
        item for item in live if _quota_state(item, now=now)[2]
    ]
    return {
        'live_count': len(live),
        'cooldown_count': len(on_cooldown),
        'cooldown_share': _percent(len(on_cooldown), len(live)),
        'is_small_sample': len(live) < MIN_MEASURED_ITEMS,
    }


def _build_drifted_state_rows(items, *, now):
    """被累计口径吃掉的商品，现在处于什么生命周期等级。

    一件额度用尽且已沉底 45 天的商品，和一件额度用尽但上周刚被浏览过的商品，
    运营该做的事完全相反：前者需要的是「补一次刷新机会」，后者什么都不需要。
    等级直接复用 `_level_for`，和卖家在自己页面上看到的标签是同一套。
    """
    drifted = [item for item in items if _is_drifted(item, now=now)]

    rows = []
    for level in ATTENTION_LEVELS:
        count = sum(
            1 for item in drifted
            if _level_for(item, now=now)[0] == level
        )
        rows.append({
            'level': level,
            'label': LEVEL_LABELS[level],
            'count': count,
            'share': _percent(count, len(drifted)),
        })
    # 「等级正常」不是 ATTENTION_LEVELS 的一档，但被累计口径吃掉的商品里必然
    # 有一批状态良好——它们只是刷不动了。不列出来会让占比加起来不到 100%，
    # 而运营会以为漏了样本。
    healthy = len(drifted) - sum(row['count'] for row in rows)
    rows.append({
        'level': 'healthy',
        'label': '等级正常',
        'count': healthy,
        'share': _percent(healthy, len(drifted)),
    })
    return {
        'measured_count': len(drifted),
        'is_small_sample': len(drifted) < MIN_MEASURED_ITEMS,
        'rows': rows,
    }


def _build_drift_list(items, *, now):
    """被累计口径吃掉的商品清单，按沉底天数从长到短。

    按 `last_active_days` 排，不按刷新次数排：运营要先去处理的是搁得最久的
    那些，不是刷得最勤的那些。标题只取前 20 个字符——清单是给「现在去核实」
    用的，不是给归档用的，原文不该整段进看板。
    """
    drifted = [item for item in items if _is_drifted(item, now=now)]
    drifted.sort(key=lambda row: (-row.freshness_age_days, row.pk))
    rows = []
    for item in drifted[:DRIFT_LIST_LIMIT]:
        rows.append({
            'item_id': item.pk,
            'title': item.title[:20],
            'refresh_count': item.refresh_count,
            'since_refresh_days': int(max(_days_between(now, item.last_refreshed_at), 0)),
            'age_days': item.freshness_age_days,
            'level': _level_for(item, now=now)[0],
            'status': item.status,
        })
    return {
        'rows': rows,
        'total_count': len(drifted),
        'truncated': len(drifted) > DRIFT_LIST_LIMIT,
    }


def _summary(quota, cooldown, wall, since_refresh):
    parts = []
    if not quota['live_count']:
        return '当前没有仍在展示的商品，刷新额度这条限流暂时无从谈起。'
    if quota['exhausted_live_count']:
        parts.append(
            f"{quota['exhausted_live_count']} 件在售商品的刷新额度已用尽"
            f"（占 {quota['exhausted_share']}%）"
        )
        if quota['drifted_count']:
            parts.append(
                f"，其中 {quota['drifted_count']} 件最后一次刷新已超过 "
                f"{QUOTA_MONTH_DAYS} 天——「本月 {MAX_REFRESH_PER_MONTH} 次」"
                f"这句文案对它们是空的"
            )
        else:
            parts.append('，且每一件的最后一次刷新都在一个月内')
    else:
        parts.append('当前没有在售商品的刷新额度被用尽')
    if wall['hit_wall_count']:
        parts.append(
            f"；本周期有 {wall['hit_wall_count']} 件商品刚撞上额度墙"
        )
    if cooldown['cooldown_count']:
        parts.append(
            f"；另有 {cooldown['cooldown_count']} 件卡在 "
            f"{MIN_REFRESH_GAP_HOURS} 小时间隔上，那是另一条限流"
        )
    return ''.join(parts) + '。'


def build_refresh_quota_drift(days=30, now=None):
    """量出「本月刷新次数」这个口径和实际生效的累计口径之间差了多少。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    items = _fetch_items()

    quota = _build_quota_stats(items, now=now)
    wall = _build_wall_stats(items, now=now, period_start=start)
    cooldown = _build_cooldown_stats(items, now=now)
    since_refresh = _build_since_refresh_stats(items, now=now)
    drifted_state = _build_drifted_state_rows(items, now=now)
    drift_list = _build_drift_list(items, now=now)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(quota['live_count']),
        'summary': _summary(quota, cooldown, wall, since_refresh),
        'quota': quota,
        'wall': wall,
        'cooldown': cooldown,
        'since_refresh': since_refresh,
        'drifted_state': drifted_state,
        'drift_list': drift_list,
        'max_refresh_per_month': MAX_REFRESH_PER_MONTH,
        'min_refresh_gap_hours': MIN_REFRESH_GAP_HOURS,
        'quota_month_days': QUOTA_MONTH_DAYS,
        'stale_days': STALE_DAYS,
        'min_measured_items': MIN_MEASURED_ITEMS,
        'noise_points': NOISE_POINTS,
    }