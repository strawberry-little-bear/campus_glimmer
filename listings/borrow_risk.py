# -*- coding: utf-8 -*-
"""Stratify overdue borrows by category and by deposit band.

The escalation ladder answers "how far has this one borrow gone" and the
governance queue answers "is anyone working on it". Neither answers the
question an operator asks next: is this a few unlucky students, or is one
category of item structurally harder to get back? A power bank borrowed during
exam week and a camping tent borrowed in July are not the same problem, and
averaging them into a single overdue rate hides the difference.

This module therefore groups borrows two ways - by category and by deposit
band - and reports, per group, how many borrows there were, how many were
escalated at all, how many reached level three, and how long the closed ones
took. The point is to make "which kind of item tends to end up in the
governance queue" a number someone can act on instead of a hunch.

It is a read-only aggregation, like governance_sla. It never cancels an
order, never touches a deposit, and never marks a borrower. It also never
feeds back into the escalation ladder: a category that looks risky is a
finding for a human to reason about, not a licence to escalate its borrowers
sooner. Escalation timing is a policy about how long a classmate's item may be
missing, and it must not shift because of a statistic.
"""

from datetime import timedelta

from django.utils import timezone

from .models import BorrowReturnEscalation, Order


# Deposit bands. The cut points are deliberately round numbers on the amounts
# students actually see in the borrow form, not quantiles: an operator reading
# the table needs to recognise the band they are looking at, and a quartile
# boundary would move every month without telling them anything.
DEPOSIT_BANDS = (
    (0, 50, '押金 50 元以下'),
    (50, 100, '押金 50-100 元'),
    (100, 200, '押金 100-200 元'),
    (200, None, '押金 200 元以上'),
)

# How many borrows a group needs before its rates mean anything. Below this the
# group is reported but marked as a sample too small to draw a conclusion from,
# because one overdue tent out of two borrows is 50% and tells nobody anything.
MIN_SAMPLE_SIZE = 5

DEFAULT_LOOKBACK_DAYS = 120


def _band_label(deposit_amount):
    """Which deposit band a given deposit falls into."""
    if deposit_amount is None:
        return '押金未设置'
    for lower, upper, label in DEPOSIT_BANDS:
        if deposit_amount >= lower and (upper is None or deposit_amount < upper):
            return label
    return DEPOSIT_BANDS[-1][2]


def _rate(part, whole):
    """Percentage rounded to one decimal, or None when there is no sample."""
    if not whole:
        return None
    return round(part / whole * 100, 1)


def _fetch_borrows(*, days, now):
    """Every borrow order created in the window, with its escalation attached.

    Borrows are read from the order side rather than the escalation side. An
    order that was never escalated still belongs in the denominator: if the
    table only listed escalated orders, every group would show a 100%
    escalation rate and the stratification would be worthless.
    """
    start = now - timedelta(days=max(1, int(days)))
    borrows = list(
        Order.objects
        .filter(
            item__trade_mode='borrow',
            created_at__gte=start,
            created_at__lte=now,
        )
        .select_related('item', 'item__category')
        .values(
            'id',
            'deposit_amount',
            'status',
            'return_due_at',
            'returned_at',
            'item__category__name',
        )
    )
    escalations = {
        row['order_id']: row
        for row in BorrowReturnEscalation.objects.filter(
            order__item__trade_mode='borrow',
            order__created_at__gte=start,
            order__created_at__lte=now,
        ).values('order_id', 'escalation_level', 'resolved_at', 'last_escalated_at')
    }
    return borrows, escalations


def _group_rows(borrows, escalations, *, key_of, now):
    """Aggregate one stratification of the same borrows.

    key_of maps a borrow to its group name. Keeping the aggregation in one
    place means the category table and the deposit table cannot drift apart in
    how they count, which matters because the two are read side by side and a
    reader assumes they are looking at the same population.
    """
    groups = {}
    for borrow in borrows:
        key = key_of(borrow)
        group = groups.setdefault(key, {
            'key': key,
            'borrow_count': 0,
            'escalated_count': 0,
            'level_three_count': 0,
            'resolved_count': 0,
            'open_count': 0,
            'closed_durations': [],
            'overdue_days': [],
        })
        group['borrow_count'] += 1
        escalation = escalations.get(borrow['id'])
        if not escalation:
            continue
        group['escalated_count'] += 1
        if escalation['escalation_level'] >= 3:
            group['level_three_count'] += 1
        resolved_at = escalation['resolved_at']
        if resolved_at:
            group['resolved_count'] += 1
            # Measure the closure from the rung that actually happened, not from
            # the due date: an order that sat at level one for a week is not an
            # order the operator spent a week on.
            started_at = escalation['last_escalated_at'] or borrow['return_due_at']
            if started_at:
                group['closed_durations'].append(
                    max((resolved_at - started_at).total_seconds(), 0.0) / 86400
                )
        else:
            group['open_count'] += 1
            if borrow['return_due_at']:
                group['overdue_days'].append(
                    max((now - borrow['return_due_at']).total_seconds(), 0.0) / 86400
                )

    rows = []
    for group in groups.values():
        sample = group['borrow_count']
        durations = group['closed_durations']
        rows.append({
            'key': key_value(group),
            'borrow_count': sample,
            'escalated_count': group['escalated_count'],
            'level_three_count': group['level_three_count'],
            'resolved_count': group['resolved_count'],
            'open_count': group['open_count'],
            'escalation_rate': _rate(group['escalated_count'], sample),
            'level_three_rate': _rate(group['level_three_count'], sample),
            'average_close_days': round(sum(durations) / len(durations), 1) if durations else None,
            'max_overdue_days': (
                round(max(group['overdue_days']), 1) if group['overdue_days'] else None
            ),
            'is_small_sample': sample < MIN_SAMPLE_SIZE,
        })
    # Worst first: the group most likely to end up in the governance queue
    # leads, so an operator reads the actionable rows before the noise.
    rows.sort(key=lambda row: (
        -(row['level_three_rate'] or 0),
        -row['level_three_count'],
        -row['borrow_count'],
        row['key'],
    ))
    return rows


def key_value(group):
    """Group name, kept separate so the row shape stays uniform."""
    return group['key']



def _strongest_group(rows):
    """The worst group that is still large enough to be worth reading."""
    usable = [row for row in rows if not row['is_small_sample'] and row['level_three_rate']]
    if not usable:
        return None
    return usable[0]


def _conclusion(category_rows, deposit_rows, *, has_sample):
    """One sentence naming the groups that actually stand out.

    A rate is only worth leading with when the sample supports it, so the
    sentence falls back to a plain statement about volume when every group is
    small. Saying the data is not there yet is a legitimate answer: a platform
    with forty borrows a month should not be handed a ranking of categories.
    """
    if not has_sample:
        return '周期内借用单量不足，暂时无法按分类或押金额度比较逾期风险。'

    worst_category = _strongest_group(category_rows)
    worst_deposit = _strongest_group(deposit_rows)
    parts = []
    if worst_category:
        parts.append(
            f'{worst_category["key"]}的第三级催收率最高，'
            f'{worst_category["borrow_count"]} 笔借用中有 {worst_category["level_three_count"]} 笔拖到第三级'
        )
    if worst_deposit:
        parts.append(
            f'{worst_deposit["key"]}区间共 {worst_deposit["borrow_count"]} 笔，'
            f'其中 {worst_deposit["level_three_count"]} 笔拖到第三级'
        )
    if not parts:
        return '周期内各分类与押金档位的借用量都偏少，暂不足以支撑风险分层结论。'
    return '；'.join(parts) + '。'


def build_borrow_risk(*, days=DEFAULT_LOOKBACK_DAYS, now=None):
    """Stratify borrow escalation risk by category and by deposit band."""
    now = now or timezone.now()
    borrows, escalations = _fetch_borrows(days=days, now=now)

    category_rows = _group_rows(
        borrows, escalations,
        key_of=lambda borrow: borrow['item__category__name'] or '未分类',
        now=now,
    )
    deposit_rows = _group_rows(
        borrows, escalations,
        key_of=lambda borrow: _band_label(borrow['deposit_amount']),
        now=now,
    )

    total_borrows = len(borrows)
    total_escalated = sum(1 for borrow in borrows if borrow['id'] in escalations)
    total_level_three = sum(
        1 for borrow in borrows
        if borrow['id'] in escalations
        and escalations[borrow['id']]['escalation_level'] >= 3
    )

    return {
        'days': days,
        'borrow_count': total_borrows,
        'escalated_count': total_escalated,
        'level_three_count': total_level_three,
        'escalation_rate': _rate(total_escalated, total_borrows),
        'level_three_rate': _rate(total_level_three, total_borrows),
        'category_rows': category_rows,
        'deposit_rows': deposit_rows,
        'min_sample_size': MIN_SAMPLE_SIZE,
        'has_sample': total_borrows >= MIN_SAMPLE_SIZE,
        'has_data': bool(borrows),
        'summary': _conclusion(
            category_rows, deposit_rows, has_sample=total_borrows >= MIN_SAMPLE_SIZE,
        ),
    }
