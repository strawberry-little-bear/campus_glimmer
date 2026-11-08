# -*- coding: utf-8 -*-
"""提醒长时间无人回应的交付安排。

提醒必须由完整事件链证明，而不是只读 ``MeetingAppointment.status``。旧预约没有
事件，旧数据上线后首次改期也可能从 ``rescheduled`` 开始；这两种情况既无法证明
当前提议何时提出，也无法证明谁应回应，因此一律跳过。任务只发送站内通知，不自动
接受、拒绝、改期或推进订单。
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .meeting_events import (
    RESPONSE_EVENT_TYPES,
    has_complete_meeting_history,
)
from .models import (
    MeetingAppointment,
    Notification,
    NotificationPreference,
    Order,
)
from .notifications import create_notification


logger = logging.getLogger(__name__)

DEFAULT_RESPONSE_HOURS = 24
NOTIFICATION_KIND = 'meeting_response_pending'
ELIGIBLE_ORDER_STATUSES = frozenset({'confirmed', 'meeting'})


def _latest_unanswered_proposal(events, appointment):
    """Return the latest proposal, only when it still describes this row."""
    if not has_complete_meeting_history(events):
        return None

    latest_index = None
    for index, event in enumerate(events):
        if event.event_type in {'proposed', 'rescheduled'}:
            latest_index = index
    if latest_index is None:
        return None

    latest = events[latest_index]
    if event_after_latest := events[latest_index + 1:]:
        if any(
            event.event_type in RESPONSE_EVENT_TYPES
            or event.event_type == 'cancelled'
            for event in event_after_latest
        ):
            return None

    # Current appointment state is a second proof. A complete chain that no
    # longer matches the current row must not be used to remind the wrong party.
    if (
        latest.appointment_id != appointment.pk
        or latest.proposed_by_id is None
        or latest.proposed_by_id != appointment.proposed_by_id
        or latest.proposed_start_at != appointment.start_at
        or latest.proposed_end_at != appointment.end_at
        or latest.location_id != appointment.location_id
        or appointment.responded_by_id is not None
        or appointment.responded_at is not None
    ):
        return None
    return latest


def _preference_disabled(recipient_id):
    """Read the switch without creating a preference row during a dry run."""
    preference = NotificationPreference.objects.filter(
        user_id=recipient_id,
    ).only(NOTIFICATION_KIND).first()
    return bool(preference and not getattr(preference, NOTIFICATION_KIND))


def _process_candidate(*, appointment_id, order_id, cutoff, dry_run):
    """Revalidate one candidate under the same locks used by meeting writes."""
    with transaction.atomic():
        # Every meeting write locks the order first. Following that order here
        # keeps the scheduled worker from deadlocking with proposal/response.
        order = Order.objects.select_for_update(of=('self',)).select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order_id)
        try:
            appointment = MeetingAppointment.objects.select_for_update(
                of=('self',),
            ).get(pk=appointment_id, order_id=order_id)
        except MeetingAppointment.DoesNotExist:
            return 'skipped'

        if order.status not in ELIGIBLE_ORDER_STATUSES or appointment.status != 'pending':
            return 'skipped'

        events = list(
            appointment.events.order_by('occurred_at', 'pk').select_related('proposed_by')
        )
        proposal = _latest_unanswered_proposal(events, appointment)
        if proposal is None or proposal.occurred_at > cutoff:
            return 'skipped'

        proposer = proposal.proposed_by
        if proposer is None or proposer.pk not in {order.buyer_id, order.seller_id}:
            return 'skipped'
        recipient = order.seller if proposer.pk == order.buyer_id else order.buyer
        if _preference_disabled(recipient.pk):
            return 'skipped'

        dedupe_key = f'meeting-response:{proposal.pk}'
        already_reminded = Notification.objects.filter(
            recipient_id=recipient.pk,
            kind=NOTIFICATION_KIND,
            dedupe_key=dedupe_key,
        ).exists()
        if already_reminded:
            return 'reminded'

        # A dry run reports what would be sent but writes neither notification
        # nor preference rows.
        if dry_run:
            return 'sent'

        notification = create_notification(
            recipient,
            actor=proposer,
            kind=NOTIFICATION_KIND,
            title='交付安排等待你回应',
            message=(
                f'对方为商品“{order.item.title}”提出了交付时间安排，'
                '已等待一段时间，请查看并回应。'
            ),
            order=order,
            item=order.item,
            target_url=reverse('order_detail', args=[order.id]),
            dedupe_key=dedupe_key,
            dedupe_forever=True,
        )
        return 'sent' if notification else 'skipped'


def send_meeting_response_reminders(*, response_hours=DEFAULT_RESPONSE_HOURS,
                                    now=None, dry_run=False):
    """Send at most one reminder for each unanswered proposal.

    ``sent`` counts reminders that were written, or would be written in a dry
    run. ``reminded`` counts due proposals whose reminder already exists, and
    ``skipped`` covers every candidate invalidated by current state, legacy
    history, a changed preference, or deleted participant data.
    """
    if isinstance(response_hours, bool) or not isinstance(response_hours, int):
        raise ValueError('response_hours 必须是正整数。')
    if response_hours <= 0:
        raise ValueError('response_hours 必须大于 0。')

    now = now or timezone.now()
    cutoff = now - timedelta(hours=response_hours)
    candidates = list(
        MeetingAppointment.objects.filter(
            status='pending',
            order__status__in=ELIGIBLE_ORDER_STATUSES,
        ).order_by('pk').values_list('pk', 'order_id')
    )
    result = {
        'candidates': len(candidates),
        'sent': 0,
        'skipped': 0,
        'reminded': 0,
        'failed': 0,
        'response_hours': response_hours,
    }

    for appointment_id, order_id in candidates:
        try:
            outcome = _process_candidate(
                appointment_id=appointment_id,
                order_id=order_id,
                cutoff=cutoff,
                dry_run=dry_run,
            )
        except Exception:
            # One malformed row or delivery failure must not stop the rest of
            # the batch. The aggregate failure count goes into the task ledger.
            logger.exception(
                '发送交付安排待回应提醒失败：appointment=%s order=%s',
                appointment_id,
                order_id,
            )
            result['failed'] += 1
            continue
        result[outcome] += 1

    return result
