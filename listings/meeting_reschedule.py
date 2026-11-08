# -*- coding: utf-8 -*-
"""一次交付安排被改了几次才定下来。

``MeetingAppointment`` 仍是每笔订单的当前快照；上线后的首次提议、每次改期
以及接受/拒绝会写入不可变的 ``MeetingAppointmentEvent``。本模块只对从首次
提议开始记录的事件链给出精确改期次数，旧预约继续用 ``OrderEvent`` 备注做
兼容下界，并把两类样本分开披露。

它回答四件事：一次安排改几次才定下来、改期之后对方的回应是快还是慢、改期时
地点换没换、以及谁在改期（只按角色）。这些都是对照，不给因果。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .meeting_events import (
    first_response_seconds,
    has_complete_meeting_history,
    load_meeting_events_by_order,
    proposal_events,
)
from .models import MeetingAppointment, OrderEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 上线前的改期只能从这句订单事件备注反推。第一次提交写的是“提出了新的……”，
# 所以匹配必须包含“更新了”，否则首次提议也会被误算成改期。
RESCHEDULE_NOTE = '更新了交付时间安排'

RESCHEDULE_BUCKETS = (
    (1, '一次谈成'),
    (2, '改过一次'),
    (4, '改过 2-3 次'),
    (None, '改过 4 次以上'),
)

# 与 meeting_attendance 保持同一组分档上界。
RESPONSE_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (None, '3 天以上'),
)

MIN_MEASURED_ORDERS = 12
NOISE_POINTS = 8
RESCHEDULE_LIST_LIMIT = 20
SCHEDULED_STATUSES = ('confirmed', 'meeting', 'completed', 'borrowed', 'returned', 'cancelled')


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _period_start(days, today):
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} 秒'
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


def _bucket_index(buckets, value):
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or value < limit:
            return index
    return len(buckets) - 1


def _fetch_appointments(*, created_after=None, created_before=None):
    appointments = MeetingAppointment.objects.select_related(
        'order', 'order__item', 'order__buyer', 'order__seller', 'location',
        'proposed_by', 'responded_by',
    ).order_by('created_at', 'id')
    if created_after is not None:
        appointments = appointments.filter(created_at__gte=created_after)
    if created_before is not None:
        appointments = appointments.filter(created_at__lte=created_before)
    return list(appointments)


def _legacy_note_counts(orders):
    """只给没有完整事件链的订单取兼容下界。"""
    order_ids = [order.id for order in orders]
    if not order_ids:
        return {}
    counts = {}
    notes = OrderEvent.objects.filter(
        order_id__in=order_ids, note__contains=RESCHEDULE_NOTE,
    ).values_list('order_id', flat=True)
    for order_id in notes:
        counts[order_id] = counts.get(order_id, 0) + 1
    return counts


def _build_evidence(appointments, events_by_order):
    """逐单决定使用精确事件链还是旧备注下界，绝不在同一条链上混算。"""
    legacy_notes = _legacy_note_counts([a.order for a in appointments])
    evidence = {}
    for appointment in appointments:
        events = events_by_order.get(appointment.order_id, [])
        complete = has_complete_meeting_history(events)
        proposals = proposal_events(events)
        if complete:
            count = max(len(proposals) - 1, 0)
            source = 'events'
        else:
            count = legacy_notes.get(appointment.order_id, 0)
            source = 'legacy_note' if count else 'legacy_missing'
        evidence[appointment.order_id] = {
            'appointment': appointment,
            'events': events,
            'proposals': proposals,
            'is_complete': complete,
            'count': count,
            'source': source,
        }
    return evidence


def _response_seconds(evidence):
    """完整链取首次提议到首次回应；旧链退回当前快照的兼容口径。"""
    if evidence['is_complete']:
        return first_response_seconds(evidence['events'])
    appointment = evidence['appointment']
    if not appointment.responded_at or not appointment.created_at:
        return None
    return max((appointment.responded_at - appointment.created_at).total_seconds(), 0.0)


def _role_of_event(event, order):
    if not event.proposed_by_id:
        return 'unknown'
    if event.proposed_by_id == order.buyer_id:
        return 'buyer'
    if event.proposed_by_id == order.seller_id:
        return 'seller'
    return 'unknown'


def _role_of_appointment(appointment):
    if not appointment.proposed_by_id:
        return 'unknown'
    if appointment.proposed_by_id == appointment.order.buyer_id:
        return 'buyer'
    if appointment.proposed_by_id == appointment.order.seller_id:
        return 'seller'
    return 'unknown'


def _build_bucket_rows(counts):
    buckets = [0] * len(RESCHEDULE_BUCKETS)
    for count in counts:
        buckets[_bucket_index(RESCHEDULE_BUCKETS, count)] += 1
    total = len(counts)
    return [
        {
            'label': label,
            'count': count,
            'share': _percent(count, total),
        }
        for (_limit, label), count in zip(RESCHEDULE_BUCKETS, buckets)
    ]


def _build_reschedule_stats(evidence):
    exact = [item for item in evidence.values() if item['is_complete']]
    legacy = [item for item in evidence.values() if not item['is_complete']]
    exact_counts = [item['count'] for item in exact]
    legacy_counts = [item['count'] for item in legacy]
    exact_settled = sum(1 for count in exact_counts if count == 0)
    legacy_settled = sum(1 for count in legacy_counts if count == 0)
    exact_changed = len(exact_counts) - exact_settled
    legacy_changed = len(legacy_counts) - legacy_settled
    legacy_note_backed = sum(1 for item in legacy if item['source'] == 'legacy_note')
    legacy_missing = len(legacy) - legacy_note_backed

    return {
        'scheduled_total': len(evidence),
        # 兼容旧键名，但含义明确限定为精确事件链样本。
        'total': len(exact),
        'exact_total': len(exact),
        'legacy_total': len(legacy),
        'legacy_note_backed_count': legacy_note_backed,
        'legacy_missing_count': legacy_missing,
        'settled_once_count': exact_settled,
        'settled_once_rate': _percent(exact_settled, len(exact)),
        'changed_count': exact_changed,
        'changed_rate': _percent(exact_changed, len(exact)),
        'changed_once_count': sum(1 for count in exact_counts if count == 1),
        'changed_thrice_or_more_count': sum(1 for count in exact_counts if count >= 2),
        'changed_thrice_or_more_rate': _percent(
            sum(1 for count in exact_counts if count >= 2), len(exact),
        ),
        'max_label': str(max(exact_counts)) if exact_counts else '—',
        'bucket_rows': _build_bucket_rows(exact_counts),
        'legacy_settled_once_count': legacy_settled,
        'legacy_settled_once_rate': _percent(legacy_settled, len(legacy)),
        'legacy_changed_count': legacy_changed,
        'legacy_changed_rate': _percent(legacy_changed, len(legacy)),
        'legacy_max_label': str(max(legacy_counts)) if legacy_counts else '—',
        'legacy_bucket_rows': _build_bucket_rows(legacy_counts),
        'is_small_sample': len(exact) < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
    }


def _build_response_contrast(evidence):
    """只在完整事件链上做对照，旧快照不混进方向计算。"""
    groups = {
        'changed': {'label': '改过一次以上', 'seconds': []},
        'settled': {'label': '一次谈成', 'seconds': []},
    }
    for item in evidence.values():
        if not item['is_complete']:
            continue
        seconds = _response_seconds(item)
        if seconds is None:
            continue
        key = 'changed' if item['count'] else 'settled'
        groups[key]['seconds'].append(seconds)

    rows = []
    for key in ('changed', 'settled'):
        seconds_list = groups[key]['seconds']
        buckets = [{'label': label, 'count': 0} for _limit, label in RESPONSE_BUCKETS]
        for seconds in seconds_list:
            buckets[_bucket_index(RESPONSE_BUCKETS, seconds)]['count'] += 1
        for bucket in buckets:
            bucket['share'] = _percent(bucket['count'], len(seconds_list))
        rows.append({
            'key': key,
            'label': groups[key]['label'],
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
    legacy_measured = sum(
        1 for item in evidence.values()
        if not item['is_complete'] and _response_seconds(item) is not None
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
        'legacy_measured_count': legacy_measured,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'noise_points': NOISE_POINTS,
    }


def _location_key(event):
    if event.location_id:
        return ('id', event.location_id)
    name = (event.location_name or '').strip()
    return ('name', name) if name else None


def _build_location_shift_stats(evidence):
    """完整链逐轮比较相邻提议；地点名快照兜住地点被删除的情况。"""
    measurable = 0
    shifted = 0
    unmeasurable = 0
    rescheduled_orders = 0
    shifted_orders = 0
    for item in evidence.values():
        if not item['is_complete'] or item['count'] == 0:
            continue
        rescheduled_orders += 1
        order_shifted = False
        proposals = item['proposals']
        for previous, current in zip(proposals, proposals[1:]):
            previous_key = _location_key(previous)
            current_key = _location_key(current)
            if previous_key is None or current_key is None:
                unmeasurable += 1
                continue
            measurable += 1
            if previous_key != current_key:
                shifted += 1
                order_shifted = True
        if order_shifted:
            shifted_orders += 1
    return {
        'measurable_count': measurable,
        'shifted_count': shifted,
        'shift_rate': _percent(shifted, measurable),
        'unmeasurable_count': unmeasurable,
        'rescheduled_order_count': rescheduled_orders,
        'orders_with_shift_count': shifted_orders,
        'is_small_sample': measurable < MIN_MEASURED_ORDERS,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'legacy_comparable_count': 0,
    }


def _build_role_stats(evidence):
    """完整链精确数每一次改期由哪个角色提出，不输出用户名。"""
    counts = {'buyer': 0, 'seller': 0, 'unknown': 0}
    for item in evidence.values():
        if not item['is_complete']:
            continue
        for event in item['events']:
            if event.event_type == 'rescheduled':
                counts[_role_of_event(event, item['appointment'].order)] += 1
    total = sum(counts.values())
    return {
        'buyer_count': counts['buyer'],
        'seller_count': counts['seller'],
        'unknown_count': counts['unknown'],
        'buyer_share': _percent(counts['buyer'], total),
        'seller_share': _percent(counts['seller'], total),
        'total': total,
    }


def _build_legacy_role_stats(evidence):
    """旧快照只能说明最后一版归谁，单独披露，不冒充历史次数。"""
    counts = {'buyer': 0, 'seller': 0, 'unknown': 0}
    for item in evidence.values():
        if item['is_complete']:
            continue
        counts[_role_of_appointment(item['appointment'])] += 1
    total = sum(counts.values())
    return {
        'buyer_count': counts['buyer'],
        'seller_count': counts['seller'],
        'unknown_count': counts['unknown'],
        'buyer_share': _percent(counts['buyer'], total),
        'seller_share': _percent(counts['seller'], total),
        'total': total,
    }


def _build_reschedule_rows(evidence):
    rows = []
    role_labels = {'buyer': '买家', 'seller': '卖家', 'unknown': '—'}
    for item in evidence.values():
        if item['count'] < 2:
            continue
        appointment = item['appointment']
        if item['is_complete'] and item['proposals']:
            role = _role_of_event(item['proposals'][-1], appointment.order)
        else:
            role = _role_of_appointment(appointment)
        rows.append({
            'order_id': appointment.order_id,
            'item_title': appointment.order.item.title if appointment.order.item_id else '',
            'count': item['count'],
            'count_label': str(item['count']) if item['is_complete'] else f'至少 {item["count"]}',
            'status_label': appointment.get_status_display(),
            'proposed_by_role': role_labels[role],
            'history_label': '完整事件链' if item['is_complete'] else '旧备注下界',
            'start_at': appointment.start_at,
        })
    rows.sort(key=lambda row: (-row['count'], row['order_id']))
    return rows[:RESCHEDULE_LIST_LIMIT]


def _build_summary(reschedule_stats, response_contrast, location_stats):
    if not reschedule_stats['scheduled_total']:
        return '当前周期还没有已安排的交付时间，改期统计暂无可核对的样本。'
    parts = [
        f"周期内 {reschedule_stats['scheduled_total']} 笔订单排过交付时间；"
        f"其中完整事件链 {reschedule_stats['exact_total']} 笔，"
        f"旧快照 {reschedule_stats['legacy_total']} 笔"
    ]
    if reschedule_stats['exact_total']:
        parts.append(
            f"。完整链中 {reschedule_stats['settled_once_count']} 笔一次谈成"
            f"（{reschedule_stats['settled_once_rate']}%），"
            f"{reschedule_stats['changed_count']} 笔改过"
        )
    if reschedule_stats['legacy_total']:
        parts.append(
            f"。旧快照中 {reschedule_stats['legacy_note_backed_count']} 笔能从备注确认改过，"
            f"{reschedule_stats['legacy_missing_count']} 笔没有可核对的改期痕迹"
        )
    if response_contrast['both_measured']:
        parts.append(
            f"。完整链中改过订单的中位首次回应 "
            f"{response_contrast['changed_median_label']}，一次谈成为 "
            f"{response_contrast['settled_median_label']}"
        )
    elif reschedule_stats['exact_total']:
        parts.append(
            f"。完整链的改期与回应对照还不足各 {response_contrast['min_measured_orders']} 条，"
            f"暂不给方向"
        )
    if location_stats['rescheduled_order_count']:
        parts.append(
            f"。逐轮比较后，地点发生变化的提议轮次 "
            f"{location_stats['shifted_count']} / {location_stats['measurable_count']}"
        )
    if reschedule_stats['is_small_sample']:
        parts.append(
            f"；完整事件链不足 {reschedule_stats['min_measured_orders']} 笔，"
            f"占比只作参考"
        )
    return ''.join(parts)


def build_meeting_reschedule(days=30, now=None):
    """生成改期报告；只读事件与当前快照，不改写任何业务字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    appointments = _fetch_appointments(created_after=start, created_before=now)
    events_by_order = load_meeting_events_by_order(a.order_id for a in appointments)
    evidence = _build_evidence(appointments, events_by_order)

    reschedule_stats = _build_reschedule_stats(evidence)
    response_contrast = _build_response_contrast(evidence)
    location_stats = _build_location_shift_stats(evidence)
    role_stats = _build_role_stats(evidence)
    legacy_role_stats = _build_legacy_role_stats(evidence)
    reschedule_rows = _build_reschedule_rows(evidence)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(appointments),
        'summary': _build_summary(reschedule_stats, response_contrast, location_stats),
        'reschedule_stats': reschedule_stats,
        'response_contrast': response_contrast,
        'location_stats': location_stats,
        'role_stats': role_stats,
        'legacy_role_stats': legacy_role_stats,
        'reschedule_rows': reschedule_rows,
        'reschedule_row_limit': RESCHEDULE_LIST_LIMIT,
        'min_measured_orders': MIN_MEASURED_ORDERS,
        'noise_points': NOISE_POINTS,
    }