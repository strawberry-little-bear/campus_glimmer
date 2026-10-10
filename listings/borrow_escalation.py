# -*- coding: utf-8 -*-
"""Escalate overdue borrows in steps instead of notifying once and going quiet.

``process_borrow_due_notifications`` sends one overdue notice per order and
writes a timestamp so it can never repeat. That is the whole story today: a
student who keeps a classmate's power bank for six weeks gets no further
contact from the platform, the lender is left to argue over private messages,
and by the time it becomes a dispute an operator has no timeline to look at.

Escalation is deliberately a ladder rather than a loop. Each rung is a different
audience, because re-notifying the same person is the one move that adds no
information: level one asks the borrower, level two tells both sides the
platform is recording it, level three hands the case to the governance queue.
Once the borrower has been asked and ignored them, the only remaining move is to
leave the private channel - so the ladder is capped at three by design, not by
a missing feature.

What this module must never do is decide the outcome. It does not cancel the
order, it does not touch the deposit, and it does not mark anyone as having
wronged anyone. The deposit is settled offline between the two students; a
platform-side deduction would invent a financial relationship they never agreed
to, and a "guilty" flag would turn a slow return into a permanent reputation
wound that outlives the item. The ladder records what happened and who was told,
and leaves the judgement to the governance queue, which already exists and
already has an SLA.
"""

from datetime import timedelta

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import BorrowReturnEscalation, Order
from .notifications import create_notification


# How long after the due date each rung opens. Level one is the "you forgot
# about it" nudge and waits a day; level three means a week has passed and the
# private channel has demonstrably failed.
ESCALATION_SCHEDULE = {
    1: timedelta(days=1),
    2: timedelta(days=3),
    3: timedelta(days=7),
}

# Minimum spacing between two escalations of the same order. A borrower should
# not be chased twice in one evening, and an operator should not be handed a
# case that was created a minute ago.
MIN_ESCALATION_INTERVAL = timedelta(hours=24)

# Where the ladder stops. Reaching the cap is a statement that the private
# channel is exhausted, not that the platform has run out of ideas.
MAX_ESCALATION_LEVEL = 3

DEFAULT_LOOKBACK_DAYS = 120


LEVEL_NOTES = {
    1: "已提醒借用方尽快归还，并说明逾期会影响押金与信誉",
    2: "已同时通知借用方与出借方，平台已开始记录这笔逾期",
    3: "已上报治理工作台，交由运营跟进处理",
}


def _due_for_level(order, level, *, now):
    """Whether this order has been overdue long enough to reach a level."""
    if order.status != 'borrowed' or not order.return_due_at:
        return False
    if order.return_due_at > now:
        return False
    return now - order.return_due_at >= ESCALATION_SCHEDULE[level]


def _target_level(order, escalation, *, now):
    """The highest rung this order currently qualifies for.

    The level is derived from how long the item has been overdue, not from how
    many escalations happened. A missed run therefore cannot leave an order
    permanently one rung behind: the next run simply catches up to where the
    Rungs that were skipped are not replayed. An order found three days overdue
    receives the level-two message, not yesterday's level-one message followed by
    today's: catching up on three messages at once is the exact noise the ladder
    exists to avoid, and the wider audience is the one that still needs telling.
    """
    if escalation is not None and escalation.is_resolved:
        return 0
    level = escalation.escalation_level if escalation else 0
    for candidate in sorted(ESCALATION_SCHEDULE, reverse=True):
        if _due_for_level(order, candidate, now=now):
            return max(level, candidate)
    return level


def _notify_level_one(order, escalation, *, now):
    """Ask the borrower directly, and say what the overdue costs them.

    Only the borrower is contacted. Telling the lender at this point would be
    noise - they already know the item is theirs and is late.
    """
    hours_overdue = round((now - order.return_due_at).total_seconds() / 3600)
    message = (
        f'商品"{order.item.title}"已超过预计归还时间约 {hours_overdue} 小时。'
        f'平台会继续记录这笔借用，归还确认后记录即关闭。'
    )
    create_notification(
        order.buyer,
        kind='order_expired',
        title='借用已逾期，请尽快归还',
        message=message,
        order=order,
        item=order.item,
        target_url=reverse('order_detail', args=[order.id]),
    )


def _notify_level_two(order, escalation, *, now):
    """Tell both sides the platform is now keeping a record.

    The lender is brought in here, not earlier. Until now the situation was a
    private arrangement that had slipped; from this point it is something the
    platform is tracking, and the lender deserves to know that before the case
    can reach an operator.
    """
    days_overdue = max(1, round((now - order.return_due_at).total_seconds() / 86400))
    for recipient in (order.buyer, order.seller):
        create_notification(
            recipient,
            kind='order_expired',
            title='借用逾期已进入平台记录',
            message=(
                f'商品"{order.item.title}"已逾期约 {days_overdue} 天，'
                f'平台已将这笔借用纳入跟进记录，请双方尽快沟通完成归还。'
            ),
            order=order,
            item=order.item,
            target_url=reverse('order_detail', args=[order.id]),
        )


def _notify_level_three(order, escalation, *, now):
    """Hand the case to the governance queue.

    Level three does not send a third private message. It surfaces the order in
    the governance workbench, where a staff member can act on it under the same
    SLA that governs reports, disputes and delivery incidents.
    """
    days_overdue = max(1, round((now - order.return_due_at).total_seconds() / 86400))
    escalation.note = LEVEL_NOTES[3]
    escalation.save(update_fields=['note', 'updated_at'])
    # 治理工作台按 EscalationLevel >= 3 读取待处理清单，这里不再额外落一张队列表，
    # 避免出现第二个需要同步的写入点。
    _ = days_overdue


def _apply_level(order, escalation, level, *, now):
    # 跳级时不回播被跳过的那一级，但也不能因此让某一方从头到尾被蒙在鼓里：
    # 借出方在第二级才第一次被告知，直接跳到第三级必须补上这一声。
    if level >= 2 and escalation.escalation_level < 2:
        _notify_level_two(order, escalation, now=now)
    if level == 1:
        _notify_level_one(order, escalation, now=now)
    elif level == 3:
        _notify_level_three(order, escalation, now=now)
    escalation.escalation_level = level
    escalation.last_escalated_at = now
    escalation.escalation_count += 1
    escalation.note = LEVEL_NOTES[level]
    escalation.save(update_fields=[
        'escalation_level', 'last_escalated_at', 'escalation_count',
        'note', 'updated_at',
    ])


def escalate_overdue_borrows(*, now=None, lookback_days=DEFAULT_LOOKBACK_DAYS):
    """Walk every overdue borrow up the escalation ladder.

    Orders are picked up in overdue order, so the longest-running case is
    escalated first. That is not a nicety: the queue is small by nature, and if
    a run is ever truncated the rows that matter most were handled first.
    """
    now = now or timezone.now()
    lookback_days = max(1, int(lookback_days))
    start = now - timedelta(days=lookback_days)

    escalated = 0
    resolved = 0
    by_level = {1: 0, 2: 0, 3: 0}

    orders = list(
        Order.objects.select_related('item', 'buyer', 'seller').filter(
            status='borrowed', return_due_at__isnull=False, return_due_at__lte=now,
            created_at__gte=start,
        ).order_by('return_due_at')
    )
    for order in orders:
        with transaction.atomic():
            escalation = BorrowReturnEscalation.objects.select_for_update().filter(
                order=order,
            ).first()
            if escalation is not None and escalation.is_resolved:
                continue
            target = _target_level(order, escalation, now=now)
            if target <= (escalation.escalation_level if escalation else 0):
                continue
            # 同一笔订单两次升级之间至少隔一天，避免催收变成骚扰。
            last_escalated_at = escalation.last_escalated_at if escalation else None
            if (
                last_escalated_at
                and now - last_escalated_at < MIN_ESCALATION_INTERVAL
            ):
                continue
            if escalation is None:
                # The row is written when the order reaches a rung, not when it
                # merely crosses the deadline. A level-zero row would count as
                # open work on the board for an order nobody has chased yet.
                escalation = BorrowReturnEscalation.objects.create(order=order)
            _apply_level(order, escalation, target, now=now)
            by_level[target] = by_level.get(target, 0) + 1
            escalated += 1

    # A borrow that left the borrowed state without the hook firing - a return
    # confirmed from the admin, or a dispute that ended the cycle - still has to
    # close its row, or the ladder keeps counting work that no longer exists.
    resolved = BorrowReturnEscalation.objects.filter(
        resolved_at__isnull=True,
    ).exclude(order__status='borrowed').update(resolved_at=now)

    return {
        'escalated': escalated,
        'level_one': by_level[1],
        'level_two': by_level[2],
        'level_three': by_level[3],
        'resolved': resolved,
        'tracked': BorrowReturnEscalation.objects.filter(
            resolved_at__isnull=True, escalation_level__gt=0,
        ).count(),
    }


def close_escalation(order, *, now=None):
    """Mark an escalation resolved once the borrow is actually returned.

    Resolution is a closure, not a deletion: the row keeps its level and its
    count so the timeline stays auditable, but stops counting as open work.
    Campus borrowing depends on trust between classmates, so a returned item
    should not leave a permanent mark - only the memory that it was escalated.
    """
    now = now or timezone.now()
    updated = BorrowReturnEscalation.objects.filter(
        order=order, resolved_at__isnull=True,
    ).update(resolved_at=now)
    return updated


def build_borrow_escalation_report(*, days=DEFAULT_LOOKBACK_DAYS, now=None, limit=20):
    """Summarise open and recently closed escalations for the operations board.

    Only escalations that actually happened are reported. An order that came
    back late but was never escalated is not a case, and padding the list with
    it would make the operator read rows that require no action.
    """
    now = now or timezone.now()
    days = max(1, int(days))
    start = now - timedelta(days=days)

    escalations = list(
        BorrowReturnEscalation.objects.select_related(
            'order', 'order__item', 'order__buyer', 'order__seller',
        ).filter(escalation_level__gt=0).order_by('-last_escalated_at', '-id')
    )

    rows = []
    for escalation in escalations[:max(limit, 1)]:
        order = escalation.order
        overdue_days = None
        if order.return_due_at:
            overdue_days = max(0, round((now - order.return_due_at).total_seconds() / 86400))
        rows.append({
            'order': order,
            'escalation': escalation,
            'level': escalation.escalation_level,
            'level_label': escalation.get_escalation_level_display(),
            'overdue_days': overdue_days,
            'is_open': escalation.is_open,
            'escalation_count': escalation.escalation_count,
            'last_escalated_at': escalation.last_escalated_at,
            'resolved_at': escalation.resolved_at,
        })

    period_rows = [
        row for row in escalations
        if row.last_escalated_at and row.last_escalated_at >= start
    ]
    open_rows = [row for row in rows if row['is_open']]
    summary = {
        'open_count': len(open_rows),
        'level_three_count': sum(1 for row in open_rows if row['level'] >= 3),
        'period_count': len(period_rows),
        'resolved_count': sum(
            1 for row in escalations
            if row.resolved_at and row.resolved_at >= start
        ),
        'max_overdue_days': max(
            (row['overdue_days'] for row in open_rows if row['overdue_days'] is not None),
            default=None,
        ),
        'escalation_total': sum(row['escalation_count'] for row in rows),
    }

    return {
        'days': days,
        'rows': rows,
        'summary': summary,
        'has_data': bool(rows),
        'summary_text': _summary_text(summary),
    }


def _summary_text(summary):
    """One sentence for the dashboard, most urgent fact first."""
    if not summary['open_count']:
        if summary['resolved_count']:
            return f'当前没有未闭环的借用逾期，周期内已闭环 {summary["resolved_count"]} 笔。'
        return '当前没有需要催收的借用逾期。'
    parts = [f'当前 {summary["open_count"]} 笔借用逾期尚未闭环']
    if summary['level_three_count']:
        parts.append(f'其中 {summary["level_three_count"]} 笔已上报治理队列')
    if summary['max_overdue_days'] is not None:
        parts.append(f'最长已逾期 {summary["max_overdue_days"]} 天')
    return '，'.join(parts) + '。'
