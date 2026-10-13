# -*- coding: utf-8 -*-
"""Measure the deadline the platform promised, and whether nudges changed anything.

`Order` 上有四个连续的时间字段——`confirmation_deadline`、
`confirmation_reminder_sent_at`、`return_reminder_sent_at`、
`return_overdue_notice_sent_at`。写入路径全在 `order_maintenance.py` 里，完整、
幂等、有行锁；读取路径只有 `admin.py` 列给管理员看、`order_detail.html` 在
`status == 'pending'` 时把「确认截止」显示给本人看，以及两个视图里各一份硬编码的
24 小时。运营侧一条统计都没有。

这个模块补上三件事：卖家确认到底要多久、催过之后到底有没有用、借用侧催过之后
归还节奏变了没有。

六条必须守住的边界，决定了它能声称什么、不能声称什么：

1. **只报相关性，不报因果。** 一笔订单在提醒之后确认，可能正是因为那条提醒，也可能
   是卖家本来就要确认、只是恰好排在那之后。本模块能确认的只有「动作发生在提醒
   之后」，所以看板上标注为观察值，不写成归因结果。和 `lifecycle_reminder_effect`
   同一个立场。
2. **分母只收「已经离开 `pending`」的订单。** 还挂在本阶段的订单，确认还没发生，
   根本谈不上花了多久——这是右删失样本，和 `handoff_code_usage`、
   `meeting_attendance`、`return_handshake` 同一个道理。单独数出来，不丢也不混。
3. **确认时刻只认 `OrderEvent`。** `Order.updated_at` 是 `auto_now`，任何一次保存
   都会改写它，拿它证明「卖家此刻确认」是不成立的。`transition_order` 在状态真正
   走到 `confirmed` 时写一条事件，那才是可观测的确认时刻。没有事件行的订单不进
   时长样本，单独数出来——它的确认发生在 `OrderEvent` 上线之前，或者状态是别处
   直接改的，平台答不了它花了多久。
4. **被自动释放的订单按「有没有催过」拆。** 催过之后仍被释放，和从没催过就被释放，
   是两件不同的事：后者意味着卖家在整整 24 小时里没有上线。合成一个数字会让人以为
   问题只有一种。
5. **借用侧单独取数。** 它有自己的状态终点和提醒字段，混在出售订单里会让两边的
   「催过之后」互相污染。借用订单按 `Item.trade_mode == 'borrow'` 认，不按订单状态
   猜——一笔 `completed` 的出售订单和一笔 `returned` 的借用订单讲的是两件事。
6. **不按统计周期切「超时还没确认」的等待。** 一笔跨越周期边界的等待，按周期切开
   会被算成两段短的，而那正是最不该被切开的样本。这一列按订单的一生累计。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import Order, OrderEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 已经离开「待卖家确认」的订单状态。取消也算离开——它确实有过一个终点。
CONCLUDED_STATUSES = ('confirmed', 'meeting', 'completed', 'borrowed', 'returned', 'cancelled')

# 订单走到这个状态，才认为卖家真的确认了。
CONFIRMED_STATUS = 'confirmed'

# 确认时长分档。按小时和天切，不按分钟——一次卖家确认的时间尺度是小时到天。
CONFIRM_BUCKETS = (
    (3600, '1 小时内'),
    (6 * 3600, '1-6 小时'),
    (86400, '当天'),
    (3 * 86400, '隔天到 3 天'),
    (None, '3 天以上'),
)

# 提醒之后确认的订单，距截止还剩多久。左闭右开、互斥且完整。
# 第一档的上界写 0 而不是 None：`process_order_timeouts` 的释放判定是
# `deadline <= now`，所以踩着截止点过去之后才确认的订单确实存在，只是极少。
# 写 None 会让它变成兜底档，把所有正数都吸进去——桶表的第一档永远是下界。
MARGIN_BUCKETS = (
    (0, '已过截止才确认', '截止点过去之后才点确认，只是没被自动释放——通常是刚好错过'),
    (3600, '1 小时内', '踩线：只剩不到一小时，基本是提醒起的作用'),
    (6 * 3600, '1-6 小时', '提前量刚好覆盖默认的 6 小时提醒'),
    (24 * 3600, '6-24 小时', '提前量比承诺还长，提醒对它没有意义'),
    (None, '24 小时以上', '很早就确认了，截止时间对它没有约束力'),
)

# 借用侧催过之后的归还节奏：距「预计归还」多久才真的归还。
RETURN_MARGIN_BUCKETS = (
    (0, '逾期才归还', '归还登记晚于预计归还时间，催收阶梯该管的事'),
    (3600, '提前 1 小时内', '踩线：提醒起作用的窗口就在这里'),
    (86400, '提前 1 天内', '提前量刚够一次安排'),
    (None, '提前 1 天以上', '提前量充足，提醒对它没有意义'),
)

# 借用订单的终点。借用交易的终点是「已归还」，与出售的「交易完成」分开。
BORROW_CONCLUDED_STATUSES = ('returned', 'cancelled')

# 一组订单至少要这么多条，占比和对照才给方向。和其余模块同一个量级。
MIN_MEASURED_ORDERS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
PENDING_LIST_LIMIT = 20
RELEASE_LIST_LIMIT = 20

# `process_order_timeouts` 释放预约时写下的事件备注。自动释放和用户主动取消，
# 靠这条备注区分——两者都表现为 `cancelled`，但运营动作完全相反。
AUTO_RELEASE_NOTE = '自动释放'


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


def _bucket_index(buckets, seconds):
    """值落在哪一档。左闭右开，最后一档收尾。

    桶表有两列的也有三列的（`(limit, label)` 与 `(limit, label, note)`），所以
    只取每个元组的第一个元素当上界，剩下的原样留给调用方拼表。

    这里按顺序找第一个「严格小于上界」的档位，所以第一档的上界必须写 0 而不是
    ``None``：写 ``None`` 会让它变成兜底档，把所有正数都吸进去。只有最后一档的
    上界才写 ``None``。
    """
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _confirmed_at_by_order(orders):
    """每笔订单的卖家确认时刻，只认 `OrderEvent`。

    `Order.updated_at` 是 `auto_now`，任何一次保存都会改写它，不能拿来证明「卖家
    此刻确认」。状态真正走到 `confirmed` 时 `transition_order` 写一条事件，那才是
    可观测的确认时刻。同一笔订单若有多条事件行，取最早的一条——第一次确认才是
    这次承诺的兑现时刻。
    """
    order_ids = [order.id for order in orders]
    if not order_ids:
        return {}
    rows = {}
    events = OrderEvent.objects.filter(
        order_id__in=order_ids, to_status=CONFIRMED_STATUS,
    ).order_by('created_at', 'id').values_list('order_id', 'created_at')
    for order_id, created_at in events:
        if order_id not in rows:
            rows[order_id] = created_at
    return rows


def _auto_released_order_ids(orders):
    """哪些订单是被系统自动释放的，哪些是用户主动取消的。

    两者都表现为 `cancelled`，但运营动作完全相反：一边是「卖家 24 小时没上线」，
    一边是「买家改了主意」。只认事件备注，不按状态猜。
    """
    order_ids = [order.id for order in orders]
    if not order_ids:
        return set()
    return set(
        OrderEvent.objects.filter(
            order_id__in=order_ids, note__contains=AUTO_RELEASE_NOTE,
        ).values_list('order_id', flat=True)
    )


def _fetch_orders(*, created_after=None, created_before=None, statuses, borrow_only=False):
    """按显式排序取出订单。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。
    """
    orders = Order.objects.select_related('item', 'buyer', 'seller').filter(
        status__in=statuses,
    ).order_by('created_at', 'id')
    if borrow_only:
        orders = orders.filter(item__trade_mode='borrow')
    if created_after is not None:
        orders = orders.filter(created_at__gte=created_after)
    if created_before is not None:
        orders = orders.filter(created_at__lte=created_before)
    return list(orders)


def _build_confirm_stats(orders, confirmed_at_by_order):
    """从下单到卖家确认花了多久。"""
    bucket_counts = [0] * len(CONFIRM_BUCKETS)
    seconds_list = []
    for order in orders:
        confirmed_at = confirmed_at_by_order.get(order.id)
        if confirmed_at is None:
            continue
        seconds = (confirmed_at - order.created_at).total_seconds()
        if seconds < 0:
            continue
        seconds_list.append(seconds)
        bucket_counts[_bucket_index(CONFIRM_BUCKETS, seconds)] += 1
    total = sum(bucket_counts)
    return {
        'total': total,
        'median_label': _format_duration(_median(seconds_list)),
        'bucket_rows': [
            {
                'label': label,
                'count': count,
                'share': _percent(count, total),
            }
            for (limit, label), count in zip(CONFIRM_BUCKETS, bucket_counts)
        ],
    }


def _build_reminder_stats(orders, confirmed_at_by_order, auto_released_ids, start, now):
    """催过之后到底有没有用。

    分母是周期内真的收到过确认提醒的订单。四种结局分开报，合成一个「已提醒 N 笔」
    会把最有效的提醒和最无效的提醒混成一个平均值：

    - 提醒之后确认了，且距截止还剩多少（最有信息量的一列）；
    - 提醒之后仍被自动释放：催了也没用；
    - 提醒之后用户主动取消：和承诺无关，单独数；
    - 确认时刻取不到（没有事件行）：右删失样本，不进任何一档。
    """
    reminded = [
        order for order in orders
        if order.confirmation_reminder_sent_at is not None
        and start <= order.confirmation_reminder_sent_at <= now
    ]
    confirmed_after = 0
    expired_after = 0
    cancelled_by_user = 0
    unmeasurable = 0
    margin_counts = [0] * len(MARGIN_BUCKETS)

    for order in reminded:
        confirmed_at = confirmed_at_by_order.get(order.id)
        if confirmed_at is not None:
            if confirmed_at >= order.confirmation_reminder_sent_at:
                confirmed_after += 1
                margin = (order.confirmation_deadline - confirmed_at).total_seconds()
                margin_counts[_bucket_index(MARGIN_BUCKETS, margin)] += 1
            else:
                # 确认发生在提醒之前：提醒是幂等的，正常不会出现。单独数，不猜。
                unmeasurable += 1
        elif order.id in auto_released_ids:
            expired_after += 1
        elif order.status == 'cancelled':
            cancelled_by_user += 1
        else:
            unmeasurable += 1

    total = len(reminded)
    return {
        'total': total,
        'confirmed_after': confirmed_after,
        'expired_after': expired_after,
        'cancelled_by_user': cancelled_by_user,
        'unmeasurable': unmeasurable,
        'confirmed_after_share': _percent(confirmed_after, total),
        'expired_after_share': _percent(expired_after, total),
        'margin_rows': [
            {
                'label': label,
                'note': note,
                'count': count,
                'share': _percent(count, confirmed_after),
            }
            for (limit, label, note), count in zip(MARGIN_BUCKETS, margin_counts)
        ],
    }


def _build_release_rows(orders, auto_released_ids, start, now):
    """被自动释放的订单，按「有没有催过」拆，逐笔列出。"""
    rows = []
    for order in orders:
        if order.id not in auto_released_ids:
            continue
        reminded_at = order.confirmation_reminder_sent_at
        was_reminded = reminded_at is not None and start <= reminded_at <= now
        waited = (
            (order.confirmation_deadline - order.created_at).total_seconds()
            if order.confirmation_deadline else None
        )
        rows.append({
            'order_id': order.id,
            'item_title': order.item.title if order.item_id else '',
            'waited_label': _format_duration(waited),
            'was_reminded': was_reminded,
            'reminded_at': reminded_at,
        })
    rows.sort(key=lambda row: (not row['was_reminded'], -row['order_id']))
    return rows[:RELEASE_LIST_LIMIT]


def _build_borrow_stats(orders, start, now):
    """借用侧催过之后，归还节奏变了没有。

    分母是周期内收到过归还提醒、且已经离开「借用中」的借用订单。已经归还的报
    「距预计归还还剩多久」；被取消的单独数——它不是「没还」，它是「交易没有继续」。
    """
    reminded = [
        order for order in orders
        if order.return_reminder_sent_at is not None
        and start <= order.return_reminder_sent_at <= now
    ]
    returned = 0
    cancelled = 0
    unmeasurable = 0
    margin_counts = [0] * len(RETURN_MARGIN_BUCKETS)

    for order in reminded:
        if order.status == 'returned' and order.returned_at and order.return_due_at:
            returned += 1
            margin = (order.return_due_at - order.returned_at).total_seconds()
            margin_counts[_bucket_index(RETURN_MARGIN_BUCKETS, margin)] += 1
        elif order.status == 'cancelled':
            cancelled += 1
        else:
            # 归还登记或预计归还时间缺一个，答不了「提前多久还」。
            unmeasurable += 1

    total = len(reminded)
    return {
        'total': total,
        'returned': returned,
        'cancelled': cancelled,
        'unmeasurable': unmeasurable,
        'returned_share': _percent(returned, total),
        'margin_rows': [
            {
                'label': label,
                'note': note,
                'count': count,
                'share': _percent(count, returned),
            }
            for (limit, label, note), count in zip(RETURN_MARGIN_BUCKETS, margin_counts)
        ],
    }


def _build_pending_rows(orders, now):
    """已经超过截止时间、还挂在「待卖家确认」的订单。

    这一列不按统计周期切：等待时长是从截止时刻算到现在的，而「现在」不属于任何
    统计周期。一笔跨越周期边界的等待，按周期切开会被算成两段短的，而那正是最不该
    被切开的样本。周期只用来筛「哪些订单进入统计」，不用来切等待本身。
    """
    rows = []
    for order in orders:
        if order.status != 'pending' or not order.confirmation_deadline:
            continue
        seconds = max((now - order.confirmation_deadline).total_seconds(), 0.0)
        rows.append({
            'order_id': order.id,
            'item_title': order.item.title if order.item_id else '',
            'overdue_label': _format_duration(seconds),
            'overdue_seconds': seconds,
            'was_reminded': bool(order.confirmation_reminder_sent_at),
        })
    rows.sort(key=lambda row: -row['overdue_seconds'])
    return rows[:PENDING_LIST_LIMIT]


def _build_summary(confirm_stats, reminder_stats, release_rows, borrow_stats):
    if not confirm_stats['total'] and not reminder_stats['total']:
        return '当前周期还没有订单离开「待卖家确认」，确认承诺的快慢还读不出来。'
    parts = []
    if confirm_stats['total']:
        parts.append(
            f'周期内确认了 {confirm_stats["total"]} 笔，'
            f'中位确认时长 {confirm_stats["median_label"]}。'
        )
    if reminder_stats['total']:
        parts.append(
            f'其中 {reminder_stats["total"]} 笔收到过确认提醒，'
            f'提醒之后确认 {reminder_stats["confirmed_after"]} 笔、'
            f'仍被自动释放 {reminder_stats["expired_after"]} 笔。'
        )
    if release_rows:
        parts.append('催过之后仍被自动释放的订单已逐笔列出，供核实。')
    if borrow_stats['total']:
        parts.append(
            f'借用侧 {borrow_stats["total"]} 笔收到过归还提醒，'
            f'已归还 {borrow_stats["returned"]} 笔。'
        )
    return ' '.join(parts)


def build_order_deadline_reminders(days=30, now=None):
    '''订单承诺的截止时间与提醒效果：确认要多久、催了有没有用，只报告不改写。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    orders = _fetch_orders(
        created_after=start, created_before=now, statuses=CONCLUDED_STATUSES,
    )
    confirmed_at_by_order = _confirmed_at_by_order(orders)
    auto_released_ids = _auto_released_order_ids(orders)

    confirm_stats = _build_confirm_stats(orders, confirmed_at_by_order)
    reminder_stats = _build_reminder_stats(
        orders, confirmed_at_by_order, auto_released_ids, start, now,
    )
    release_rows = _build_release_rows(orders, auto_released_ids, start, now)

    # 借用侧单独取数：它有自己的状态终点和提醒字段，混在出售订单里会让两边的
    # 「催过之后」互相污染。借用订单按 `Item.trade_mode` 认，不按订单状态猜。
    borrow_orders = _fetch_orders(
        created_after=start, created_before=now,
        statuses=BORROW_CONCLUDED_STATUSES, borrow_only=True,
    )
    borrow_stats = _build_borrow_stats(borrow_orders, start, now)

    # 右删失样本单独数：还挂在「待卖家确认」的订单，确认还没发生，根本谈不上花了多久。
    pending_orders = _fetch_orders(
        created_after=start, created_before=now, statuses=('pending',),
    )
    pending_rows = _build_pending_rows(pending_orders, now)
    pending_count = Order.objects.filter(
        status='pending', confirmation_deadline__isnull=False, created_at__lte=now,
    ).count()
    pending_in_window = Order.objects.filter(
        status='pending', confirmation_deadline__isnull=False,
        created_at__gte=start, created_at__lte=now,
    ).count()

    # 台账把两件事加成的一个数，在这里拆开报：预约确认即将超时是一侧，借用即将
    # 到期是另一侧。不改 `TaskRun` 的写入口径，只在看板上分开——六个可观测性模块
    # 看到的调度负载本来就分不清是哪一侧在涨。
    confirmation_reminded_in_window = sum(
        1 for order in orders
        if order.confirmation_reminder_sent_at is not None
        and start <= order.confirmation_reminder_sent_at <= now
    )
    borrow_reminded_in_window = sum(
        1 for order in borrow_orders
        if order.return_reminder_sent_at is not None
        and start <= order.return_reminder_sent_at <= now
    )

    # 逾期通知发过之后仍然没归还的借用订单。这不是「催过之后有没有还」，是催收
    # 阶梯的领域，这里只报规模，不动阶梯。
    overdue_still_borrowed = Order.objects.filter(
        status='borrowed', item__trade_mode='borrow',
        return_due_at__isnull=False, return_due_at__lte=now,
        return_overdue_notice_sent_at__isnull=False,
    ).count()

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(orders),
        'summary': _build_summary(
            confirm_stats, reminder_stats, release_rows, borrow_stats,
        ),
        'confirm_stats': confirm_stats,
        'reminder_stats': reminder_stats,
        'release_rows': release_rows,
        'release_row_limit': RELEASE_LIST_LIMIT,
        'borrow_stats': borrow_stats,
        'overdue_still_borrowed': overdue_still_borrowed,
        'pending_count': pending_count,
        'pending_in_window': pending_in_window,
        'pending_rows': pending_rows,
        'pending_row_limit': PENDING_LIST_LIMIT,
        'confirmation_reminded_in_window': confirmation_reminded_in_window,
        'borrow_reminded_in_window': borrow_reminded_in_window,
        'reminder_hours_default': 6,
        'confirmation_timeout_hours': 24,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'noise_points': NOISE_POINTS,
    }