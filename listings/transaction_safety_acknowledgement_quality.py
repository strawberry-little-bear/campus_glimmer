# -*- coding: utf-8 -*-
"""交易安全卡确认质量：当前覆盖率、确认时延与待确认清单。

``OrderSafetyAcknowledgement`` 已经把「谁在什么时候确认过哪一版安全卡」落下来了，
但它的读路径只有订单详情页和后台。运营侧看不到当前版本的确认覆盖、双方补齐的
节奏，也看不到哪些还挂着的订单缺哪一侧确认。

本模块按订单创建时间切窗口，只收仍处于 ``pending`` / ``confirmed`` / ``meeting``
的订单——这三个状态与确认入口一致。当前指纹必须在这里重新计算，不能拿历史确认
冒充：交付地点、商品图片或预约状态变化后，旧版本确认只进入历史，不进入当前覆盖。

四条边界：

1. 确认是用户自己的核对动作，不是对方的待办；本模块不发通知、不生成告警。
2. 缺任一侧确认的订单是右删失样本，只报规模，不进时延中位数和分档。
3. 没有落库的安全卡版本不可知，版本变化只能按「曾被确认过的指纹数」报下界。
4. 清单不输出用户名、不按用户汇总、不排名；它是给运营看订单缺口的，不是考勤表。
"""

from collections import defaultdict, namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import Order, OrderSafetyAcknowledgement
from .transaction_safety import (
    build_transaction_safety, transaction_safety_fingerprint,
)

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 与 acknowledge_order_safety 的入口保持一致。已完成、取消、借用中、归还后的订单
# 不能补确认；把它们放进当前覆盖率分母，会把流程限制读成用户拖延。
ACTIVE_STATUSES = ('pending', 'confirmed', 'meeting')

# 确认节奏按小时和天切。安全卡核对发生在用户打开订单的自然节奏里，分钟级抖动
# 没有运营含义；边界与订单确认、评价等既有看板保持同一个时间尺度。
LATENCY_BUCKETS = (
    (3600, '1 小时内', '下单后很快核对'),
    (6 * 3600, '1-6 小时', '同一天内处理'),
    (86400, '当天', '仍在一个自然日内'),
    (3 * 86400, '隔天到 3 天', '交易安排推进后才补看'),
    (None, '3 天以上', '需要核对时已经隔了很久'),
)

# 清单服务「现在去看哪几单」，不按统计周期切；但页面只列前 20 条，总数照实报。
PENDING_ROW_LIMIT = 20

ROLE_LABELS = {
    'buyer': '买家',
    'seller': '卖家',
}

# 一次性把订单和确认记录取回来，再在应用层复算当前指纹。安全卡本身依赖少量
# 每单上下文查询；这里至少避免同一份订单与确认记录在四个统计节里各查一遍。
OrderAcknowledgementState = namedtuple('OrderAcknowledgementState', [
    'order',
    'current_fingerprint',
    'acknowledgements',
    'acknowledged_fingerprints',
    'current_by_role',
])


def _period_start(days, today):
    """统计窗口第一天的本地零点。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _share(part, whole):
    """占比；分母为 0 时返回 None，页面显示「—」，不把 0 样本包装成 0%。"""
    return round(part / whole * 100, 1) if whole else None


def _percentile(values, fraction):
    """Nearest-rank percentile，保持与其余看板模块同一套解释。"""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _format_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 0:
        return '时间异常'
    if seconds < 60:
        return '不到 1 分钟'
    if seconds < 3600:
        return f'{seconds // 60} 分钟'
    if seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f'{hours} 小时{minutes:02d} 分' if minutes else f'{hours} 小时'
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    return f'{days} 天{hours:02d} 小时' if hours else f'{days} 天'


def _bucket_rows(seconds_values, buckets):
    """按固定左闭右开分档计数；0 样本档保留。"""
    counts = [0] * len(buckets)
    for seconds in seconds_values:
        for index, bucket in enumerate(buckets):
            limit = bucket[0]
            if limit is None or seconds < limit:
                counts[index] += 1
                break
    total = len(seconds_values)
    return [
        {
            'label': label,
            'note': note,
            'count': counts[index],
            'share': _share(counts[index], total),
        }
        for index, (_, label, note) in enumerate(buckets)
    ]


def _fetch_states(*, period_orders, all_active_orders):
    """取订单状态与确认记录，并复算当前安全卡指纹。"""
    orders_by_id = {order.id: order for order in all_active_orders}
    for order in period_orders:
        orders_by_id[order.id] = order

    acknowledgements_by_order = defaultdict(list)
    if orders_by_id:
        acknowledgements = OrderSafetyAcknowledgement.objects.filter(
            order_id__in=orders_by_id,
        ).order_by('acknowledged_at', 'id')
        for acknowledgement in acknowledgements:
            acknowledgements_by_order[acknowledgement.order_id].append(acknowledgement)

    states = []
    for order in orders_by_id.values():
        summary = build_transaction_safety(order)
        fingerprint = transaction_safety_fingerprint(summary)
        records = acknowledgements_by_order.get(order.id, [])
        current_by_role = {}
        for record in records:
            if record.fingerprint == fingerprint and record.role not in current_by_role:
                current_by_role[record.role] = record
        states.append(OrderAcknowledgementState(
            order=order,
            current_fingerprint=fingerprint,
            acknowledgements=records,
            acknowledged_fingerprints={record.fingerprint for record in records},
            current_by_role=current_by_role,
        ))
    states.sort(key=lambda state: (state.order.created_at, state.order.id))
    return states


def _coverage_stats(states):
    total = len(states)
    both = sum(len(state.current_by_role) == 2 for state in states)
    buyer_only = sum(
        set(state.current_by_role) == {'buyer'} for state in states
    )
    seller_only = sum(
        set(state.current_by_role) == {'seller'} for state in states
    )
    neither = sum(not state.current_by_role for state in states)
    buyer_count = both + buyer_only
    seller_count = both + seller_only

    rows = [
        ('双方都已确认', both),
        ('仅买家确认', buyer_only),
        ('仅卖家确认', seller_only),
        ('双方未确认', neither),
    ]
    return {
        'total': total,
        'both_count': both,
        'buyer_only_count': buyer_only,
        'seller_only_count': seller_only,
        'neither_count': neither,
        'buyer_count': buyer_count,
        'seller_count': seller_count,
        'buyer_rate': _share(buyer_count, total),
        'seller_rate': _share(seller_count, total),
        'both_rate': _share(both, total),
        'rows': [
            {'label': label, 'count': count, 'share': _share(count, total)}
            for label, count in rows
        ],
    }


def _latency_stats(states, coverage_total):
    completed_states = [
        state for state in states if len(state.current_by_role) == 2
    ]
    first_values = []
    complete_values = []
    invalid_count = 0
    for state in completed_states:
        durations = [
            (record.acknowledged_at - state.order.created_at).total_seconds()
            for record in state.current_by_role.values()
        ]
        if any(value < 0 for value in durations):
            invalid_count += 1
            continue
        first_values.append(min(durations))
        complete_values.append(max(durations))

    return {
        'completed_count': len(completed_states),
        'incomplete_count': coverage_total - len(completed_states),
        'invalid_timestamp_count': invalid_count,
        'first_median_label': _format_duration(_percentile(first_values, 0.5)),
        'first_p90_label': _format_duration(_percentile(first_values, 0.9)),
        'complete_median_label': _format_duration(_percentile(complete_values, 0.5)),
        'complete_p90_label': _format_duration(_percentile(complete_values, 0.9)),
        'first_bucket_rows': _bucket_rows(first_values, LATENCY_BUCKETS),
        'complete_bucket_rows': _bucket_rows(complete_values, LATENCY_BUCKETS),
    }


def _version_stats(states):
    observed = [state for state in states if state.acknowledged_fingerprints]
    one_version = sum(len(state.acknowledged_fingerprints) == 1 for state in observed)
    two_versions = sum(len(state.acknowledged_fingerprints) == 2 for state in observed)
    three_or_more = sum(len(state.acknowledged_fingerprints) >= 3 for state in observed)
    current_after_prior = sum(
        state.current_fingerprint in state.acknowledged_fingerprints
        and len(state.acknowledged_fingerprints) > 1
        for state in observed
    )
    current_pending_after_prior = sum(
        state.current_fingerprint not in state.acknowledged_fingerprints
        and bool(state.acknowledged_fingerprints)
        for state in observed
    )
    rows = [
        ('确认过 1 个指纹', one_version),
        ('确认过 2 个指纹', two_versions),
        ('确认过 3 个及以上指纹', three_or_more),
    ]
    return {
        'observed_order_count': len(observed),
        'unobserved_order_count': len(states) - len(observed),
        'one_version_count': one_version,
        'two_version_count': two_versions,
        'three_or_more_count': three_or_more,
        'current_after_prior_count': current_after_prior,
        'current_pending_after_prior_count': current_pending_after_prior,
        'rows': [
            {'label': label, 'count': count, 'share': _share(count, len(observed))}
            for label, count in rows
        ],
    }


def _pending_stats(states, now):
    pending_states = [
        state for state in states if len(state.current_by_role) < 2
    ]
    pending_states.sort(
        key=lambda state: (
            -len(state.current_by_role),
            state.order.created_at,
            state.order.id,
        ),
    )
    rows = []
    for state in pending_states[:PENDING_ROW_LIMIT]:
        missing_roles = [
            ROLE_LABELS[role] for role in ('buyer', 'seller')
            if role not in state.current_by_role
        ]
        age = (now - state.order.created_at).total_seconds()
        rows.append({
            'order_id': state.order.id,
            'item_title': state.order.item.title,
            'status_label': state.order.get_status_display(),
            'missing_roles_label': '、'.join(missing_roles),
            'missing_count': len(missing_roles),
            'age_label': _format_duration(age),
        })
    return {
        'total': len(pending_states),
        'missing_both_count': sum(len(state.current_by_role) == 0 for state in pending_states),
        'missing_one_count': sum(len(state.current_by_role) == 1 for state in pending_states),
        'row_limit': PENDING_ROW_LIMIT,
        'is_truncated': len(pending_states) > PENDING_ROW_LIMIT,
        'rows': rows,
    }


def _summary(coverage, pending):
    if coverage['total']:
        return (
            f"本期 {coverage['total']} 笔仍可确认订单里，双方当前确认 "
            f"{coverage['both_count']} 笔；全站待确认 {pending['total']} 笔。"
        )
    if pending['total']:
        return (
            f"本期窗口内没有仍可确认的新订单，但全站仍有 {pending['total']} 笔待确认。"
        )
    return '当前没有仍处于可确认阶段的订单。'


def build_transaction_safety_acknowledgement_quality(days=30, now=None):
    """统计交易安全卡确认覆盖、确认节奏与待确认清单；只读，不改任何记录。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localtime(now).date())

    base_orders = Order.objects.select_related(
        'item', 'buyer', 'seller', 'meeting_location', 'item__category', 'item__location',
    ).filter(status__in=ACTIVE_STATUSES).order_by('created_at', 'id')
    period_orders = list(base_orders.filter(created_at__gte=start, created_at__lte=now))
    all_active_orders = list(base_orders)

    states = _fetch_states(
        period_orders=period_orders, all_active_orders=all_active_orders,
    )
    period_state_ids = {order.id for order in period_orders}
    period_states = [
        state for state in states if state.order.id in period_state_ids
    ]

    coverage = _coverage_stats(period_states)
    latency = _latency_stats(period_states, coverage['total'])
    versions = _version_stats(period_states)
    pending = _pending_stats(states, now)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'active_statuses': ACTIVE_STATUSES,
        'has_data': bool(states),
        'summary': _summary(coverage, pending),
        'coverage': coverage,
        'latency': latency,
        'versions': versions,
        'pending': pending,
        'pending_row_limit': PENDING_ROW_LIMIT,
    }
