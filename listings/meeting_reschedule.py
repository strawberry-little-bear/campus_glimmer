# -*- coding: utf-8 -*-
"""一次交付安排被改了几次才定下来。

`MeetingAppointment` 是 `OneToOneField(order)`——一笔订单最多一条安排，摆在
那里的时间是「当前这一版」。`propose_meeting`（`views.py` 936 行起）对同一笔
订单第二次提交时走的是 `if not created:` 分支：把 `proposed_by`、`start_at`、
`end_at`、`location` 全部改写，`status` 打回 `pending`，`responded_by` /
`responded_at` 清空。前几版被原地覆盖，没有版本表。

所以「改期」在库里不是一条记录，是一条痕迹：`OrderEvent` 里那句
`更新了交付时间安排，等待对方确认`。这个模块把那句痕迹数出来。

它回答四件事：一次安排平均改几次才定下来、改期之后对方的回应变快了还是变慢
了、改期时地点换没换、以及谁在改期（只按角色）。四件事都是对照，不给因果——
改期多的订单可能本来就是难谈的商品，而不是改期本身让事情变难。

`meeting_attendance.py` 已经报过响应时长的总体中位数和到场率，本模块不复述
那两个数，只报「改过 vs 一次谈成」的对照。两边的分档表必须对得上，否则运营
会以为其中一个看板是错的。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import MeetingAppointment, OrderEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# `propose_meeting` 的 update 分支写下的事件备注。第一次提交写的是「提出了新的
# 交付时间安排」，只多一个字，所以匹配必须写全「更新了」三个字——写成「新的
# 交付时间安排」会把第一次提议也算成一次改期。
RESCHEDULE_NOTE = '更新了交付时间安排'

# 改期次数分档。0 次是「一次谈成」，它是这一列的主档，不是补集：平台关心的
# 恰恰是「有多少安排根本不用改」。
RESCHEDULE_BUCKETS = (
    (1, '一次谈成'),
    (2, '改过一次'),
    (4, '改过 2-3 次'),
    (None, '改过 4 次以上'),
)

# 改期之后对方的回应时长分档。和 `meeting_attendance` 的 `RESPONSE_BUCKETS`
# 刻意保持同一组上界——两边的响应时长必须是同一个尺度，运营才能把两个面板的
# 数字对上。改这里的任何一档，都要同时改那边。
RESPONSE_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (None, '3 天以上'),
)

# 一组订单至少要这么多条，占比和对照才给方向。和其余模块同一个量级。
MIN_MEASURED_ORDERS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
RESCHEDULE_LIST_LIMIT = 20

# 有过一条交付安排的订单状态。这份名单记录的是「一笔订单只要排过交付时间，它就
# 已经进过分母了」这个决策——分母由预约本身的存在性决定，不由订单状态决定。
# 连一次都没约上的订单不进分母：它谈不到改期，属于订单阶段流的领域，这里只报规模。
# 这里不做按状态的过滤，只把名单留在常量里：一旦有人想按订单状态筛分母，先看见
# 这份名单，再看见上面那段说明，就不会把它当成一次随手加的条件。
SCHEDULED_STATUSES = ('confirmed', 'meeting', 'completed', 'borrowed', 'returned', 'cancelled')


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

    第一档的上界写 1 而不是 ``None``：改期次数是整数，0 次要单独一档，
    写 ``None`` 会让第一档变成兜底档把所有次数都吸进去。
    """
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _fetch_appointments(*, created_after=None, created_before=None):
    """按显式排序取出预约，查询条件由调用方给。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。
    """
    appointments = MeetingAppointment.objects.select_related(
        'order', 'order__item', 'order__buyer', 'order__seller', 'location',
        'proposed_by', 'responded_by',
    ).order_by('created_at', 'id')
    if created_after is not None:
        appointments = appointments.filter(created_at__gte=created_after)
    if created_before is not None:
        appointments = appointments.filter(created_at__lte=created_before)
    return list(appointments)


def _reschedule_counts_by_order(orders):
    """每笔订单的改期次数，只认 `OrderEvent` 里那句备注的条数。

    这是本模块唯一能证明「改过」的证据，所以只能给下界。两条丢失要记住：
    `OrderEvent` 是后来才有的表，上线之前的改动没有痕迹；备注是自由文本，
    按子串匹配，那句话一旦被改，这里会静默少算。报出来的数是下界，不是精确值。
    """
    order_ids = [order.id for order in orders]
    if not order_ids:
        return {}
    counts = {}
    events = OrderEvent.objects.filter(
        order_id__in=order_ids, note__contains=RESCHEDULE_NOTE,
    ).values_list('order_id', flat=True)
    for order_id in events:
        counts[order_id] = counts.get(order_id, 0) + 1
    return counts


def _response_seconds(appointment):
    """从这一版提议交上去到对方回话的秒数，没回应返回 None。

    只认 `responded_at`：`updated_at` 也会在改期时被写，拿它算会让「多久回应」
    和「多久改主意」混在一起——那正是这个模块要分开的两件事。
    """
    if not appointment.responded_at or not appointment.created_at:
        return None
    return max((appointment.responded_at - appointment.created_at).total_seconds(), 0.0)


def _role_of(appointment, order):
    """这一版是谁提的。只分买家、卖家、看不出三种，不落到具体的人。"""
    if not appointment.proposed_by_id:
        return 'unknown'
    if appointment.proposed_by_id == order.buyer_id:
        return 'buyer'
    if appointment.proposed_by_id == order.seller_id:
        return 'seller'
    return 'unknown'


def _build_reschedule_stats(appointments, reschedule_counts):
    """一次安排改几次才定下来。"""
    bucket_counts = [0] * len(RESCHEDULE_BUCKETS)
    for appointment in appointments:
        count = reschedule_counts.get(appointment.order_id, 0)
        bucket_counts[_bucket_index(RESCHEDULE_BUCKETS, count)] += 1
    total = sum(bucket_counts)

    counts = [reschedule_counts.get(a.order_id, 0) for a in appointments]
    settled_once = sum(1 for count in counts if count == 0)
    changed_once = sum(1 for count in counts if count == 1)
    changed_thrice_or_more = sum(1 for count in counts if count >= 2)

    return {
        'total': total,
        'settled_once_count': settled_once,
        'settled_once_rate': _percent(settled_once, total),
        'changed_count': total - settled_once,
        'changed_rate': _percent(total - settled_once, total),
        'changed_once_count': changed_once,
        'changed_thrice_or_more_count': changed_thrice_or_more,
        'changed_thrice_or_more_rate': _percent(changed_thrice_or_more, total),
        'max_label': str(max(counts)) if counts else '—',
        'bucket_rows': [
            {
                'label': label,
                'count': count,
                'share': _percent(count, total),
            }
            for (limit, label), count in zip(RESCHEDULE_BUCKETS, bucket_counts)
        ],
        'is_small_sample': total < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_response_contrast(appointments, reschedule_counts):
    """改过的订单和一次谈成的订单，对方回应当初那一版各花了多久。

    和 `meeting_attendance` 的响应时长是同一个尺度、同一组上界，但问的不是
    一件事：那边问「总体上对方多久理」，这里问「改过的订单是不是更难谈」。
    两组各自过门槛、差值超过噪声带才给方向，否则只报数字。
    """
    groups = {
        'changed': {'label': '改过一次以上', 'seconds': []},
        'settled': {'label': '一次谈成', 'seconds': []},
    }
    for appointment in appointments:
        seconds = _response_seconds(appointment)
        if seconds is None:
            continue
        key = 'changed' if reschedule_counts.get(appointment.order_id, 0) else 'settled'
        groups[key]['seconds'].append(seconds)

    rows = []
    for key in ('changed', 'settled'):
        group = groups[key]
        seconds_list = group['seconds']
        buckets = [{'label': label, 'count': 0} for _limit, label in RESPONSE_BUCKETS]
        for seconds in seconds_list:
            buckets[_bucket_index(RESPONSE_BUCKETS, seconds)]['count'] += 1
        for bucket in buckets:
            bucket['share'] = _percent(bucket['count'], len(seconds_list))
        rows.append({
            'key': key,
            'label': group['label'],
            'total': len(seconds_list),
            'median_label': _format_duration(_median(seconds_list)),
            'bucket_rows': buckets,
        })

    changed_median = _median(groups['changed']['seconds'])
    settled_median = _median(groups['settled']['seconds'])
    both_measured = (
        len(groups['changed']['seconds']) >= MIN_MEASURED_ORDERS
        and len(groups['settled']['seconds']) >= MIN_MEASURED_ORDERS
    )
    gap_points = None
    if both_measured and settled_median:
        gap_points = round(
            (changed_median - settled_median) / settled_median * 100, 1,
        )
    return {
        'rows': rows,
        'changed_total': len(groups['changed']['seconds']),
        'settled_total': len(groups['settled']['seconds']),
        'changed_median_label': rows[0]['median_label'],
        'settled_median_label': rows[1]['median_label'],
        'gap_points': gap_points,
        'gap_is_meaningful': bool(
            gap_points is not None and abs(gap_points) >= NOISE_POINTS,
        ),
        'both_measured': both_measured,
        'is_small_sample': not both_measured,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'noise_points': NOISE_POINTS,
    }


def _build_location_shift_stats(appointments, reschedule_counts):
    """改期时地点换没换。

    只能和「当前这一版」比：前几版的地点被 `propose_meeting` 的 update 分支
    原地覆盖了，库里没有。所以这一节报的是「最后一次改期时，地点和现在这一版
    不一样」的订单占多少，而不是「地点改过几次」。这个上界是硬的——平台没有
    存过历史地点，报「改过几次地点」是编数据。

    订单上的 `meeting_location` 由 `propose_meeting` 同步写入，所以拿它当
    「当前这一版」的参照。两者都没有地点的订单，答不了换没换，单独数。
    """
    measurable = 0
    shifted = 0
    both_missing = 0
    for appointment in appointments:
        if not reschedule_counts.get(appointment.order_id, 0):
            continue
        order_location = getattr(appointment.order, 'meeting_location_id', None)
        appointment_location = appointment.location_id
        if not order_location or not appointment_location:
            both_missing += 1
            continue
        measurable += 1
        if order_location != appointment_location:
            shifted += 1
    return {
        'measurable_count': measurable,
        'shifted_count': shifted,
        'shift_rate': _percent(shifted, measurable),
        'both_missing_count': both_missing,
        'is_small_sample': measurable < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_role_stats(appointments, reschedule_counts):
    """谁在改期。只按角色分：买家提的、卖家提的、双方都提过。

    `proposed_by` 在改期时被改写，所以一笔订单改过两次以上时，库里只剩最后
    一版的提议人。这里能报的只是「最后一版是谁提的」，不是「谁提的次数多」——
    后者需要版本表，而平台没有。如实报前者，不给后者。
    """
    counts = {'buyer': 0, 'seller': 0, 'unknown': 0}
    for appointment in appointments:
        counts[_role_of(appointment, appointment.order)] += 1
    total = sum(counts.values())
    return {
        'buyer_count': counts['buyer'],
        'seller_count': counts['seller'],
        'unknown_count': counts['unknown'],
        'buyer_share': _percent(counts['buyer'], total),
        'seller_share': _percent(counts['seller'], total),
        'total': total,
    }


def _build_reschedule_rows(appointments, reschedule_counts):
    """改过两次以上的订单逐笔列出。只带订单号、商品、改了几次、当前版本归谁。"""
    rows = []
    for appointment in appointments:
        count = reschedule_counts.get(appointment.order_id, 0)
        if count < 2:
            continue
        rows.append({
            'order_id': appointment.order_id,
            'item_title': appointment.order.item.title if appointment.order.item_id else '',
            'count': count,
            'status_label': appointment.get_status_display(),
            'proposed_by_role': {
                'buyer': '买家', 'seller': '卖家', 'unknown': '—',
            }[_role_of(appointment, appointment.order)],
            'start_at': appointment.start_at,
        })
    rows.sort(key=lambda row: (-row['count'], row['order_id']))
    return rows[:RESCHEDULE_LIST_LIMIT]


def _build_summary(reschedule_stats, response_contrast, location_stats, role_stats):
    """一段话把这个模块的读数讲完，样本不足的地方明说。"""
    if not reschedule_stats['total']:
        return '当前周期还没有已安排的交付时间，改期统计暂无可核对的样本。'
    parts = [
        f"周期内 {reschedule_stats['total']} 笔订单排过交付时间，"
        f"{reschedule_stats['settled_once_count']} 笔一次谈成"
        f"（{reschedule_stats['settled_once_rate']}%）"
    ]
    if reschedule_stats['changed_count']:
        parts.append(
            f"，改过 {reschedule_stats['changed_count']} 笔，"
            f"其中改过两次以上 {reschedule_stats['changed_thrice_or_more_count']} 笔"
        )
    if response_contrast['gap_points'] is not None:
        parts.append(
            f"。改过的一次以上订单中位回应 {response_contrast['changed_median_label']}，"
            f"一次谈成的 {response_contrast['settled_median_label']}"
        )
        if response_contrast['gap_is_meaningful']:
            parts.append('，差值超过噪声阈值')
        else:
            parts.append('，两组差距在噪声范围内，只报数字')
    else:
        parts.append(
            f"。改期与回应对照至少各需 {response_contrast['min_measured_orders']} 条"
            f"已回应样本，目前不足，暂不给方向"
        )
    if location_stats['measurable_count']:
        parts.append(
            f"。改期时地点也换了的 {location_stats['shifted_count']} / "
            f"{location_stats['measurable_count']} 笔"
        )
    if reschedule_stats['is_small_sample']:
        parts.append(
            f"；不足 {reschedule_stats['min_measured_orders']} 笔已安排订单，"
            f"占比只作参考"
        )
    return ''.join(parts)


def build_meeting_reschedule(days=30, now=None):
    '''一次交付安排被改了几次才定下来，只报告，不改写任何字段。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    appointments = _fetch_appointments(created_after=start, created_before=now)
    orders = [appointment.order for appointment in appointments]
    reschedule_counts = _reschedule_counts_by_order(orders)

    reschedule_stats = _build_reschedule_stats(appointments, reschedule_counts)
    response_contrast = _build_response_contrast(appointments, reschedule_counts)
    location_stats = _build_location_shift_stats(appointments, reschedule_counts)
    role_stats = _build_role_stats(appointments, reschedule_counts)
    reschedule_rows = _build_reschedule_rows(appointments, reschedule_counts)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(appointments),
        'summary': _build_summary(
            reschedule_stats, response_contrast, location_stats, role_stats,
        ),
        'reschedule_stats': reschedule_stats,
        'response_contrast': response_contrast,
        'location_stats': location_stats,
        'role_stats': role_stats,
        'reschedule_rows': reschedule_rows,
        'reschedule_row_limit': RESCHEDULE_LIST_LIMIT,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'noise_points': NOISE_POINTS,
    }