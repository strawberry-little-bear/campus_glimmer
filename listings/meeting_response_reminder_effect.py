# -*- coding: utf-8 -*-
"""观察交付安排待回应提醒发出后的阅读与协商动作。

台账里的每一条 MeetingResponseReminderEvent 都是一个稳定的发送样本。提醒
发出后，最值得回看的不是“系统又发了多少”，而是接收人是否读过，以及同一提议
回合随后是否出现接受、拒绝或再次改期。

四条边界固定了这个面板能说什么：

1. 发送分母只来自周期内的台账。旧通知没有台账时单列为未计入，不从去重键
   倒推一个看似完整的样本。
2. 阅读只有 Notification.is_read 与 read_at 同时成立才算已读；通知被删除后，
   发送事实仍在台账里，但阅读状态明确记为不可观测。
3. 回应只取提醒时间之后、同一提议回合里最早的一次接受、拒绝或改期。新提议
   开始下一轮，不把后一轮的动作算回上一轮。
4. 固定观察 72 小时。窗口未结束时仍未回应的是删失样本，不能写成“没有回应”；
   超过窗口后才出现的动作单列为晚回应，不进入窗口内回应计数；它在窗口内仍按未回应计入已过窗分母。

这里只报告观察值，不宣称提醒导致了动作，也不据此自动接受、拒绝、改期、
提升提醒频率或给接收人排名。
"""

from datetime import timedelta
from statistics import median

from django.db.models import Q
from django.utils import timezone

from .meeting_response_reminders import NOTIFICATION_KIND
from .models import (
    MeetingAppointmentEvent,
    MeetingResponseReminderEvent,
    Notification,
)


EFFECT_WINDOW_HOURS = 72
MIN_SEGMENT_SAMPLES = 12
ACTION_EVENT_TYPES = frozenset({'accepted', 'declined', 'rescheduled'})
ROLE_LABELS = (('buyer', '买家'), ('seller', '卖家'))
ACTION_LABELS = {
    'accepted': '接受安排',
    'declined': '拒绝安排',
    'rescheduled': '再次改期',
}
LATENCY_BUCKETS = (
    ('within_1h', '1 小时内', 1),
    ('within_6h', '1–6 小时', 6),
    ('within_24h', '6–24 小时', 24),
    ('within_window', '24–72 小时', EFFECT_WINDOW_HOURS),
)


def _period_bounds(days, now):
    """按本地日期切出与运营看板其它面板一致的周期。"""
    today = timezone.localdate(now)
    start_date = today - timedelta(days=days - 1)
    start = timezone.make_aware(
        timezone.datetime.combine(start_date, timezone.datetime.min.time()),
    )
    return start, start_date


def _percent(value, total):
    if not total:
        return None
    return round(value * 100.0 / total, 1)


def _label_hours(hours):
    if hours is None:
        return '—'
    if hours < 1:
        return '%.1f 分钟' % (hours * 60)
    return '%.1f 小时' % hours


def _load_events(reminders):
    """一次取回所有相关订单的事件链，避免按提醒逐条查询。"""
    order_ids = {reminder.proposal_event.order_id for reminder in reminders}
    if not order_ids:
        return {}
    events = MeetingAppointmentEvent.objects.filter(
        order_id__in=order_ids,
    ).order_by('order_id', 'occurred_at', 'pk')
    grouped = {}
    for event in events:
        grouped.setdefault(event.order_id, []).append(event)
    return grouped


def _first_action_after(events, proposal, sent_at):
    """返回提醒之后、同一提议回合里的首个有效动作。

    只有在完整链中仍由这条提议开启的回合才能回答“后来发生了什么”。事件表
    中看不到更早的提议时，本函数不替旧数据补造历史。遇到下一版提议、取消或
    更早的回应就停止，避免把下一轮动作错记到这一轮。
    """
    if not proposal.appointment_id:
        return None
    try:
        proposal_index = next(
            index for index, event in enumerate(events) if event.pk == proposal.pk
        )
    except StopIteration:
        return None

    for event in events[proposal_index + 1:]:
        if event.appointment_id != proposal.appointment_id:
            continue
        if event.event_type in {'proposed', 'cancelled'}:
            break
        if event.event_type in ACTION_EVENT_TYPES:
            if event.occurred_at < sent_at:
                return None
            return event
    return None


def _notice_state(reminder):
    notification = reminder.notification
    if notification is None:
        return 'missing'
    if notification.is_read and notification.read_at is not None:
        return 'read'
    return 'unread'


def _latency_bucket(hours):
    lower = 0.0
    for key, _label, upper in LATENCY_BUCKETS:
        if lower < hours <= upper:
            return key
        lower = upper
    return None


def _build_segment(key, label, observations):
    sent = len(observations)
    available = sum(row['notice_state'] != 'missing' for row in observations)
    read = sum(row['notice_state'] == 'read' for row in observations)
    missing = sent - available
    accepted = sum(
        row['action_type'] == 'accepted' and row['within_window']
        for row in observations
    )
    declined = sum(
        row['action_type'] == 'declined' and row['within_window']
        for row in observations
    )
    rescheduled = sum(
        row['action_type'] == 'rescheduled' and row['within_window']
        for row in observations
    )
    responded = accepted + declined + rescheduled
    late = sum(
        row['action_type'] is not None and not row['within_window']
        for row in observations
    )
    open_samples = sum(row['status'] == 'open' for row in observations)
    no_action = sum(row['status'] == 'no_action' for row in observations)
    closed = responded + no_action + late
    latencies = [
        row['response_hours'] for row in observations
        if row['within_window'] and row['response_hours'] is not None
    ]
    return {
        'key': key,
        'label': label,
        'sent_count': sent,
        'notice_available_count': available,
        'read_notice_count': read,
        'missing_notice_count': missing,
        'notice_read_rate': _percent(read, available),
        'responded_count': responded,
        'accepted_count': accepted,
        'declined_count': declined,
        'rescheduled_count': rescheduled,
        'closed_count': closed,
        'response_rate': _percent(responded, closed),
        'open_count': open_samples,
        'no_action_count': no_action,
        'late_action_count': late,
        'median_response_hours': round(median(latencies), 1) if latencies else None,
        'median_response_label': _label_hours(median(latencies)) if latencies else '—',
        'comparable': closed >= MIN_SEGMENT_SAMPLES,
    }


def _build_latency_rows(observations):
    counts = {key: 0 for key, _label, _upper in LATENCY_BUCKETS}
    for row in observations:
        if not row['within_window'] or row['response_hours'] is None:
            continue
        bucket = _latency_bucket(row['response_hours'])
        if bucket:
            counts[bucket] += 1
    total = sum(counts.values())
    return [
        {
            'key': key,
            'label': label,
            'count': counts[key],
            'share': _percent(counts[key], total),
        }
        for key, label, _upper in LATENCY_BUCKETS
    ]


def build_meeting_response_reminder_effect(*, days=30, now=None):
    """构建页面与 CSV 共用的只读提醒效果面板。"""
    now = now or timezone.now()
    start, start_date = _period_bounds(days, now)
    window_ends_before = now - timedelta(hours=EFFECT_WINDOW_HOURS)
    reminders = list(
        MeetingResponseReminderEvent.objects.filter(
            sent_at__gte=start, sent_at__lte=now,
        ).select_related('proposal_event', 'notification').order_by('sent_at', 'pk')
    )
    events_by_order = _load_events(reminders)

    observations = []
    for reminder in reminders:
        action = _first_action_after(
            events_by_order.get(reminder.proposal_event.order_id, ()),
            reminder.proposal_event,
            reminder.sent_at,
        )
        response_hours = None
        action_type = None
        within_window = False
        if action is not None:
            action_type = action.event_type
            response_hours = max(
                (action.occurred_at - reminder.sent_at).total_seconds() / 3600.0,
                0.0,
            )
            within_window = response_hours <= EFFECT_WINDOW_HOURS

        if within_window:
            status = 'responded'
        elif action is not None:
            status = 'late_action'
        elif reminder.sent_at <= window_ends_before:
            status = 'no_action'
        else:
            status = 'open'

        observations.append({
            'reminder': reminder,
            'role': reminder.recipient_role,
            'notice_state': _notice_state(reminder),
            'action_type': action_type,
            'response_hours': response_hours,
            'within_window': within_window,
            'status': status,
        })

    all_segment = _build_segment('all', '全部', observations)
    role_rows = [
        _build_segment(role, label, [row for row in observations if row['role'] == role])
        for role, label in ROLE_LABELS
    ]
    latency_rows = _build_latency_rows(observations)

    linked_notification_ids = [
        reminder.notification_id for reminder in reminders
        if reminder.notification_id is not None
    ]
    legacy_notices = Notification.objects.filter(
        kind=NOTIFICATION_KIND,
        created_at__gte=start,
        created_at__lte=now,
    )
    if linked_notification_ids:
        legacy_notices = legacy_notices.exclude(pk__in=linked_notification_ids)
    legacy_notice_count = legacy_notices.count()

    if not reminders:
        summary = (
            '周期内还没有交付安排待回应提醒台账。旧通知不会按去重键补造发送样本；'
            '新提醒发送后才会进入这里。'
        )
    elif all_segment['closed_count'] < MIN_SEGMENT_SAMPLES:
        summary = (
            '已过观察窗的样本少于 %d 条，只报告计数与时延，不判断提醒效果方向。'
            % MIN_SEGMENT_SAMPLES
        )
    else:
        summary = (
            '已过观察窗的 %d 条提醒中，%d 条在 72 小时内出现同一提议回合的后续动作；'
            '这是观察值，不表示动作由提醒引起。'
            % (all_segment['closed_count'], all_segment['responded_count'])
        )

    return {
        'has_data': bool(reminders),
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'period_start_date': start_date,
        'window_hours': EFFECT_WINDOW_HOURS,
        'window_ends_before': window_ends_before,
        'min_samples': MIN_SEGMENT_SAMPLES,
        'summary': summary,
        'sample_note': (
            '发送分母来自周期内不可变提醒台账；'
            '窗口回应率 = 72 小时内出现接受、拒绝或改期的关闭样本 ÷ 已过观察窗样本。'
        ),
        'source_note': (
            '旧通知缺少台账时不计入效果分母，单列为未计入；通知删除后仍保留发送样本，'
            '但阅读状态记为不可观测。'
        ),
        'causality_note': (
            '后续动作只说明发生在提醒之后，不能证明由提醒引起；本面板不自动推进预约、'
            '不提高发送频率，也不按用户或角色排名。'
        ),
        'action_labels': ACTION_LABELS,
        'sent_count': all_segment['sent_count'],
        'notice_available_count': all_segment['notice_available_count'],
        'read_notice_count': all_segment['read_notice_count'],
        'notice_read_rate': all_segment['notice_read_rate'],
        'missing_notice_count': all_segment['missing_notice_count'],
        'closed_count': all_segment['closed_count'],
        'responded_count': all_segment['responded_count'],
        'accepted_count': all_segment['accepted_count'],
        'declined_count': all_segment['declined_count'],
        'rescheduled_count': all_segment['rescheduled_count'],
        'response_rate': all_segment['response_rate'],
        'open_count': all_segment['open_count'],
        'no_action_count': all_segment['no_action_count'],
        'late_action_count': all_segment['late_action_count'],
        'median_response_hours': all_segment['median_response_hours'],
        'median_response_label': all_segment['median_response_label'],
        'comparable': all_segment['comparable'],
        'role_rows': role_rows,
        'latency_rows': latency_rows,
        'legacy_notice_count': legacy_notice_count,
    }
