# -*- coding: utf-8 -*-
"""重新上架的那件商品，为什么一回来就沉底。

`Item.freshness_age_days` 读的是 `last_refreshed_at or created_at`，而
`last_refreshed_at` 只在 `refresh_item` 里被写入。`edit_item` 的重新上架分支只改
`status`——它不碰 `last_refreshed_at`，也不碰 `refresh_count`。所以重新上架这个
动作在系统里没有留下任何时间戳，一件上学期发布、这学期重新上架的商品，一回到在售
列表就带着整个假期的新鲜度欠账，`_level_for` 直接判「沉底较久」。

系统给卖家的两句话因此是矛盾的：一句「商品已重新上架」，紧接一句「距上次发布或
刷新已 90 天」。运营一开始会以为诊断坏了。诊断没坏，它说的是事实。

供给侧看板已经报了「发布超 45 天无互动」的总量，但把三种情况混进了一个数字：
发布时就没运营好一直沉底的、重新上架过而卖家以为给过它一次机会的、发布时设了很
长截止时间从没到期过的。第三种不需要运营介入，前两种需要的动作完全不同。

而这批商品恰好是**刷新额度最充裕**的那批：`refresh_count == 0` 意味着 4 次额度
一次没用过，24 小时间隔也没占着。问题最好解决的一批商品，恰好也是系统最没提醒过
的那一批。

本模块只报告，不改写任何一件商品、任何一个字段。判据是一条纯字段比较，不做推断：

    status == 'available'
    且 expires_at 存在且晚于当前时刻
    且 freshness_age_days >= STALE_DAYS
    且 refresh_count == 0

刻意没有去推断「它是否重新上架过」。重新上架不留时间戳，而 `expires_at` 在未来
这个信号新发布的商品也会有（发布时就可以设一个未来的截止时间）。所以本模块报的是
**新鲜度欠账**这个可观测量，不是重新上架这个不可观测量。

`STALE_DAYS`、`MAX_REFRESH_PER_MONTH`、`_level_for`、`_refresh_state` 全部从
`lifecycle_diagnostics` 导入——判据只允许有一份。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.db.models import Count, Q
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
from .models import BrowsingHistory, Favorite, Item, Order

# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 在售商品少于这么多件时，占比只报数字不给方向。
MIN_MEASURED_ITEMS = 12

# 清单的上限。清单是给「现在去处理」用的，不是给归档用的。
DRIFT_LIST_LIMIT = 20

# 只数当前在架的商品。已售出、已预订、已过期的商品不进分母——额度对它们没有意义，
# 混进来会把「问题好解决」这个结论冲淡。
ON_SALE_STATUS = 'available'

# 订单结局里算互动的状态。走到这两步的订单算一次真实互动，`cancelled` 不算——
# 取消是失败，不是需求。
ENGAGED_ORDER_STATUSES = ('completed', 'returned')

# 新鲜度欠账的分档。上界 None 的那一档兜底。欠账天数直接复用 STALE_DAYS 作为第一档
# 的下界，不另起一套：读者会把两个看板的「45 天」当成同一个数。
ARREARS_BUCKETS = (
    (90, '45-89 天'),
    (180, '90-179 天'),
    (365, '180-364 天'),
    (None, '365 天及以上'),
)

# 刷新次数的分档，用来证明「问题最好解决的一批恰好最没被提醒」。0 次单独一档，
# 其余按已用额度走。
REFRESH_COUNT_BUCKETS = (
    (1, '一次都没刷新过'),
    (2, '刷过 1 次'),
    (3, '刷过 2 次'),
    (MAX_REFRESH_PER_MONTH + 1, '刷过 3 次及以上'),
)

# 一件在售商品的一行快照。一次查完固定下来：分档、等级、额度分布、无互动对照、
# 清单各读一遍，按商品现算会让每件商品打好几遍库，而看板是一次打开全算的。
ItemArrears = namedtuple('ItemArrears', [
    'item', 'arrears_days', 'level', 'level_label', 'tone',
    'refresh_count', 'can_refresh', 'refresh_block_reason',
    'view_count', 'favorite_count', 'engaged_order_count',
])

# 欠账商品清单的一行。`category_label` 在模块里就对成名字，模板不需要知道 CHOICES。
DriftRow = namedtuple('DriftRow', [
    'item', 'category_label', 'arrears_days', 'refresh_count', 'level', 'level_label',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _bucket_index(count, buckets):
    """分档下标。桶是 (上界, 标签) 的元组，左闭右开。"""
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or count < limit:
            return index
    return len(buckets) - 1


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _fetch_items(*, now):
    """取出当前在售、带着未来截止时间、且已沉底的未刷新商品。

    刻意不带任何周期过滤。新鲜度是当前状态不是周期量，按 `created_at` 切会漏掉
    上学期发布、至今仍沉底的那批——而它们正是本模块唯一要看的那批。

    沉底判据在这里写成 `created_at` 而不是 `freshness_age_days`，是因为判据里已经
    限定了 `refresh_count=0`：一次都没刷新过的商品 `last_refreshed_at` 必为空，
    `freshness_age_days` 就退化成 `now - created_at`，两者等价。放进取库里算是为了
    让数据库用索引扛掉大部分行，而不是把全站在售商品拉进 Python 再逐件比。
    """
    return list(
        Item.objects.select_related('category', 'location', 'seller').filter(
            status=ON_SALE_STATUS,
            expires_at__isnull=False,
            expires_at__gt=now,
            refresh_count=0,
            created_at__lte=now - timedelta(days=STALE_DAYS),
        ).order_by('created_at', 'id')
    )


def _collect_signals(item_ids):
    """把互动信号按商品聚合好，一次查完三张表。

    全表扫一遍会在一个成熟站点上变成一次没有索引可用的全表关联，而这里要的只是
    每件商品各自有过几次浏览、收藏和成交。返回三个以商品 id 为键的字典。
    """
    if not item_ids:
        return {}, {}, {}

    views = {
        row['item_id']: row['total'] or 0
        for row in BrowsingHistory.objects.filter(item_id__in=item_ids)
        .values('item_id').annotate(total=Count('id'))
    }
    favorites = {
        row['item_id']: row['total'] or 0
        for row in Favorite.objects.filter(item_id__in=item_ids)
        .values('item_id').annotate(total=Count('id'))
    }
    orders = {
        row['item_id']: row['total'] or 0
        for row in Order.objects.filter(
            item_id__in=item_ids, status__in=ENGAGED_ORDER_STATUSES,
        ).values('item_id').annotate(total=Count('id'))
    }
    return views, favorites, orders


def _build_arrears_stats(rows):
    """在售商品里带着新鲜度欠账的规模，按欠账天数分档。"""
    total = len(rows)
    bucket_counts = [0] * len(ARREARS_BUCKETS)
    for row in rows:
        bucket_counts[_bucket_index(row.arrears_days, ARREARS_BUCKETS)] += 1

    bucket_rows = []
    for index, (limit, label) in enumerate(ARREARS_BUCKETS):
        count = bucket_counts[index]
        bucket_rows.append({
            'label': label,
            'count': count,
            'share': _percent(count, total),
        })

    return {
        'measured_count': total,
        'is_small_sample': total < MIN_MEASURED_ITEMS,
        'bucket_rows': bucket_rows,
    }


def _build_live_denominator(rows):
    """在售商品总数，以及其中带着未来截止时间的件数。

    欠账商品的分母是「在售且带着未来截止时间」的商品，不是全部在售商品。一件没有
    截止时间的商品永远走不到 `expire_items`，也就永远没有重新上架这个动作，把它算进
    分母只会稀释占比。
    """
    return {
        'live_count': Item.objects.filter(status=ON_SALE_STATUS).count(),
        'deadline_live_count': Item.objects.filter(
            status=ON_SALE_STATUS, expires_at__isnull=False, expires_at__gt=timezone.now(),
        ).count(),
        'drifted_count': len(rows),
    }


def _build_level_rows(rows):
    """欠账商品现在处于什么生命周期等级。"""
    counts = dict.fromkeys(ATTENTION_LEVELS, 0)
    for row in rows:
        if row.level in counts:
            counts[row.level] += 1

    total = len(rows)
    rows_out = []
    for level in ATTENTION_LEVELS:
        count = counts[level]
        rows_out.append({
            'level': level,
            'label': LEVEL_LABELS[level],
            'count': count,
            'share': _percent(count, total),
        })
    return {
        'measured_count': total,
        'is_small_sample': total < MIN_MEASURED_ITEMS,
        'rows': rows_out,
    }


def _build_quota_rows(rows):
    """刷新额度对这批商品是不是空的。

    这是本模块最要紧的一组：`refresh_count == 0` 是判据本身，所以第一档的件数必然
    等于全部。真正要看的是「额度全满、间隔也没占着、一刷就灵」的件数——那才是运营
    现在就能推动的那部分。
    """
    counts = [0] * len(REFRESH_COUNT_BUCKETS)
    ready_count = 0
    for row in rows:
        counts[_bucket_index(row.refresh_count, REFRESH_COUNT_BUCKETS)] += 1
        if row.can_refresh:
            ready_count += 1

    total = len(rows)
    bucket_rows = []
    for index, (limit, label) in enumerate(REFRESH_COUNT_BUCKETS):
        count = counts[index]
        bucket_rows.append({
            'label': label,
            'count': count,
            'share': _percent(count, total),
        })

    return {
        'measured_count': total,
        'is_small_sample': total < MIN_MEASURED_ITEMS,
        'bucket_rows': bucket_rows,
        'ready_count': ready_count,
        'ready_share': _percent(ready_count, total),
        'max_refresh_per_month': MAX_REFRESH_PER_MONTH,
        'min_refresh_gap_hours': MIN_REFRESH_GAP_HOURS,
    }


def _build_engagement_rows(rows):
    """欠账商品里有多少件已经彻底没有互动。

    无互动指浏览、收藏、成交订单三者皆零。这部分连刷新都救不回来——刷回去也只是
    让一件没人看的商品再沉一次，需要的是重新拍照或降价。单独数出来，免得运营把
    「一刷就灵」的结论套到全部欠账商品上。
    """
    total = len(rows)
    engaged_count = 0
    for row in rows:
        if row.view_count or row.favorite_count or row.engaged_order_count:
            engaged_count += 1
    silent_count = total - engaged_count

    return {
        'measured_count': total,
        'is_small_sample': total < MIN_MEASURED_ITEMS,
        'engaged_count': engaged_count,
        'engaged_share': _percent(engaged_count, total),
        'silent_count': silent_count,
        'silent_share': _percent(silent_count, total),
    }


def _build_drift_list(rows):
    """欠账商品清单。"""
    ordered = sorted(rows, key=lambda row: (-row.arrears_days, row.item.id))
    shown = ordered[:DRIFT_LIST_LIMIT]
    return {
        'rows': [
            DriftRow(
                item=row.item,
                category_label=row.item.category.name if row.item.category else '未分类',
                arrears_days=row.arrears_days,
                refresh_count=row.refresh_count,
                level=row.level,
                level_label=row.level_label,
            )
            for row in shown
        ],
        'total_count': len(ordered),
        'truncated': len(ordered) > len(shown),
        'limit': DRIFT_LIST_LIMIT,
    }


def build_relist_freshness(days=30, now=None):
    """构建「重新上架的那件商品，为什么一回来就沉底」看板数据。

    `days` 只影响周期量的说明文字，不参与筛选：新鲜度是当前状态，按周期切会漏掉
    上学期发布、至今仍沉底的那批商品。
    """
    now = now or timezone.now()
    items = _fetch_items(now=now)
    views, favorites, orders = _collect_signals([item.id for item in items])

    rows = []
    for item in items:
        level, label, tone, _reasons, _actions = _level_for(item, now=now)
        can_refresh, block_reason = _refresh_state(item, now=now)
        # 信号在构造行时就地取定。挂到 item 上再读是个陷阱：`Item` 是模型实例，
        # 给它塞一个 `view_count` 属性下次 `refresh_from_db` 就丢了，而本模块
        # 在同一个请求里会把同一批行读五遍。
        rows.append(ItemArrears(
            item=item,
            arrears_days=item.freshness_age_days,
            level=level,
            level_label=label,
            tone=tone,
            refresh_count=item.refresh_count,
            can_refresh=can_refresh,
            refresh_block_reason=block_reason,
            view_count=views.get(item.id, 0),
            favorite_count=favorites.get(item.id, 0),
            engaged_order_count=orders.get(item.id, 0),
        ))

    denominator = _build_live_denominator(rows)
    drifted_share = _percent(denominator['drifted_count'], denominator['deadline_live_count'])

    return {
        'period_days': days,
        'period_choices': PERIOD_CHOICES,
        'now': now,
        'has_data': bool(rows),
        'summary': (
            f"在售且带着未来截止时间的商品共 {denominator['deadline_live_count']} 件，"
            f"其中 {denominator['drifted_count']} 件从没刷新过且已沉底。"
            if denominator['deadline_live_count']
            else '当前没有在售且带着未来截止时间的商品。'
        ),
        'stale_days': STALE_DAYS,
        'max_refresh_per_month': MAX_REFRESH_PER_MONTH,
        'min_measured_items': MIN_MEASURED_ITEMS,
        'denominator': denominator,
        'drifted_share': drifted_share,
        'arrears': _build_arrears_stats(rows),
        'levels': _build_level_rows(rows),
        'quota': _build_quota_rows(rows),
        'engagement': _build_engagement_rows(rows),
        'drift_list': _build_drift_list(rows),
    }