# -*- coding: utf-8 -*-
"""交付安排事件链的写入与读取规则。"""

from django.utils import timezone

from .models import MeetingAppointment, MeetingAppointmentEvent


PROPOSAL_EVENT_TYPES = frozenset({'proposed', 'rescheduled'})
RESPONSE_EVENT_TYPES = frozenset({'accepted', 'declined'})
EVENT_TYPES = frozenset(
    value for value, _label in MeetingAppointmentEvent.EVENT_CHOICES
)
STATUSES = frozenset(value for value, _label in MeetingAppointment.STATUS_CHOICES)


def record_meeting_event(*, appointment, event_type, actor=None,
                         previous_status='', occurred_at=None):
    """在当前事务里写入一条安排事件，并快照当时的整次提议。

    调用方必须先保存预约，再调用本函数。提议、回应和订单取消与各自的
    状态更新共用一个事务；本函数不自行开事务，否则外层回滚时无法把事件
    一起撤回。
    """
    if appointment.pk is None:
        raise ValueError('交付安排必须先保存，才能记录事件。')
    if event_type not in EVENT_TYPES:
        raise ValueError(f'未知的交付安排事件类型：{event_type}')
    if previous_status not in STATUSES | {''}:
        raise ValueError(f'未知的交付安排状态：{previous_status}')
    if appointment.status not in STATUSES:
        raise ValueError(f'未知的交付安排状态：{appointment.status}')

    location_name = ''
    if appointment.location_id:
        location_name = appointment.location.name

    return MeetingAppointmentEvent.objects.create(
        order_id=appointment.order_id,
        appointment=appointment,
        event_type=event_type,
        occurred_at=occurred_at or timezone.now(),
        actor=actor,
        proposed_by_id=appointment.proposed_by_id,
        proposed_start_at=appointment.start_at,
        proposed_end_at=appointment.end_at,
        location_id=appointment.location_id,
        location_name=location_name,
        responded_by_id=appointment.responded_by_id,
        responded_at=appointment.responded_at,
        decline_reason=appointment.decline_reason,
        previous_status=previous_status,
        new_status=appointment.status,
    )


def load_meeting_events_by_order(order_ids):
    """按订单返回有序事件链；没有事件的订单不出现在映射里。"""
    order_ids = list(order_ids)
    if not order_ids:
        return {}
    events_by_order = {}
    events = MeetingAppointmentEvent.objects.filter(
        order_id__in=order_ids,
    ).select_related(
        'appointment', 'actor', 'proposed_by', 'location', 'responded_by',
    ).order_by('occurred_at', 'pk')
    for event in events:
        events_by_order.setdefault(event.order_id, []).append(event)
    return events_by_order


def has_complete_meeting_history(events):
    """只有从首次提议开始记录的链，才算上线后的完整样本。

    旧预约在上线后仍可能被改期，这时第一条事件是 ``rescheduled``；它证明后续
    动作真实存在，却缺少改期前的版本。把它算成完整历史会让首个改期凭空消失。
    """
    proposals = proposal_events(events)
    return bool(proposals and proposals[0].event_type == 'proposed')


def proposal_events(events):
    return [event for event in events if event.event_type in PROPOSAL_EVENT_TYPES]


def response_events(events):
    return [event for event in events if event.event_type in RESPONSE_EVENT_TYPES]


def proposal_response_pairs(events):
    """把每次提议和它之后的第一次接受/拒绝配对。

    改期后进入新的一轮；同一轮不会把第二次回应重复计入。停在 pending 的
    最新提议没有配对，正是尚未回应的样本。
    """
    pairs = []
    current_proposal = None
    for event in events:
        if event.event_type in PROPOSAL_EVENT_TYPES:
            current_proposal = event
        elif event.event_type in RESPONSE_EVENT_TYPES and current_proposal is not None:
            pairs.append((current_proposal, event))
            current_proposal = None
    return pairs


def first_response_seconds(events):
    """首次提议到首次回应；后续改期不会改写这个时长。"""
    pairs = proposal_response_pairs(events)
    if not pairs:
        return None
    proposal, response = pairs[0]
    return max((response.occurred_at - proposal.occurred_at).total_seconds(), 0.0)
