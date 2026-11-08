# -*- coding: utf-8 -*-
"""统计求购响应被确认之后，是否真的接上了交易。

`DemandResponse` 记录卖家拿在售商品响应求购。发布者确认时写入 `accepted_at`、把求购改成
`fulfilled`，并把其他待处理响应改成临时状态 `deferred`。如果确认商品在生成订单或领取申请前
失效，发布者可以显式重新选择：旧确认进入 `superseded`，求购回到 `active`，仍可用的 `deferred`
响应恢复为 `pending`。已有交易记录或求购已截止时不开放重新选择。

本模块只读四件事：窗口内每次确认之后有多少真的创建了订单；从确认到创建订单隔了多久；订单最终
是否成交；没有订单的确认当时卡在商品、求购还是正常推进中。历史确认按 `accepted_at` 留在分母，
`superseded` 不会因重新选择从统计消失。模块不自动建单、不改状态、不替免费商品挑选申请人，也不做
个人追踪或「疑似放鸽子」判断。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import (
    DemandPost, DemandReopenEvent, DemandResponse, GiftApplication, Item, Order,
)

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

# 曾经被确认的响应都要留在分母里。`superseded` 是发布者重新选择后留下的历史确认，
# 不能因为状态不再是 `accepted` 就从转化统计里消失。`withdrawn` 不在其中：撤回是
# 响应人自己撤的，它没有进入过发布者的决策。
CONFIRMED_STATUSES = ('accepted', 'superseded')
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

# 确认之后没有订单的响应，走到了哪一步。领取申请是显式业务事实，优先于后来由商品或
# 求购状态推断出的卡点；只有没有领取申请时，才继续判断商品、求购和尚未发起交易。
BLOCKED_LABELS = {
    'gift_pending': '已申请领取，等待卖家选择',
    'gift_rejected': '领取申请未选中',
    'gift_withdrawn': '领取申请已撤回',
    'gift_selected_without_order': '领取申请已选中，待补交易记录',
    'gift_unknown': '领取申请状态不在已知范围',
    'item_gone': '响应商品已经不可交易',
    'demand_closed': '求购那边已经不重要了',
    'still_walking': '商品和求购仍可推进，尚未发起交易',
}

GIFT_APPLICATION_STAGE_REASONS = {
    'pending': 'gift_pending',
    'rejected': 'gift_rejected',
    'withdrawn': 'gift_withdrawn',
    'selected': 'gift_selected_without_order',
}

# 求购的哪些状态算「那边已经不重要了」。只收 `closed` / `expired`——发布者或系统主动
# 结束的。`fulfilled` 不在这里：它是 `accept` 当场写的，人人如此，把它算进来会让每一条
# 未成交的响应都落进「求购那边已经不重要了」，而真正要运营看一眼的「尚未发起交易」就一条
# 都数不到了。
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


REOPEN_COUNT_BUCKETS = (
    ('1', '第 1 次重新选择'),
    ('2', '第 2 次重新选择'),
    ('3plus', '第 3 次及以后'),
)

# 重开后再次确认的样本，先看是否接出了订单；没有订单后才进入领取阶段和
# 商品/求购状态推断。订单优先，避免一份同时关联领取申请的订单被重复分类。
REOPEN_NO_ORDER_STAGES = (
    ('gift_pending', '等待卖家选择领取申请'),
    ('gift_rejected', '领取申请未选中'),
    ('gift_withdrawn', '领取申请已撤回'),
    ('gift_selected_without_order', '领取已选中但缺订单'),
    ('gift_unknown', '领取申请状态未知'),
    ('item_gone', '再次确认的商品已不可交易'),
    ('demand_closed', '求购已关闭或过期'),
    ('still_walking', '商品与求购仍可推进，尚未发起交易'),
)

TRADE_MODE_LABELS = dict(Item.TRADE_MODE_CHOICES)

# 一条已确认的响应，加上它接出的订单、领取申请和当前阶段。一次查完固定下来：
# 转化率、时延分布、成交拆分和阶段清单各读一遍，按响应现算会让每条响应打好几遍库，
# 而看板是一次打开全算的。
ConfirmedResponse = namedtuple('ConfirmedResponse', [
    'response', 'order', 'gift_application', 'blocked_reason', 'latency_seconds',
])

# 阶段清单的一行。`blocked_label` 是预先对好的名字，模板不需要知道 `BLOCKED_LABELS`。
StuckRow = namedtuple('StuckRow', ['response', 'blocked_reason', 'blocked_label'])

ReopenSample = namedtuple('ReopenSample', [
    'event', 'confirmation', 'reopen_latency_seconds', 'transaction_latency_seconds',
])


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


def _confirmed_at(response):
    """确认时刻。迁移前没有 accepted_at 的旧记录回退到 updated_at。"""
    return response.accepted_at or response.updated_at


def _order_is_from_response(order, response):
    """一笔订单算不算这条响应带来的。

    新订单直接保存 `demand_response`，以这条显式来源为准：即使同一件商品、同一个求购
    发布者后来确认了多条响应，订单也不会被错归给旁边的响应。

    迁移前没有来源字段的旧订单才回退到三条推断：商品相同、买家是求购发布者、下单不早于
    确认时刻。响应人是商品卖家，不能拿 `responder` 当买家。
    """
    if order is None:
        return False
    if order.demand_response_id is not None:
        return order.demand_response_id == response.pk
    return (
        order.item_id == response.item_id
        and order.buyer_id == response.demand.requester_id
        and order.created_at >= _confirmed_at(response)
    )


def _legacy_gift_application_is_compatible(application, response):
    """迁移前的领取申请没有来源时，能否归到这条确认。

    新记录只认 `GiftApplication.demand_response`。旧记录必须同时满足商品相同、申请人是
    求购发布者、申请不早于确认；同一件商品有多条确认时只选离申请最近的一条，并由显式
    来源优先覆盖，避免一份申请被重复算进多条响应。
    """
    return (
        application.demand_response_id is None
        and application.item_id == response.item_id
        and application.applicant_id == response.demand.requester_id
        and application.created_at >= _confirmed_at(response)
    )


def _map_gift_applications(responses, applications):
    """把领取申请一次性归到确认响应，返回 `{response_id: application}`。"""
    response_by_id = {response.pk: response for response in responses}
    mapped = {}
    explicit_ids = set()
    for application in applications:
        response_id = application.demand_response_id
        if response_id in response_by_id:
            mapped[response_id] = application
            explicit_ids.add(response_id)

    # 旧记录只做兼容推断。每条申请先找离申请最近的一条兼容确认，再把多份历史申请
    # 收敛成每条响应一份；显式来源永远不会被兼容记录覆盖。
    legacy = sorted(
        (application for application in applications
         if application.demand_response_id is None),
        key=lambda application: (application.created_at, application.pk),
    )
    for application in legacy:
        candidates = [
            response for response in responses
            if _legacy_gift_application_is_compatible(application, response)
        ]
        if not candidates:
            continue
        target = max(candidates, key=lambda response: (_confirmed_at(response), response.pk))
        current = mapped.get(target.pk)
        if target.pk in explicit_ids:
            continue
        if current is None or (
            application.created_at,
            application.pk,
        ) > (
            current.created_at,
            current.pk,
        ):
            mapped[target.pk] = application
    return mapped


def _blocked_reason(response, gift_application=None):
    """一条确认之后没有订单的响应，走到了哪一步。顺序即优先级，命中即停。

    领取申请的状态比商品和求购的后续状态更具体：等待卖家选择、未选中、撤回和已选中
    待补订单，都说明发布者已经发起过这一步，不能再把它们统称为「还在路上」。只有完全
    没有领取申请时，才用商品、求购和仍可推进这三个推断类别兜底。
    """
    if gift_application is not None:
        return GIFT_APPLICATION_STAGE_REASONS.get(
            gift_application.status, 'gift_unknown',
        )
    item = response.item
    if item is None or not item.is_available_now:
        return 'item_gone'
    demand = response.demand
    if demand is not None and demand.status in DEMAND_DONE_STATUSES:
        return 'demand_closed'
    return 'still_walking'


def _fetch_confirmed_responses(days, now):
    """按显式排序取出周期内确认的响应，连订单和领取申请一起取回来。

    统计窗口按显式记录的 `accepted_at` 切，不按 `created_at` 切：一条窗口前提交、窗口内
    才被确认的响应，它的转化发生在窗口内。重开后的历史响应会从 `accepted` 变成
    `superseded`，但仍保留 `accepted_at`，因此既不会从历史分母消失，也不会被重开时间
    误算成一次新确认。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    """
    start = _period_start(days, timezone.localtime(now).date())
    responses = list(
        DemandResponse.objects.select_related(
            'demand', 'demand__requester', 'item', 'item__category',
            'item__location', 'item__seller', 'responder',
        ).filter(
            status__in=CONFIRMED_STATUSES,
            accepted_at__gte=start,
            accepted_at__lte=now,
        ).order_by('accepted_at', 'id')
    )
    if not responses:
        return []

    response_ids = {response.pk for response in responses}
    item_ids = {response.item_id for response in responses}
    requester_ids = {response.demand.requester_id for response in responses}

    # 新订单按显式来源取；迁移前的旧订单才用商品和求购发布者推断。领取申请那一侧
    # 也有显式来源和旧记录兼容推断，三种查询都按响应集合收窄。
    candidate_orders = list(
        Order.objects.select_related('item', 'buyer', 'seller').filter(
            Q(demand_response_id__in=response_ids)
            | Q(gift_application__demand_response_id__in=response_ids)
            | (
                Q(demand_response__isnull=True)
                & Q(item_id__in=item_ids, buyer_id__in=requester_ids)
            )
        ).order_by('created_at', 'id')
    )
    candidate_applications = list(
        GiftApplication.objects.select_related(
            'item', 'applicant', 'order', 'demand_response',
        ).filter(
            Q(demand_response_id__in=response_ids)
            | (
                Q(demand_response__isnull=True)
                & Q(item_id__in=item_ids, applicant_id__in=requester_ids)
            )
        ).order_by('created_at', 'id')
    )
    applications_by_response = _map_gift_applications(
        responses, candidate_applications,
    )
    explicit_orders = {
        order.demand_response_id: order
        for order in candidate_orders
        if order.demand_response_id in response_ids
    }

    confirmed = []
    for response in responses:
        order = explicit_orders.get(response.pk)
        if order is None:
            for candidate in candidate_orders:
                if _order_is_from_response(candidate, response):
                    order = candidate
                    break
        application = applications_by_response.get(response.pk)
        if order is None and application is not None and application.order_id:
            if _order_is_from_response(application.order, response):
                order = application.order
        latency = None
        if order is not None:
            latency = (order.created_at - _confirmed_at(response)).total_seconds()
        confirmed.append(ConfirmedResponse(
            response=response,
            order=order,
            gift_application=application,
            blocked_reason=(
                None if order is not None else _blocked_reason(response, application)
            ),
            latency_seconds=latency,
        ))
    return confirmed


def _transaction_at(confirmation):
    """一次再次确认之后最早的交易入口时间。订单和领取申请都算发起交易。"""
    timestamps = []
    if confirmation.order is not None:
        timestamps.append(confirmation.order.created_at)
    if confirmation.gift_application is not None:
        timestamps.append(confirmation.gift_application.created_at)
    return min(timestamps) if timestamps else None


def _fetch_reopen_events(days, now):
    """按重新选择事件时间切窗，历史兼容回填单独计数。"""
    start = _period_start(days, timezone.localtime(now).date())
    events = list(
        DemandReopenEvent.objects.filter(
            source='runtime', reopened_at__gte=start, reopened_at__lte=now,
        ).order_by('reopened_at', 'id')
    )
    backfill_count = DemandReopenEvent.objects.filter(
        source='backfill', reopened_at__gte=start, reopened_at__lte=now,
    ).count()
    return events, backfill_count


def _build_reopen_samples(events, confirmed):
    """为一个重开事件匹配同一求购在下一事件前的首次有效确认。

    `confirmed` 已经按 `accepted_at` 排序，且只含窗口内的确认。事件按发生
    时间顺序处理；同一求购若在窗口内又重开一次，后一次事件就是前一次观察的
    上界，避免把后来才发生的确认倒挂到更早的事件上。
    """
    rows_by_demand = {}
    for row in confirmed:
        rows_by_demand.setdefault(row.response.demand_id, []).append(row)
    events_by_demand = {}
    for event in events:
        if event.demand_id is not None:
            events_by_demand.setdefault(event.demand_id, []).append(event)

    next_boundary = {}
    for group in events_by_demand.values():
        for index in range(len(group) - 1):
            next_boundary[group[index].pk] = group[index + 1].reopened_at

    samples = []
    for event in events:
        confirmation = None
        boundary = next_boundary.get(event.pk)
        for row in rows_by_demand.get(event.demand_id, []):
            accepted_at = _confirmed_at(row.response)
            if accepted_at < event.reopened_at:
                continue
            if boundary is not None and accepted_at >= boundary:
                break
            confirmation = row
            break
        reopen_latency = None
        transaction_latency = None
        if confirmation is not None:
            accepted_at = _confirmed_at(confirmation.response)
            reopen_latency = (accepted_at - event.reopened_at).total_seconds()
            transaction_at = _transaction_at(confirmation)
            if transaction_at is not None:
                transaction_latency = (
                    transaction_at - accepted_at
                ).total_seconds()
        samples.append(ReopenSample(
            event=event,
            confirmation=confirmation,
            reopen_latency_seconds=reopen_latency,
            transaction_latency_seconds=transaction_latency,
        ))
    return samples


def _duration_stats(values):
    """重新选择漏斗里的时延摘要，只使用非负的明确时间戳。"""
    valid = [value for value in values if value is not None and value >= 0]
    return {
        'total': len(valid),
        'median_label': _format_span(_percentile(valid, 0.5)),
        'p90_label': _format_span(_percentile(valid, 0.9)),
        'average_label': _format_span(_mean(valid)),
        'same_day_count': sum(1 for value in valid if value < 86400),
        'same_day_rate': _percent(
            sum(1 for value in valid if value < 86400), len(valid),
        ),
    }


def _reopen_bucket(event):
    count = event.reopen_count_after
    if count >= 3:
        return '3plus'
    return str(count) if count in {1, 2} else '1'


def _reopen_group_stats(samples):
    reconfirmed = [sample for sample in samples if sample.confirmation is not None]
    orders = [sample for sample in reconfirmed if sample.confirmation.order is not None]
    gift_only = [
        sample for sample in reconfirmed
        if sample.confirmation.order is None
        and sample.confirmation.gift_application is not None
    ]
    transactions = [
        sample for sample in reconfirmed
        if sample.confirmation.order is not None
        or sample.confirmation.gift_application is not None
    ]
    return {
        'event_count': len(samples),
        'demand_count': len({sample.event.demand_id for sample in samples}),
        'reconfirmed_count': len(reconfirmed),
        'reconfirmation_rate': _percent(len(reconfirmed), len(samples)),
        'order_count': len(orders),
        'order_conversion_rate': _percent(len(orders), len(reconfirmed)),
        'gift_application_count': len(gift_only),
        'transaction_count': len(transactions),
        'transaction_rate': _percent(len(transactions), len(reconfirmed)),
        'no_transaction_count': len(reconfirmed) - len(transactions),
        'is_small_sample': len(samples) < MIN_MEASURED_RESPONSES,
    }


def _build_reopen_funnel(events, confirmed, backfill_count):
    """把每次重新选择做成独立样本，回答再次确认和交易是否真的接上。"""
    samples = _build_reopen_samples(events, confirmed)
    reconfirmed = [sample for sample in samples if sample.confirmation is not None]
    orders = [sample for sample in reconfirmed if sample.confirmation.order is not None]
    orderless = [sample for sample in reconfirmed if sample.confirmation.order is None]
    gift_only = [
        sample for sample in orderless
        if sample.confirmation.gift_application is not None
    ]
    transactions = [
        sample for sample in reconfirmed
        if sample.confirmation.order is not None
        or sample.confirmation.gift_application is not None
    ]

    stage_rows = []
    for key, label in REOPEN_NO_ORDER_STAGES:
        count = sum(
            1 for sample in orderless
            if sample.confirmation.blocked_reason == key
        )
        stage_rows.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _percent(count, len(orderless)),
        })

    order_stage_rows = []
    for key, label, statuses in (
        ('dealt', '已成交', DEALT_STATUSES),
        ('cancelled', '已取消', (CANCELLED_STATUS,)),
        ('open', '仍在进行中', ()),
    ):
        if key == 'open':
            count = sum(
                1 for sample in orders
                if sample.confirmation.order.status not in DEALT_STATUSES
                and sample.confirmation.order.status != CANCELLED_STATUS
            )
        else:
            count = sum(
                1 for sample in orders
                if sample.confirmation.order.status in statuses
            )
        order_stage_rows.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _percent(count, len(orders)),
        })

    count_rows = []
    for key, label in REOPEN_COUNT_BUCKETS:
        group = [sample for sample in samples if _reopen_bucket(sample.event) == key]
        count_rows.append({'key': key, 'label': label, **_reopen_group_stats(group)})

    trade_mode_rows = []
    for key, label in Item.TRADE_MODE_CHOICES:
        group = [
            sample for sample in samples
            if sample.event.previous_trade_mode == key
        ]
        trade_mode_rows.append({
            'key': key, 'label': label, **_reopen_group_stats(group),
        })
    unknown_mode = [
        sample for sample in samples
        if sample.event.previous_trade_mode not in TRADE_MODE_LABELS
    ]
    if unknown_mode:
        trade_mode_rows.append({
            'key': 'unknown', 'label': '交易方式未知',
            **_reopen_group_stats(unknown_mode),
        })

    event_count = len(samples)
    return {
        'event_count': event_count,
        'affected_demand_count': len({sample.event.demand_id for sample in samples}),
        'not_reconfirmed_count': event_count - len(reconfirmed),
        'reconfirmed_count': len(reconfirmed),
        'reconfirmation_rate': _percent(len(reconfirmed), event_count),
        'order_count': len(orders),
        'order_conversion_rate': _percent(len(orders), len(reconfirmed)),
        'gift_application_count': len(gift_only),
        'gift_application_rate': _percent(len(gift_only), len(reconfirmed)),
        'transaction_count': len(transactions),
        'no_transaction_count': len(reconfirmed) - len(transactions),
        'order_stage_rows': order_stage_rows,
        'no_order_stage_rows': stage_rows,
        'reopen_count_rows': count_rows,
        'trade_mode_rows': trade_mode_rows,
        'reopen_to_reconfirmation': _duration_stats([
            sample.reopen_latency_seconds for sample in reconfirmed
        ]),
        'reconfirmation_to_transaction': _duration_stats([
            sample.transaction_latency_seconds for sample in reconfirmed
        ]),
        'backfill_event_count': backfill_count,
        'is_small_sample': event_count < MIN_MEASURED_RESPONSES,
        'min_measured_responses': MIN_MEASURED_RESPONSES,
    }


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
        # 一次求购可能经历过多次显式确认，因此响应数和求购数不能互相替代；两个数都
        # 单列出来，重开几次、覆盖了几个求购都能直接看见。
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


def _build_gift_application_stage_stats(confirmed):
    """没有订单的确认里，免费领取申请走到了哪个阶段。

    领取申请不等于订单转化：它只证明发布者已经从确认响应进入领取队列。等待、未选中、
    撤回和“已选中但订单关系缺失”必须分开展示，尤其等待卖家选择不能被算成用户尚未
    发起交易。`selected` 正常应有订单；这里单列出来是为了让历史缺链或写入异常可见。
    """
    stuck = [row for row in confirmed if row.order is None]
    applications = [
        row.gift_application for row in stuck
        if row.gift_application is not None
    ]
    counts = {}
    for status, reason in GIFT_APPLICATION_STAGE_REASONS.items():
        counts[status] = sum(
            1 for application in applications if application.status == status
        )
    return {
        'total': len(applications),
        'pending_count': counts['pending'],
        'rejected_count': counts['rejected'],
        'withdrawn_count': counts['withdrawn'],
        'selected_without_order_count': counts['selected'],
        'unknown_count': sum(
            1 for row in stuck
            if row.blocked_reason == 'gift_unknown'
        ),
        'awaiting_start_count': sum(
            1 for row in stuck if row.blocked_reason == 'still_walking'
        ),
        'no_application_count': sum(
            1 for row in stuck if row.gift_application is None
        ),
        'application_rate': _percent(len(applications), len(stuck)),
    }


def _build_blocked_stats(confirmed):
    """确认之后没有订单的，走到了哪一步。"""
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
        'demand_closed_count': sum(
            1 for row in stuck if row.blocked_reason == 'demand_closed'
        ),
        # 这个字段名保留给既有调用方。语义已经收窄为“商品和求购仍可推进，但完全
        # 没有领取申请，也没有订单”，不再覆盖等待卖家选择等已经发起过的阶段。
        'still_walking_count': sum(
            1 for row in stuck if row.blocked_reason == 'still_walking'
        ),
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
    """阶段清单。只带商品标题、求购标题、确认时刻和阶段，不带任何用户名。"""
    stuck = [row for row in confirmed if row.order is None]
    stuck.sort(key=lambda row: _confirmed_at(row.response), reverse=True)
    rows = []
    for row in stuck[:STUCK_LIST_LIMIT]:
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
        'gift_application_row_total': len([
            row for row in stuck if row.gift_application is not None
        ]),
    }


def _summary(conversion, latency, dealt, blocked, gift_applications, reopen=None):
    if not conversion['total']:
        if reopen and reopen['event_count']:
            return (
                f"当前周期没有确认过的求购响应，但记录了 {reopen['event_count']} 次重新选择；"
                f"其中 {reopen['reconfirmed_count']} 次随后再次确认，"
                f"{reopen['order_count']} 次接出订单，"
                f"{reopen['gift_application_count']} 次只发起了免费领取申请。"
            )
        if reopen and reopen['backfill_event_count']:
            return (
                f"当前周期没有新的重新选择事件；另有 {reopen['backfill_event_count']} 条"
                f"历史兼容回填，只用于核对，不进入精确漏斗。"
            )
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
        if gift_applications['total']:
            parts.append(
                f"确认之后没有订单的 {blocked['total']} 条里，"
                f"{gift_applications['total']} 条已经发起领取申请："
                f"等待卖家选择 {gift_applications['pending_count']} 条、"
                f"未选中 {gift_applications['rejected_count']} 条、"
                f"撤回 {gift_applications['withdrawn_count']} 条、"
                f"已选中待补订单 {gift_applications['selected_without_order_count']} 条。"
            )
        if gift_applications['awaiting_start_count']:
            parts.append(
                f"另有 {gift_applications['awaiting_start_count']} 条"
                f"商品和求购仍可推进，但尚未发起交易。"
            )
        if token_other := (
            blocked['item_gone_count'] + blocked['demand_closed_count']
        ):
            parts.append(f"其余 {token_other} 条停在商品不可交易或求购已结束。")
    if reopen and reopen['event_count']:
        parts.append(
            f"另外，周期内记录了 {reopen['event_count']} 次重新选择，"
            f"{reopen['reconfirmed_count']} 次随后再次确认"
            f"（{reopen['reconfirmation_rate']}%）；再次确认后 "
            f"{reopen['order_count']} 次接出订单，"
            f"{reopen['gift_application_count']} 次只发起了免费领取申请。"
        )
    return ''.join(parts)


def build_demand_response_conversion(days=30, now=None):
    """统计已确认求购响应的后续交易和领取阶段，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    confirmed = _fetch_confirmed_responses(days, now)
    reopen_events, backfill_count = _fetch_reopen_events(days, now)
    reopen_funnel = _build_reopen_funnel(
        reopen_events, confirmed, backfill_count,
    )
    conversion = _build_conversion_stats(confirmed)
    latency = _build_latency_stats(confirmed)
    dealt = _build_dealt_stats(conversion)
    gift_applications = _build_gift_application_stage_stats(confirmed)
    blocked = _build_blocked_stats(confirmed)
    trade_modes = _build_trade_mode_stats(confirmed)
    message = _build_message_stats(confirmed)
    stuck_rows = _build_stuck_rows(confirmed)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(
            confirmed or reopen_funnel['event_count']
            or reopen_funnel['backfill_event_count']
        ),
        'summary': _summary(
            conversion, latency, dealt, blocked, gift_applications, reopen_funnel,
        ),
        'conversion': conversion,
        'latency': latency,
        'dealt': dealt,
        'gift_applications': gift_applications,
        'blocked': blocked,
        'trade_modes': trade_modes,
        'reopen_funnel': reopen_funnel,
        'message': message,
        'stuck_rows': stuck_rows,
        'blocked_labels': BLOCKED_LABELS,
        'trade_mode_labels': TRADE_MODE_LABELS,
        'dealt_statuses': DEALT_STATUSES,
    }
