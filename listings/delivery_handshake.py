# -*- coding: utf-8 -*-
"""当面交付的两次握手，第一次被运营读到。

## 已有什么，缺什么

订单进入「待当面交付」之后，交付这件事被拆成两个各自独立的动作：买家点一下
「确认交付」写 `buyer_confirmed_at`，卖家点一下「确认交付」写 `seller_confirmed_at`。
两个字段都写上，订单才从 `meeting` 走到 `completed`（或 `borrowed`），商品才变成
`sold`，贡献记录才落库。写入路径是完整的：`confirm_delivery` 有行锁，有「你已经
确认过」的幂等保护，有卖家侧六位确认码的核对，甚至有「对方确认了交付，请你确认」
的站内通知。

而读取路径只有两处：`order_detail` 把两个时间戳显示给双方本人看，`admin` 把它们
列给管理员。**运营侧一条统计都没有。**

这不是一个抽象的缺口，它是本面板上最奇怪的一处失衡。`handoff_code_usage` 读的是
同一张表 `DeliveryConfirmation` 的另外五个字段，报生成率、使用率、错误次数、生成到
使用的间隔；`return_handshake` 读的是同一张表的 `buyer_returned_at` 与
`seller_returned_at`，报借用归还的两次握手、谁先登记、隔了多久。**中间那一对——
当面交付的两次握手——没有任何读者。** 确认码有了统计，归还握手有了统计，而交付本身
只是订单状态机上的一个过场。

更具体的，这个缺口对应两类方向完全相反的运营动作。

第一类是**一方点过，另一方始终没点**。这种订单的 `is_complete` 是 `False`，订单
永远停在 `meeting`，商品永远停在 `reserved`。双方都还能看到「等待对方确认」的按钮，
通知发过了，此后再没有任何动作。平台既不知道有多少这样的订单，也不知道它们已经
挂了多久。

第二类是**顺序反常**。借用方先登记归还、发布者先登记归还，是 `return_handshake`
已经报过的反常信号；当面交付这一侧同理，只是更微妙——卖家先点「确认交付」意味着
他已经把东西给出去了，而买家还没点。这不是安全问题，但它是一个可以观察的事实，
而且现在没人看。

## 这个模块报什么

只读，不加迁移、不加字段、不改模型、不改任何写入路径。

### 一、走完当面交付的订单，两次握手是什么顺序

按「谁先点」分档：买家先、卖家先、双方同一秒。同一秒单独一档，因为它既不是「买家
先」也不是「卖家先」，硬并进任何一边都会让顺序这一列失真。

### 二、两下之间隔了多久

从先点的那一刻到后点的那一刻。按小时和天切，不按分钟——一次当面交付的时间尺度是
分钟到天，报「平均等了 26 分钟」对一个按周看板的运营没有意义。

### 三、只有一方点过的订单

分母是「已经离开 `meeting`」的订单，`is_complete` 为 `False` 的单独一节报：多少条、
其中买家点过而卖家没点、卖家点过而买家没点、以及从先点那一刻到现在挂了多久。

这一节的口径最要紧：**挂起时长不按统计周期切。** 一笔订单如果跨越周期边界，把它
按周期切开会让一个跨月的等待被算成两段短的，而那正是最不该被切开的样本。和
`return_handshake`、`handoff_code_usage` 的重新生成计数是同一个道理。

### 四、卡住的订单清单

列出当前 `is_complete` 为 `False`、且已经离开 `meeting` 的订单，按挂起时长倒序。
清单只列订单号、商品标题、先点的一方、挂了多久，不列用户名——那是一份考勤表，
不是一份看板。

## 边界与取舍

- **分母只收「已经离开 `meeting`」的订单。** 还挂在本阶段的订单根本谈不上握手有没
  有完成，是右删失样本，单独数出来，不丢也不混。和 `handoff_code_usage`、
  `meeting_attendance`、`return_handshake` 同一个道理。
- **`cancelled` 的订单要回去问事件表。** 一笔订单带着一次真实的交付确认被取消，和
  一笔从没进入过 `meeting` 就被取消，是两回事。前者两次握手开始过，后者压根没开始。
  `return_handshake` 已经为借用侧立了这个规矩，这里沿用。
- **只做对照，不给因果。** 两次握手间隔短的订单更可能完成，很可能只是因为双方本来
  就更认真；平台没有能力把这两件事分开，也就不该把对照讲成结论。
- **不按用户排名、不列用户名、不列自由文本原文。**
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import DeliveryConfirmation, Order, OrderEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 当面交付阶段。两个交付确认时间戳只在订单处于这个状态时被写，视图里
# confirm_delivery 守着这一条：状态一变，按钮就消失，两个字段从此不再变化。
MEETING_STATUS = 'meeting'

# 走完当面交付的订单状态。只有从「待当面交付」走到这些状态，两次握手才真的都点过了。
CONCLUDED_STATUSES = ('completed', 'borrowed', 'returned', 'cancelled')

# 其中真正做成了的那些。取消不是失败：它可能只是双方没约成，和交付确认点没点无关。
SUCCESS_STATUSES = ('completed', 'borrowed', 'returned')

# 两组握手间隔分档。按小时和天切，不按分钟。
GAP_BUCKETS = (
    (60, '1 分钟内'),
    (3600, '1 小时内'),
    (86400, '当天'),
    (259200, '隔天到 3 天'),
    (None, '3 天以上'),
)

# 一方点过之后的挂起时长分档。按天切：一笔交付没点完的订单，时间尺度是天。
HANG_BUCKETS = (
    (86400, '当天', '先点的一方还在等，另一方当天就会跟上'),
    (3 * 86400, '1-3 天', '一方点了，另一方三天内跟上'),
    (7 * 86400, '3-7 天', '等满一周之内，双方都还记得这件事'),
    (30 * 86400, '7-30 天', '超过一周，通常意味着其中一方忘了这回事'),
    (None, '30 天以上', '长尾：挂了快一个月，商品和钱都还卡在这一笔上'),
)

# 一组订单至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条样本上
# 几个百分点的差距是噪声。
MIN_MEASURED_ORDERS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 卡住订单清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
STALLED_LIST_LIMIT = 20

# 谁先点。订单上没有这个字段，这里从两个时间戳的先后推。
FIRST_SIDES = (
    ('buyer', '买家先点'),
    ('seller', '卖家先点'),
    ('both', '双方同一秒'),
    ('none', '只有一方点过'),
)

FIRST_SIDE_LABELS = dict(FIRST_SIDES)


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


def _format_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} 分钟'
    if seconds < 3600:
        return f'{seconds // 60} 分钟'
    if seconds < 86400:
        return f'{seconds // 3600} 小时'
    return f'{seconds // 86400} 天'


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _gap_bucket_index(seconds):
    """两下之间的间隔落在哪一档。"""
    if seconds is None:
        return None
    for index, (limit, _label) in enumerate(GAP_BUCKETS):
        if limit is None or seconds < limit:
            return index
    return len(GAP_BUCKETS) - 1


def _hang_bucket_index(seconds):
    """一方点过之后挂了多久。"""
    if seconds is None:
        return None
    for index, (limit, _label, _note) in enumerate(HANG_BUCKETS):
        if limit is None or seconds < limit:
            return index
    return len(HANG_BUCKETS) - 1


def _entered_meeting(order):
    """这笔订单有没有真的进入过当面交付。

    一笔被取消的订单，可能是带着一次真实的交付确认被取消，也可能是从没进入过
    ``meeting`` 就被取消。后者两次握手压根没开始，不该进分母。回去问事件表。
    """
    if order.status != 'cancelled':
        return True
    return OrderEvent.objects.filter(
        order=order, to_status=MEETING_STATUS,
    ).exists()


def _fetch_concluded_orders(*, created_after=None, created_before=None):
    """按显式排序取出已经离开当面交付阶段的订单。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。
    """
    orders = Order.objects.select_related(
        'item', 'buyer', 'seller', 'delivery_confirmation',
    ).filter(status__in=CONCLUDED_STATUSES).order_by('created_at', 'id')
    if created_after is not None:
        orders = orders.filter(created_at__gte=created_after)
    if created_before is not None:
        orders = orders.filter(created_at__lte=created_before)
    return list(orders)


def _build_sequence_rows(orders, confirmation_by_order):
    """两次握手是什么顺序，以及两下之间隔了多久。"""
    counts = {key: 0 for key, _label in FIRST_SIDES}
    gaps = {key: [] for key, _label in FIRST_SIDES}
    bucket_counts = [0] * len(GAP_BUCKETS)

    for order in orders:
        confirmation = confirmation_by_order.get(order.id)
        if confirmation is None:
            continue
        buyer_at = confirmation.buyer_confirmed_at
        seller_at = confirmation.seller_confirmed_at
        if not buyer_at and not seller_at:
            continue
        if buyer_at and seller_at:
            if buyer_at == seller_at:
                side = 'both'
                gap = 0.0
            else:
                first_is_buyer = buyer_at < seller_at
                side = 'buyer' if first_is_buyer else 'seller'
                gap = abs((buyer_at - seller_at).total_seconds())
        else:
            side = 'none'
            gap = None
        counts[side] += 1
        if gap is not None:
            gaps[side].append(gap)
            index = _gap_bucket_index(gap)
            if index is not None:
                bucket_counts[index] += 1

    total = sum(counts.values())
    rows = []
    for key, label in FIRST_SIDES:
        count = counts[key]
        rows.append({
            'key': key,
            'label': label,
            'count': count,
            'share': _percent(count, total),
            'median_gap_label': _format_duration(_median(gaps[key])),
            'is_small_sample': count < MIN_MEASURED_ORDERS,
        })
    measurable = [
        key for key in ('buyer', 'seller', 'both') if counts[key] >= MIN_MEASURED_ORDERS
    ]
    return rows, bucket_counts, measurable


def _build_hang_rows(orders, confirmation_by_order, now):
    """只有一方点过的订单：多少条、点的是哪一方、挂了多久。"""
    hang_counts = {'buyer_only': 0, 'seller_only': 0}
    hang_bucket_counts = [0] * len(HANG_BUCKETS)
    hang_seconds = []

    for order in orders:
        confirmation = confirmation_by_order.get(order.id)
        if confirmation is None:
            continue
        buyer_at = confirmation.buyer_confirmed_at
        seller_at = confirmation.seller_confirmed_at
        if bool(buyer_at) == bool(seller_at):
            continue
        side = 'buyer_only' if buyer_at else 'seller_only'
        hang_counts[side] += 1
        first_at = buyer_at or seller_at
        seconds = max((now - first_at).total_seconds(), 0.0)
        hang_seconds.append(seconds)
        index = _hang_bucket_index(seconds)
        if index is not None:
            hang_bucket_counts[index] += 1

    total = sum(hang_counts.values())
    return {
        'total': total,
        'buyer_only': hang_counts['buyer_only'],
        'seller_only': hang_counts['seller_only'],
        'buyer_only_share': _percent(hang_counts['buyer_only'], total),
        'seller_only_share': _percent(hang_counts['seller_only'], total),
        'median_hang_label': _format_duration(_median(hang_seconds)),
        'bucket_rows': [
            {
                'label': label,
                'note': note,
                'count': count,
                'share': _percent(count, total),
            }
            for (limit, label, note), count in zip(HANG_BUCKETS, hang_bucket_counts)
        ],
    }


def _build_stalled_rows(orders, confirmation_by_order, now):
    """当前只有一方点过、且已经离开当面交付的订单，按挂起时长倒序。"""
    rows = []
    for order in orders:
        confirmation = confirmation_by_order.get(order.id)
        if confirmation is None:
            continue
        buyer_at = confirmation.buyer_confirmed_at
        seller_at = confirmation.seller_confirmed_at
        if bool(buyer_at) == bool(seller_at):
            continue
        first_at = buyer_at or seller_at
        seconds = max((now - first_at).total_seconds(), 0.0)
        rows.append({
            'order_id': order.id,
            'item_title': order.item.title if order.item_id else '',
            'first_side': 'buyer' if buyer_at else 'seller',
            'first_side_label': FIRST_SIDE_LABELS['buyer' if buyer_at else 'seller'],
            'hang_label': _format_duration(seconds),
            'hang_seconds': seconds,
            'order_status': order.status,
        })
    rows.sort(key=lambda row: -row['hang_seconds'])
    return rows[:STALLED_LIST_LIMIT]


def _build_outcome_contrast(orders, confirmation_by_order):
    """握手间隔与订单是否做成，只做对照，不给因果。"""
    fast = 0
    fast_success = 0
    slow = 0
    slow_success = 0
    for order in orders:
        confirmation = confirmation_by_order.get(order.id)
        if confirmation is None:
            continue
        buyer_at = confirmation.buyer_confirmed_at
        seller_at = confirmation.seller_confirmed_at
        if not (buyer_at and seller_at):
            continue
        gap = abs((buyer_at - seller_at).total_seconds())
        is_success = order.status in SUCCESS_STATUSES
        if gap < 3600:
            fast += 1
            fast_success += int(is_success)
        else:
            slow += 1
            slow_success += int(is_success)
    fast_rate = _percent(fast_success, fast)
    slow_rate = _percent(slow_success, slow)
    gap_points = (
        round(fast_rate - slow_rate, 1) if fast and slow else None
    )
    return {
        'fast_total': fast,
        'fast_success': fast_success,
        'fast_rate': fast_rate,
        'slow_total': slow,
        'slow_success': slow_success,
        'slow_rate': slow_rate,
        'gap_points': gap_points,
        'is_measurable': fast >= MIN_MEASURED_ORDERS and slow >= MIN_MEASURED_ORDERS,
    }


def _build_summary(sequence_rows, hang, contrast):
    total = sum(row['count'] for row in sequence_rows)
    if not total:
        return '当前周期还没有走完当面交付的订单，交付确认的两下之间还读不出顺序。'
    both = sum(row['count'] for row in sequence_rows if row['key'] != 'none')
    buyer_first = next(row['count'] for row in sequence_rows if row['key'] == 'buyer')
    seller_first = next(row['count'] for row in sequence_rows if row['key'] == 'seller')
    parts = [
        f'走完当面交付且两次都点过的订单 {both} 笔，买家先点 {buyer_first} 笔、'
        f'卖家先点 {seller_first} 笔。',
    ]
    if hang['total']:
        parts.append(
            f'只有一方点过的 {hang["total"]} 笔，中位挂起 {hang["median_hang_label"]}。'
        )
    if contrast['gap_points'] is not None and contrast['is_measurable']:
        parts.append(f'两下间隔在 1 小时内的订单做成率高出 {contrast["gap_points"]} 个百分点。')
    return ' '.join(parts)


def build_delivery_handshake(days=30, now=None):
    '''当面交付的两次握手：谁先点、隔多久、有没有只点一半，只报告不改写任何字段。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    orders = _fetch_concluded_orders(created_after=start, created_before=now)

    confirmation_by_order = {
        order.id: getattr(order, 'delivery_confirmation', None) for order in orders
    }
    entered = [order for order in orders if _entered_meeting(order)]

    sequence_rows, bucket_counts, measurable = _build_sequence_rows(
        entered, confirmation_by_order,
    )
    hang = _build_hang_rows(entered, confirmation_by_order, now)
    contrast = _build_outcome_contrast(entered, confirmation_by_order)
    stalled_rows = _build_stalled_rows(entered, confirmation_by_order, now)

    # 右删失样本单独数：还挂在当面交付阶段的订单，握手还没结束，根本谈不上有没有完成。
    pending_count = Order.objects.filter(status=MEETING_STATUS, created_at__lte=now).count()
    pending_in_window = Order.objects.filter(
        status=MEETING_STATUS, created_at__gte=start, created_at__lte=now,
    ).count()

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(entered),
        'summary': _build_summary(sequence_rows, hang, contrast),
        'sequence_rows': sequence_rows,
        'gap_bucket_rows': [
            {
                'label': label,
                'count': count,
                'share': _percent(count, sum(bucket_counts)),
            }
            for (limit, label), count in zip(GAP_BUCKETS, bucket_counts)
        ],
        'measurable_sides': measurable,
        'hang_stats': hang,
        'stalled_rows': stalled_rows,
        'stalled_row_limit': STALLED_LIST_LIMIT,
        'outcome_contrast': contrast,
        'pending_count': pending_count,
        'pending_in_window': pending_in_window,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'noise_points': NOISE_POINTS,
    }