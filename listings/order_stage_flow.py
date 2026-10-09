# -*- coding: utf-8 -*-
"""Measure how long each stage of an order actually takes.

The dashboard already knew how many orders were created, confirmed and
completed. It could not tell where the time went: a seller who confirms in
three minutes and a pair that agrees on a meeting time but takes five days to
show up produce the same "average confirmation hours" number.

OrderEvent already records every transition, including the action-only events
that keep the previous status (arrival check-in, meeting scheduling, disputes).
This module reads that trail once, per order, and reconstructs the stage
timeline: order placed -> seller confirmed -> moved to handoff -> deal closed
-> borrowed item returned.

Three measurement rules make the numbers trustworthy rather than merely
plausible:

1. Right-censored samples are excluded. An order still sitting in a stage at
   the end of the period has not finished it, so counting its elapsed time
   would report "in progress" as "slow". Those orders are counted separately
   as still_in_stage so the stage is not silently under-reported. Where the
   chain ends depends on the trade mode: a sale is finished at "completed", a
   borrow only at "returned".
2. Out-of-order events are dropped and counted. A milestone recorded before
   the previous one (backfilled data, a clock that moved backwards) would
   otherwise produce a negative or meaningless duration. Dropping the sample
   keeps the median honest; the counter keeps the loss visible.
3. Durations are computed in Python. Subtracting two datetime columns inside a
   queryset is not portable across the database backends this project may run
   on, and one pass over the period rows costs less than the extra queries a
   database-side expression would need.
"""

from datetime import timedelta

from django.utils import timezone

from .models import Order, OrderEvent


# The stage chain. Each entry is one stage in the life of an order; its
# duration is the gap between consecutive milestones. "milestones" is a tuple
# because a stage can end at more than one status: closing a deal is recorded
# as "completed" for a sale and "borrowed" for a lend.
STAGES = (
    {
        'key': 'placed_to_confirmed',
        'label': '下单 → 卖家确认',
        'from_label': '下单',
        'to_label': '卖家确认',
        'milestones': ('confirmed',),
        'note': '买家发起预约到卖家接受，是买家最先感知到的等待。',
    },
    {
        'key': 'confirmed_to_meeting',
        'label': '卖家确认 → 当面交付',
        'from_label': '卖家确认',
        'to_label': '当面交付',
        'milestones': ('meeting',),
        'note': '双方约定交付时间与地点，把订单推进到线下环节。',
    },
    {
        'key': 'meeting_to_closed',
        'label': '当面交付 → 成交 / 借出',
        'from_label': '当面交付',
        'to_label': '成交 / 借出',
        'milestones': ('completed', 'borrowed'),
        'note': '见面确认到双方点完成交，出售记为成交，借用记为借出。',
    },
    {
        'key': 'borrowed_to_returned',
        'label': '借出 → 物品归还',
        'from_label': '借出',
        'to_label': '物品归还',
        'milestones': ('returned',),
        'note': '仅限期借用：从借出到物品归还并重新上架。',
    },
)

# Stage keys in chain order, used to walk the timeline by position.
STAGE_KEYS = tuple(stage['key'] for stage in STAGES)

# Trade modes whose chain ends at "borrowed" rather than "completed".
BORROW_TRADE_MODES = ('borrow',)

# A stage taking longer than this is called out as a bottleneck candidate.
SLOW_STAGE_SECONDS = 48 * 3600

# Enough samples before a stage is judged at all. Below this the median is
# reported but never used for a recommendation.
MIN_SAMPLES_FOR_JUDGEMENT = 3

# Facets used to break the same measurements down by a dimension operations
# staff can act on. Each entry is (label, ORM path prefix, display field).
FACETS = (
    ('分类', 'category'),
    ('交付地点', 'location'),
)

MAX_FACET_ROWS = 6


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _format_duration(seconds):
    """Render a duration in the coarsest unit that stays readable."""
    if seconds is None:
        return '—'
    seconds = max(seconds, 0)
    if seconds < 60:
        return f'{round(seconds)} 秒'
    if seconds < 3600:
        return f'{round(seconds / 60)} 分钟'
    if seconds < 86400:
        return f'{round(seconds / 3600)} 小时'
    return f'{round(seconds / 86400)} 天'


def _period_start(now, days):
    """Local midnight `days - 1` days ago, matching the dashboard period."""
    today = timezone.localdate(now)
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, timezone.datetime.min.time()))


def _first_milestone_at(events, milestones):
    """First time an order reached any of the given statuses.

    Several paths write an event with to_status equal to the status the order
    is already in (arrival check-in, meeting scheduling, dispute filing). Those
    are real actions but not transitions, so they must not be mistaken for the
    moment the order entered a stage.
    """
    for event in events:
        if event['to_status'] not in milestones:
            continue
        if event.get('from_status') and event['from_status'] == event['to_status']:
            # A transition to the status the order is already in is a no-op
            # status write, not a stage entry.
            continue
        return event['created_at']
    return None


def _order_timeline(created_at, events):
    """Build the ordered milestone timestamps for one order.

    Returns (timeline, dropped) where timeline maps a stage key to the moment
    that stage completed, and dropped counts milestones that arrived before the
    previous stage's milestone.
    """
    timeline = {}
    dropped = 0
    previous_moment = created_at
    for stage in STAGES:
        moment = _first_milestone_at(events, stage['milestones'])
        if moment is None:
            continue
        if moment < previous_moment:
            # Backfilled or clock-skewed record: the order cannot have finished
            # this stage before the previous one.
            dropped += 1
            continue
        timeline[stage['key']] = moment
        previous_moment = moment
    return timeline, dropped


def _collect_orders(order_period):
    """Read the event trail for every order and reduce it to one record each.

    Doing this in a single pass keeps the stage table, the bottleneck detection
    and every breakdown consistent with each other, and keeps the query count
    independent of how many breakdowns are rendered.
    """
    created_at_by_order = dict(order_period.values_list('id', 'created_at'))
    facets_by_order = {}
    for row in order_period.values(
        'id', 'item__category__name', 'meeting_location__name', 'item__trade_mode',
    ):
        facets_by_order[row['id']] = row

    events_by_order = {}
    for event in OrderEvent.objects.filter(
        order__in=order_period,
    ).values('order_id', 'to_status', 'from_status', 'created_at').order_by(
        'order_id', 'created_at', 'id',
    ):
        events_by_order.setdefault(event['order_id'], []).append(event)

    records = []
    for order_id, created_at in created_at_by_order.items():
        events = events_by_order.get(order_id, [])
        timeline, dropped = _order_timeline(created_at, events)
        facets = facets_by_order.get(order_id, {})
        records.append({
            'order_id': order_id,
            'created_at': created_at,
            'timeline': timeline,
            'dropped': dropped,
            'trade_mode': facets.get('item__trade_mode'),
            'category': facets.get('item__category__name'),
            'location': facets.get('meeting_location__name'),
        })
    return records


def _terminal_stage_key(trade_mode):
    """Stage whose completion closes the order for this trade mode."""
    return 'borrowed_to_returned' if trade_mode in BORROW_TRADE_MODES else 'meeting_to_closed'


def _measure(records):
    """Turn order records into per-stage samples and censoring counts.

    A stage is measured only when the order actually completed it. An order
    that has not reached its terminal stage is right-censored: it is counted as
    still inside the stage it is working through, which is what "still_in_stage"
    reports.
    """
    samples = {key: [] for key in STAGE_KEYS}
    still_in_stage = {key: 0 for key in STAGE_KEYS}
    end_to_end = []
    dropped_samples = 0
    censored_orders = 0
    unconfirmed_orders = 0

    for record in records:
        timeline = record['timeline']
        dropped_samples += record['dropped']
        terminal_key = _terminal_stage_key(record['trade_mode'])

        if 'placed_to_confirmed' not in timeline:
            unconfirmed_orders += 1

        if terminal_key not in timeline:
            # The order never finished its chain, so its unfinished tail is
            # censored rather than slow.
            censored_orders += 1
            current_key = None
            for key in STAGE_KEYS:
                if key in timeline:
                    current_key = key
                else:
                    break
            if current_key is None:
                current_key = STAGE_KEYS[0]
            still_in_stage[current_key] += 1

        for index, stage in enumerate(STAGES):
            # The first stage starts at Order.created_at, which is not a status
            # transition and therefore never appears in the event trail.
            start = record['created_at'] if index == 0 else timeline.get(STAGE_KEYS[index - 1])
            end = timeline.get(stage['key'])
            if start is None or end is None:
                continue
            seconds = (end - start).total_seconds()
            if seconds < 0:
                # Defensive: the timeline already drops out-of-order pairs.
                dropped_samples += 1
                continue
            samples[stage['key']].append(seconds)

        if terminal_key in timeline:
            end_to_end.append(
                (timeline[terminal_key] - record['created_at']).total_seconds()
            )

    return {
        'samples': samples,
        'still_in_stage': still_in_stage,
        'end_to_end': end_to_end,
        'dropped_samples': dropped_samples,
        'censored_orders': censored_orders,
        'unconfirmed_orders': unconfirmed_orders,
    }


def _build_stage_rows(measured):
    """Summarise every stage into a row the dashboard can render."""
    rows = []
    for stage in STAGES:
        values = measured['samples'][stage['key']]
        still = measured['still_in_stage'][stage['key']]
        median = _percentile(values, 0.5)
        p90 = _percentile(values, 0.9)
        rows.append({
            'key': stage['key'],
            'label': stage['label'],
            'from_label': stage['from_label'],
            'to_label': stage['to_label'],
            'note': stage['note'],
            'sample_size': len(values),
            'still_in_stage': still,
            'median_seconds': median,
            'median_label': _format_duration(median),
            'p90_seconds': p90,
            'p90_label': _format_duration(p90),
            'average_seconds': _mean(values),
            'average_label': _format_duration(_mean(values)),
            'fastest_seconds': min(values) if values else None,
            'fastest_label': _format_duration(min(values)) if values else '—',
            'slowest_seconds': max(values) if values else None,
            'slowest_label': _format_duration(max(values)) if values else '—',
            'is_bottleneck': bool(
                median is not None
                and median > SLOW_STAGE_SECONDS
                and len(values) >= MIN_SAMPLES_FOR_JUDGEMENT
            ),
        })
    rows.sort(key=lambda row: (-(row['median_seconds'] or 0), row['key']))
    return rows


def _build_bottleneck(stage_rows):
    """Pick the stage that owns most of the median end-to-end time."""
    measured = [
        row for row in stage_rows
        if row['median_seconds'] is not None and row['sample_size'] >= MIN_SAMPLES_FOR_JUDGEMENT
    ]
    if not measured:
        return None
    total = sum(row['median_seconds'] for row in measured)
    if not total:
        return None
    worst = max(measured, key=lambda row: row['median_seconds'])
    return {
        'key': worst['key'],
        'label': worst['label'],
        'median_label': worst['median_label'],
        'sample_size': worst['sample_size'],
        'share_of_measured_total': round(worst['median_seconds'] / total * 100, 1),
    }


def _build_facet_rows(records, facet_key):
    """Break the same measurements down by one facet.

    Grouping happens in Python on the already-reduced records, so adding a
    facet costs no extra database round trip.
    """
    grouped = {}
    for record in records:
        name = record.get(facet_key)
        if not name:
            continue
        grouped.setdefault(name, []).append(record)

    rows = []
    for name, group_records in grouped.items():
        measured = _measure(group_records)
        stage_rows = _build_stage_rows(measured)
        ranked = [row for row in stage_rows if row['median_seconds'] is not None]
        if not ranked:
            continue
        worst = max(ranked, key=lambda row: row['median_seconds'])
        end_to_end = measured['end_to_end']
        rows.append({
            'name': name,
            'order_count': len(group_records),
            'measured_orders': len(end_to_end),
            'worst_stage_label': worst['label'],
            'worst_stage_median_label': worst['median_label'],
            'end_to_end_median_seconds': _percentile(end_to_end, 0.5),
            'end_to_end_median_label': _format_duration(_percentile(end_to_end, 0.5)),
            'dropped_samples': measured['dropped_samples'],
        })
    rows.sort(key=lambda row: (-row['order_count'], row['name']))
    return rows[:MAX_FACET_ROWS]


def _build_recommendations(stage_rows, bottleneck, dropped_samples, censored_orders):
    """Turn the measured stages into concrete, actionable advice."""
    recommendations = []
    if bottleneck:
        recommendations.append(
            f'当前周期耗时最长的阶段是「{bottleneck["label"]}」，'
            f'中位耗时 {bottleneck["median_label"]}，约占可测量阶段中位总耗时的 '
            f'{bottleneck["share_of_measured_total"]}%；'
            '优先缩短这一段，对端到端时长的影响最大。'
        )
    for row in stage_rows:
        if not row['is_bottleneck']:
            continue
        recommendations.append(
            f'「{row["label"]}」中位耗时 {row["median_label"]}、P90 {row["p90_label"]}，'
            f'样本 {row["sample_size"]} 笔；可以把提醒提前到该阶段开始时触发，'
            '而不是等到已经超时。'
        )
    if dropped_samples:
        recommendations.append(
            f'有 {dropped_samples} 条阶段记录的时间早于上一阶段，已按异常数据剔除；'
            '如果数量持续增加，建议检查批量导入或补录流程。'
        )
    if censored_orders:
        recommendations.append(
            f'有 {censored_orders} 笔订单在周期结束时仍未走完所属流程，'
            '这部分订单不计入耗时统计，可以结合运营提醒单独跟进。'
        )
    if not recommendations:
        recommendations.append('当前周期各阶段耗时没有明显异常，继续保持。')
    return recommendations


def build_order_stage_flow(*, days=30, now=None):
    """Build the stage-duration view for the operations dashboard.

    The result answers three questions the plain counters could not:

    1. Which stage owns the waiting time a user actually feels?
    2. Is a stage slow for everyone, or only for a handful of orders?
    3. How much of the data is trustworthy, and how much had to be dropped?

    Only orders created inside the period are measured, so a long-running order
    from an earlier period does not inflate the current numbers.
    """
    now = now or timezone.now()
    order_period = Order.objects.filter(
        created_at__gte=_period_start(now, days), created_at__lte=now,
    )

    records = _collect_orders(order_period)
    measured = _measure(records)
    stage_rows = _build_stage_rows(measured)
    bottleneck = _build_bottleneck(stage_rows)

    total_orders = len(records)
    end_to_end = measured['end_to_end']

    return {
        'period_days': days,
        'has_data': bool(total_orders),
        'summary': {
            'total_orders': total_orders,
            'measured_orders': len(end_to_end),
            'measured_share': round(len(end_to_end) / total_orders * 100, 1) if total_orders else 0,
            'dropped_samples': measured['dropped_samples'],
            'censored_orders': measured['censored_orders'],
            'unconfirmed_orders': measured['unconfirmed_orders'],
            'end_to_end_median_seconds': _percentile(end_to_end, 0.5),
            'end_to_end_median_label': _format_duration(_percentile(end_to_end, 0.5)),
            'end_to_end_p90_label': _format_duration(_percentile(end_to_end, 0.9)),
        },
        'stage_rows': stage_rows,
        'stage_max': max(
            (row['median_seconds'] or 0 for row in stage_rows), default=1,
        ) or 1,
        'bottleneck': bottleneck,
        'facet_rows': [
            {'key': key, 'label': label, 'rows': _build_facet_rows(records, key)}
            for label, key in FACETS
        ],
        'recommendations': _build_recommendations(
            stage_rows, bottleneck, measured['dropped_samples'], measured['censored_orders'],
        ),
    }