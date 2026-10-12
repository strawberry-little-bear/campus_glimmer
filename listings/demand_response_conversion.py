# -*- coding: utf-8 -*-
"""确认了求购响应之后，交易真的发生了吗。

`DemandResponse` 是站内唯一一条「卖家拿在售商品去接一个学生写的需求」的记录。写入路径
三条，`review_demand_response` 的 `accept` 分支是终点：响应改成 `accepted`、给响应人记一次
贡献、**求购改成 `fulfilled`**、同一条求购下其余 `pending` 的响应全部批量改成 `rejected`
并逐一通知。通知原文写着「请继续进入商品详情完成预约或领取申请」。

**确认之后，没有任何东西把这次确认和一笔订单连起来。**

`Order` 模型里没有一个字段指向 `DemandResponse`，也没有一个字段指向 `DemandPost`。
`create_order` 是从商品详情页发起的普通预约：校验 `is_available_now`、校验没有进行中的订单、
分流免费/借用分支，然后建 `Order`。这条路径上没有任何一处问过「这个买家是不是刚刚确认了
某个求购响应」。从确认到下单，中间全靠用户自己记住、自己摸回商品详情页、自己再走一遍预约。

于是站内同时住着两个关于「这条求购成了没有」的说法，而且它们互相不知道。一边是
`DemandPost.status` 在 `accept` 那一刻就变成 `fulfilled`——此后这条求购从所有「寻找中」的
列表里消失、从需求雷达的候选池里消失；另一边是 `Order` 那边什么都没有发生，交易可能十分钟
后就成了，也可能再也没有发生。**`fulfilled` 这个词现在描述的是「有人点了确认」，不是
「有人买到了」。**

全站唯一一个按 `DemandPost.status` 做的漏斗（`analytics.py` 的
`_build_demand_match_insights`）把响应数、处理数、确认响应数一级一级报下来，到「确认」这
一级就是终点了。运营看到的是一个确认率，看不到确认之后有多少真的变成了订单。确认率高而
成交低，说明流程断在中段；这两个数现在分居两处，谁也说不清。

已经存在的两个统计都只摸到这个缺口的一侧，都没有跨过去。`demand_match_quality.py` 回答
「平台自己打的匹配分准不准」，拿 `match_score` 和求购发布者的接受/拒绝做对照，它不关心
接受之后发生了什么；`analytics.py` 的互助反馈统计回答「确认之后发布者说这次算不算完成」，
但它把求购响应和失物招领线索并成一个总数报，而且只有提交了反馈的那些才进得来——**没有
提交反馈的那一批，恰恰是最可能断在中段的那一批。**

这个模块补四件事：确认之后有多少真的变成了订单；从确认到下单隔了多久；变成了订单的那些
最后成交了吗；确认之后没有订单的，卡在哪。

它只报告。不自动建订单、不改 `accept` 之后的任何通知文案、不把 `fulfilled` 的写入时机
推到订单完成之后、不按用户做任何聚合或排名、不做「疑似放鸽子」检测。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import DemandPost, DemandResponse, Item, Order

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 已确认响应少于这么多条时，转化率只报数字不给方向。十二条上一个转化是八个百分点，
# 和抛硬币差不多。
MIN_MEASURED_RESPONSES = 12

# 两个维度相差小于这个百分点视为噪声。和其余模块同一个量级。
NOISE_POINTS = 8

# 已确认的响应状态。`withdrawn` 不在其中：撤回是响应人自己撤的，它没有进入过发布者的
# 决策，既不是转化也不是流失，把它算进任何一边都会让分母说一件没发生的事。
CONFIRMED_STATUS = 'accepted'
WITHDRAWN_STATUS = 'withdrawn'

# 订单状态：`completed` 是交易完成，`returned` 是借用归还。借用订单走到 `returned`
# 是一次完整的闭环，和 `completed` 等价。`cancelled` 单独一类——它是一次真的失败，
# 而且失败在确认之后，比「没有订单」更值得被看见。
DEALT_STATUSES = ('completed', 'returned')
CANCELLED_STATUS = 'cancelled'

# 从确认到下单的分档。按小时起步，不按分钟也不按天起步：确认之后那段路是用户自己摸
# 回去的，分钟级的抖动没有运营含义；而按天起步会把「当天」这档撑得很大，这档恰恰是
# 最该被单独看见的。上限为 None 的那一档兜底。
CONVERSION_LATENCY_BUCKETS = (
    (3600, '1 小时内', '确认完当场就走回去下单了'),
    (24 * 3600, '1-24 小时', '当天内，还记得要点进商品详情'),
    (7 * 86400, '1-7 天', '隔了几天，多半是被催了才想起来'),
    (None, '超过 7 天', '隔了一周以上，中间很可能被别的事打断过'),
)

# 确认之后没有订单的响应，卡在哪三类。顺序即判定优先级，命中即停。
BLOCKED_LABELS = {
    'item_gone': '响应商品已经不可交易',
    'demand_closed': '求购那边已经不重要了',
    'still_walking': '还在路上',
}

# 求购的哪些状态算「那边已经不重要了」。只收 `closed` / `expired`——发布者或系统主动
# 结束的。`fulfilled` 不在这里：它是 `accept` 当场写的，人人如此，把它算进来会让每一条
# 未成交的响应都落进「求购那边已经不重要了」，而真正要运营看一眼的「还在路上」就一条
# 都数不到了——那一栏报的恰恰是商品在、求购在、两边都没动的那批。
DEMAND_DONE_STATUSES = ('closed', 'expired')

# 清单上限。清单是给「现在去处理」用的，不是给归档用的。
STUCK_LIST_LIMIT = 20

# 响应说明的长度分档。固定四档，和其余模块同一个思路。
MESSAGE_LENGTH_BUCKETS = (
    (20, '20 字以内'),
    (60, '21-60 字'),
    (150, '61-150 字'),
    (None, '150 字以上'),
)

TRADE_MODE_LABELS = dict(Item.TRADE_MODE_CHOICES)

# 一条已确认的响应，加上它转化出来的订单、卡在哪。一次查完固定下来：转化率、时延分布、
# 成交拆分、卡点清单各读一遍，按响应现算会让每条响应打好几遍库，而看板是一次打开全算的。
ConfirmedResponse = namedtuple('ConfirmedResponse', [
    'response', 'order', 'blocked_reason', 'latency_seconds',
])

# 卡点清单的一行。`blocked_label` 是预先对好的名字，模板不需要知道 `BLOCKED_LABELS` 这个字典。
StuckRow = namedtuple('StuckRow', ['response', 'blocked_reason', 'blocked_label'])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_span(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 3600:
        return f'{max(seconds // 60, 0)} 分钟'
    if seconds < 86400:
        return f'{seconds // 3600} 小时'
    return f'{seconds // 86400} 天'


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _bucket_index(seconds, buckets):
    """分档下标。桶是 (上限, 标签[, 说明]) 的元组，互斥、左闭右开。"""
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _order_is_from_response(order, response):
    """一笔订单算不算这条响应带来的。

    `Order` 里没有一个字段指向 `DemandResponse`，只能用字段比对回推，三条同时满足才算：
    商品相同、买家是响应人、下单不早于确认时刻。

    商品相同是硬条件。一件商品只能有一条 `accepted` 响应吗？不能，
    `UniqueConstraint` 只挡 `(demand, item)`——同一个人可以拿同一件商品响应两条求购，
    两条都被确认（不同发布者），于是一件商品上可能挂着两条已确认响应。这时「同一件商品
    + 同一个买家」会同时命中两条响应。

    **所以按响应逐条判定，不按订单归属**：一条响应只要能找到满足条件的订单就算转化，
    一条订单可以同时算作两条响应的转化。这个口径会让「转化响应数」和「有订单的响应数」
    在极少数重叠场景下不一致，但宁可重复计数也不假装能分清——把两笔说不清的成交硬派给
    其中一条，会让另一条的转化率凭空变成零，而它是真的转化了。
    """
    if order is None:
        return False
    return (
        order.item_id == response.item_id
        and order.buyer_id == response.responder_id
        and order.created_at >= response.updated_at
    )


def _blocked_reason(response):
    """一条确认之后没有订单的响应，卡在哪。顺序即优先级，命中即停。

    第一条是唯一一处要读业务字段做判断的地方，也是三类里最干净的一类：响应商品已经不可
    交易，说明卖家侧有动作，而且动作可查。第二条只报规模不给方向——一条关闭了的求购对应
    的未成交响应，可能是发布者自己找到了，也可能是他忘了回来关，平台分不清。
    `fulfilled` 故意不在第二条里：它是 `accept` 当场写的，人人如此，算进来会让每一条
    未成交的响应都变成「求购那边已经不重要了」，而真正要运营看一眼的第三类就一条都
    数不到。
    """
    item = response.item
    if item is None or not item.is_available_now:
        return 'item_gone'
    demand = response.demand
    if demand is not None and demand.status in DEMAND_DONE_STATUSES:
        return 'demand_closed'
    return 'still_walking'


def _fetch_confirmed_responses(days, now):
    """按显式排序取出周期内确认的响应，连订单一起取回来。

    统计窗口按 `DemandResponse.updated_at` 切，不按 `created_at` 切：一条窗口前提交、
    窗口内才被确认的响应，它的转化发生在窗口内，按 `created_at` 切会把它整条丢出去。
    `accept` 分支 `save(update_fields=[..., 'updated_at'])` 带的 `auto_now`，所以
    `updated_at` 就是确认时刻，不需要另建字段。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    """
    start = _period_start(days, timezone.localtime(now).date())
    responses = list(
        DemandResponse.objects.select_related(
            'demand', 'demand__requester', 'item', 'item__category',
            'item__location', 'item__seller', 'responder',
        ).filter(
            status=CONFIRMED_STATUS,
            updated_at__gte=start,
            updated_at__lte=now,
        ).order_by('updated_at', 'id')
    )
    if not responses:
        return []

    # 候选订单只取这些响应涉及的商品、由这些响应人买下的那一些，再逐条比对。全表扫一遍
    # 会在一个成熟站点上变成一次没有索引可用的全表关联，而这里要的只是三条字段相等。
    candidate_orders = list(
        Order.objects.select_related('item', 'buyer', 'seller').filter(
            item_id__in={response.item_id for response in responses},
            buyer_id__in={response.responder_id for response in responses},
        ).order_by('created_at', 'id')
    )

    confirmed = []
    for response in responses:
        order = None
        for candidate in candidate_orders:
            if _order_is_from_response(candidate, response):
                order = candidate
                break
        latency = None
        if order is not None:
            latency = (order.created_at - response.updated_at).total_seconds()
        confirmed.append(ConfirmedResponse(
            response=response,
            order=order,
            blocked_reason=None if order is not None else _blocked_reason(response),
            latency_seconds=latency,
        ))
    return confirmed


def _build_conversion_stats(confirmed):
    """确认之后有多少真的变成了订单。分母是已确认的响应数。"""
    total = len(confirmed)
    with_order = [row for row in confirmed if row.order is not None]
    converted = len(with_order)
    return {
        'total': total,
        'converted_count': converted,
        'conversion_rate': _percent(converted, total),
        'dealt_count': sum(
            1 for row in with_order if row.order.status in DEALT_STATUSES
        ),
        'cancelled_count': sum(
            1 for row in with_order if row.order.status == CANCELLED_STATUS
        ),
        'still_open_count': sum(
            1 for row in with_order if row.order.status not in DEALT_STATUSES
            and row.order.status != CANCELLED_STATUS
        ),
        # 一条求购只会确认一条，所以按求购去重之后这两个数在正常情况下相等。不相等就说明
        # 有并发确认，那本身就是要看见的信号，单独数出来。
        'demand_count': len({row.response.demand_id for row in confirmed}),
        'demand_with_order_count': len({
            row.response.demand_id for row in with_order
        }),
        'is_small_sample': total < MIN_MEASURED_RESPONSES,
        'min_measured_responses': MIN_MEASURED_RESPONSES,
    }


def _build_latency_stats(confirmed):
    """从确认到下单隔了多久。只报有订单的那些。

    没有订单的响应谈不上「隔了多久」，它是另一张表。中位数之外报 P90 和平均：平均数
    在长尾上会被一条隔了三周的订单带偏，运营要看的是「绝大多数人多久走完那段路」。
    """
    spans = [
        row.latency_seconds for row in confirmed
        if row.order is not None and row.latency_seconds is not None
    ]
    buckets = []
    for index, bucket in enumerate(CONVERSION_LATENCY_BUCKETS):
        buckets.append({
            'key': index,
            'label': bucket[1],
            'note': bucket[2] if len(bucket) > 2 else '',
            'count': 0,
            'share': 0,
        })
    for span in spans:
        buckets[_bucket_index(span, CONVERSION_LATENCY_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(spans))
    return {
        'total': len(spans),
        'median_label': _format_span(_percentile(spans, 0.5)),
        'p90_label': _format_span(_percentile(spans, 0.9)),
        'average_label': _format_span(_mean(spans)),
        'same_day_count': sum(1 for span in spans if span < 86400),
        'same_day_rate': _percent(sum(1 for span in spans if span < 86400), len(spans)),
        'bucket_rows': buckets,
    }


def _build_dealt_stats(conversion):
    """变成了订单的那些，最后成交了吗。

    取消单独一类，不算成交也不算未成交之外的事——它是一次真的失败，而且失败在确认之后，
    比「没有订单」更值得被看见。只报规模不猜原因：取消可能是卖家反悔、可能是买家反悔、
    可能是两个人谈崩了。订单事件时间线里有 `actor`，但一次取消的 `actor` 是操作按钮的
    那个人，不等于「谁造成的」，这个判断平台做不了。
    """
    total = conversion['converted_count']
    return {
        'total': total,
        'dealt_count': conversion['dealt_count'],
        'dealt_rate': _percent(conversion['dealt_count'], total),
        'cancelled_count': conversion['cancelled_count'],
        'cancelled_rate': _percent(conversion['cancelled_count'], total),
        'still_open_count': conversion['still_open_count'],
        'still_open_rate': _percent(conversion['still_open_count'], total),
        'is_small_sample': total < MIN_MEASURED_RESPONSES,
        'min_measured_responses': MIN_MEASURED_RESPONSES,
    }


def _build_blocked_stats(confirmed):
    """确认之后没有订单的，卡在哪。"""
    stuck = [row for row in confirmed if row.order is None]
    rows = []
    for key, label in BLOCKED_LABELS.items():
        group = [row for row in stuck if row.blocked_reason == key]
        rows.append({
            'key': key,
            'label': label,
            'count': len(group),
            'share': _percent(len(group), len(stuck)),
        })
    return {
        'total': len(stuck),
        'rows': rows,
        'item_gone_count': sum(
            1 for row in stuck if row.blocked_reason == 'item_gone'
        ),
        'still_walking_count': sum(
            1 for row in stuck if row.blocked_reason == 'still_walking'
        ),
        # 「还在路上」这一类是真正要运营看一眼的：商品在、求购在、两边都没动，
        # 那次确认就这样悬着。
        'still_walking_rate': _percent(
            sum(1 for row in stuck if row.blocked_reason == 'still_walking'),
            len(stuck),
        ),
    }


def _build_trade_mode_stats(confirmed):
    """转化率按交易方式拆开，不按总数报。

    出售要看钱，免费和借用都不涉及付款，走完那段路的动力完全不同。合并会把两种体验
    平均成一个没有解释力的数。
    """
    rows = []
    for key, label in Item.TRADE_MODE_CHOICES:
        group = [
            row for row in confirmed
            if row.response.item is not None and row.response.item.trade_mode == key
        ]
        converted = sum(1 for row in group if row.order is not None)
        rows.append({
            'key': key,
            'label': label,
            'total': len(group),
            'converted_count': converted,
            'conversion_rate': _percent(converted, len(group)),
            'is_small_sample': len(group) < MIN_MEASURED_RESPONSES,
        })
    return {'rows': rows}


def _build_message_stats(confirmed):
    """响应说明只报「写了没写」和长度，一个字符都不读。

    `message` 是自由文本，可能写着电话、微信、宿舍号。不切词、不聚类、不读内容。
    「写了 vs 没写」和转化只做对照，不给因果：一个愿意写清楚「我这个还有发票和盒子」的
    卖家可能本来就更认真，也更可能把后续那步走完，平台没有能力把「说明起了作用」和
    「这个人本来就靠谱」分开。两组各至少 12 条才给方向，差值不满 8 个百分点视为噪声。
    """
    buckets = []
    for index, bucket in enumerate(MESSAGE_LENGTH_BUCKETS):
        buckets.append({
            'key': index,
            'label': bucket[1],
            'count': 0,
            'share': 0,
        })
    filled = 0
    lengths = []
    for row in confirmed:
        text = (row.response.message or '').strip()
        length = len(text)
        lengths.append(length)
        if length:
            filled += 1
            buckets[_bucket_index(length, MESSAGE_LENGTH_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], filled)
    total = len(confirmed)
    with_message = [row for row in confirmed if (row.response.message or '').strip()]
    without_message = [row for row in confirmed if not (row.response.message or '').strip()]
    with_rate = _percent(
        sum(1 for row in with_message if row.order is not None), len(with_message),
    )
    without_rate = _percent(
        sum(1 for row in without_message if row.order is not None), len(without_message),
    )
    both_big_enough = (
        len(with_message) >= MIN_MEASURED_RESPONSES
        and len(without_message) >= MIN_MEASURED_RESPONSES
    )
    gap = round(with_rate - without_rate, 1)
    return {
        'total': total,
        'filled_count': filled,
        'fill_rate': _percent(filled, total),
        'average_length': round(_mean(lengths), 1) if lengths else 0,
        'length_rows': buckets,
        'with_message_count': len(with_message),
        'without_message_count': len(without_message),
        'with_message_rate': with_rate,
        'without_message_rate': without_rate,
        'gap_points': gap,
        'gap_is_meaningful': both_big_enough and abs(gap) >= NOISE_POINTS,
        'is_measurable': both_big_enough,
    }


def _build_stuck_rows(confirmed):
    """卡点清单。只带商品标题、求购标题、确认时刻和卡点类别，不带任何用户名。"""
    stuck = [row for row in confirmed if row.order is None]
    stuck.sort(key=lambda row: row.response.updated_at, reverse=True)
    rows = []
    for row in stuck[:STUCK_LIST_LIMIT]:
        # 卡点标签在模块里就对成名字，不往模板里推。模板要做的只是把它显示出来。
        rows.append(StuckRow(
            response=row.response,
            blocked_reason=row.blocked_reason,
            blocked_label=BLOCKED_LABELS.get(row.blocked_reason, row.blocked_reason),
        ))
    return {
        'rows': rows,
        'row_total': len(stuck),
        'row_limit': STUCK_LIST_LIMIT,
        'still_walking_row_total': len([
            row for row in stuck if row.blocked_reason == 'still_walking'
        ]),
    }


def _summary(conversion, latency, dealt, blocked):
    if not conversion['total']:
        return '当前周期没有确认过的求购响应，转化也就无从谈起。'
    parts = [
        f"周期内确认了 {conversion['total']} 条求购响应，"
        f"{conversion['converted_count']} 条之后真的变成了订单"
        f"（{conversion['conversion_rate']}%）。"
    ]
    if conversion['is_small_sample']:
        parts.append(
            f"样本不足 {conversion['min_measured_responses']} 条，"
            f"这个比例只作参考。"
        )
    if conversion['converted_count']:
        parts.append(
            f"这些订单里 {dealt['dealt_count']} 条已经成交"
            f"（{dealt['dealt_rate']}%），{dealt['cancelled_count']} 条被取消，"
            f"{dealt['still_open_count']} 条还在进行中。"
        )
    if latency['total']:
        parts.append(
            f"从确认到下单，中位 {latency['median_label']}、"
            f"P90 {latency['p90_label']}，"
            f"{latency['same_day_count']} 条在当天内走完"
            f"（{latency['same_day_rate']}%）。"
        )
    if blocked['total']:
        parts.append(
            f"确认之后没有订单的 {blocked['total']} 条里，"
            f"{blocked['still_walking_count']} 条商品和求购都还在，"
            f"那次确认就这样悬着。"
        )
    return ''.join(parts)


def build_demand_response_conversion(days=30, now=None):
    """统计已确认求购响应的后续成交情况，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    confirmed = _fetch_confirmed_responses(days, now)
    conversion = _build_conversion_stats(confirmed)
    latency = _build_latency_stats(confirmed)
    dealt = _build_dealt_stats(conversion)
    blocked = _build_blocked_stats(confirmed)
    trade_modes = _build_trade_mode_stats(confirmed)
    message = _build_message_stats(confirmed)
    stuck_rows = _build_stuck_rows(confirmed)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(confirmed),
        'summary': _summary(conversion, latency, dealt, blocked),
        'conversion': conversion,
        'latency': latency,
        'dealt': dealt,
        'blocked': blocked,
        'trade_modes': trade_modes,
        'message': message,
        'stuck_rows': stuck_rows,
        'blocked_labels': BLOCKED_LABELS,
        'trade_mode_labels': TRADE_MODE_LABELS,
        'dealt_statuses': DEALT_STATUSES,
    }
