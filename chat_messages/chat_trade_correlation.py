# -*- coding: utf-8 -*-
"""Describe how message-open latency and trade outcomes move together.

The response panel already answers how quickly a seller opens buyer inquiries.
The order panels already answer whether trades reach an order and a terminal
state. Putting those two answers next to each other is not enough: the first is
per message while the second is per order, so a reader can accidentally compare
mismatched denominators.

This module makes the unit explicit: one item, one buyer, and one seller. For
each such conversation it takes the median opening latency of buyer inquiries,
places that conversation in a fixed latency bucket, and reports whether a
matching order appeared within seven days of the first inquiry and whether it
reached a terminal event in the same window.

The table is deliberately descriptive. It does not compute a correlation
coefficient, assign a response score, rank users or items, or claim that opening
a message faster causes a trade. Heavy-tailed latencies and conversations
clustered around popular items can make a single coefficient look authoritative
while hiding exactly those structures.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from listings.models import Item, Order, OrderEvent

from .chat_response import (
    LATENCY_BUCKETS,
    _bucket_index,
    _format_duration,
    _percentile,
    response_seconds_of,
)
from .models import PrivateMessage


OUTCOME_WINDOW = timedelta(days=7)

# A bucket rate needs ten mature, timed conversations before it is read as a
# signal. Smaller buckets still show their counts so operations can see that a
# cell exists rather than wondering whether it was silently dropped.
MIN_BUCKET_SAMPLE = 10

TERMINAL_STATUSES = frozenset({'completed', 'returned'})

MESSAGE_FIELDS = (
    'item_id',
    'sender_id',
    'receiver_id',
    'created_at',
    'read_at',
)

ROW_KEYS = (
    'within-one-hour',
    'within-one-day',
    'within-three-days',
    'within-one-week',
    'over-one-week',
)


def _empty_rows():
    """Keep all latency buckets on screen, including empty ones."""
    return [
        {
            'key': key,
            'label': label,
            'conversations': 0,
            'timed_inquiries': 0,
            'conversation_latencies': [],
            'ordered': 0,
            'completed': 0,
        }
        for key, (_limit, label) in zip(ROW_KEYS, LATENCY_BUCKETS)
    ]


def _period_start(now, days):
    """Use the same local-midnight cut as the other dashboard panels."""
    today = timezone.localdate(now)
    return timezone.make_aware(
        datetime.combine(today - timedelta(days=days - 1), time.min)
    )


def _candidate_conversations(start, now):
    """Find item-linked buyer-to-seller conversations touched in the period.

    The candidate list only decides which conversations to inspect. Their first
    historical inquiry may predate the period, and those conversations are
    reported separately instead of being counted as new.
    """
    candidate_rows = PrivateMessage.objects.filter(
        item__isnull=False,
        created_at__gte=start,
        created_at__lte=now,
    ).values('item_id', 'sender_id', 'receiver_id').distinct()

    item_ids = {row['item_id'] for row in candidate_rows}
    items = Item.objects.filter(id__in=item_ids).in_bulk()
    conversations = set()
    for row in candidate_rows:
        item = items.get(row['item_id'])
        if item is None or item.seller_id != row['receiver_id']:
            continue
        if row['sender_id'] == row['receiver_id']:
            continue
        conversations.add((row['item_id'], row['sender_id'], row['receiver_id']))
    return conversations, items


def _load_history(conversations, items):
    """Load the complete buyer-inquiry history for candidate conversations."""
    item_ids = {item_id for item_id, _buyer_id, _seller_id in conversations}
    histories = {key: [] for key in conversations}
    rows = PrivateMessage.objects.filter(
        item_id__in=item_ids,
    ).values(*MESSAGE_FIELDS).order_by('created_at', 'id')
    for row in rows:
        key = (row['item_id'], row['sender_id'], row['receiver_id'])
        if key not in histories:
            continue
        item = items[row['item_id']]
        if row['sender_id'] == row['receiver_id']:
            continue
        if row['receiver_id'] != item.seller_id:
            continue
        histories[key].append(row)
    return histories


def _conversation_record(key, messages, start, now):
    """Classify one conversation and return aggregate, non-identifying facts."""
    first_at = messages[0]['created_at']
    if first_at < start:
        return {'status': 'started_before_period'}

    window_end = first_at + OUTCOME_WINDOW
    if window_end > now:
        return {'status': 'immature'}

    timed_seconds = []
    timed_inquiries = 0
    for row in messages:
        if row['created_at'] < first_at or row['created_at'] > window_end:
            continue
        if not row['read_at'] or row['read_at'] > window_end:
            continue
        seconds = response_seconds_of(row)
        if seconds is None:
            continue
        timed_inquiries += 1
        timed_seconds.append(seconds)

    if not timed_seconds:
        return {'status': 'untimed'}

    item_id, buyer_id, seller_id = key
    orders = list(Order.objects.filter(
        item_id=item_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        created_at__gte=first_at,
        created_at__lte=window_end,
    ).values('id'))
    ordered = bool(orders)
    completed = OrderEvent.objects.filter(
        order_id__in=[order['id'] for order in orders],
        to_status__in=TERMINAL_STATUSES,
        created_at__gte=first_at,
        created_at__lte=window_end,
    ).exists()

    return {
        'status': 'eligible',
        'timed_inquiries': timed_inquiries,
        'latency_seconds': _percentile(timed_seconds, 0.5),
        'ordered': ordered,
        'completed': completed,
    }


def build_chat_trade_correlation(*, days=30, now=None):
    """Build the read-only co-variation table for the operations dashboard."""
    now = now or timezone.now()
    days = max(1, int(days))
    start = _period_start(now, days)
    buckets = _empty_rows()
    bucket_by_key = {bucket['key']: bucket for bucket in buckets}

    candidates, items = _candidate_conversations(start, now)
    histories = _load_history(candidates, items)
    summary = {
        'observed_conversations': 0,
        'eligible_conversations': 0,
        'started_before_period': 0,
        'immature_conversations': 0,
        'untimed_conversations': 0,
        'timed_inquiries': 0,
        'ordered_conversations': 0,
        'completed_conversations': 0,
        'completion_rate': None,
    }

    for key in sorted(candidates):
        messages = histories.get(key, [])
        if not messages:
            continue
        summary['observed_conversations'] += 1
        record = _conversation_record(key, messages, start, now)

        if record['status'] == 'started_before_period':
            summary['started_before_period'] += 1
        elif record['status'] == 'immature':
            summary['immature_conversations'] += 1
        elif record['status'] == 'untimed':
            summary['untimed_conversations'] += 1
        else:
            bucket = bucket_by_key[
                ROW_KEYS[_bucket_index(record['latency_seconds'])]
            ]
            bucket['conversations'] += 1
            bucket['timed_inquiries'] += record['timed_inquiries']
            bucket['conversation_latencies'].append(record['latency_seconds'])
            if record['ordered']:
                bucket['ordered'] += 1
            if record['completed']:
                bucket['completed'] += 1

            summary['eligible_conversations'] += 1
            summary['timed_inquiries'] += record['timed_inquiries']
            if record['ordered']:
                summary['ordered_conversations'] += 1
            if record['completed']:
                summary['completed_conversations'] += 1

    rows = []
    for bucket in buckets:
        conversations = bucket['conversations']
        median_seconds = _percentile(bucket['conversation_latencies'], 0.5)
        completion_rate = (
            round(bucket['completed'] / conversations * 100, 1)
            if conversations else None
        )
        rows.append({
            'key': bucket['key'],
            'label': bucket['label'],
            'conversations': conversations,
            'timed_inquiries': bucket['timed_inquiries'],
            'median_seconds': median_seconds,
            'median_label': _format_duration(median_seconds),
            'ordered': bucket['ordered'],
            'completed': bucket['completed'],
            'completion_rate': completion_rate,
            'has_sample': conversations >= MIN_BUCKET_SAMPLE,
        })

    eligible = summary['eligible_conversations']
    summary['completion_rate'] = (
        round(summary['completed_conversations'] / eligible * 100, 1)
        if eligible else None
    )

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'outcome_window_days': OUTCOME_WINDOW.days,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'has_data': bool(summary['observed_conversations']),
        'summary': summary,
        'rows': rows,
        'note': (
            '同一商品、买家、卖家的会话只计一次；第一条询问后固定观察 7 天，'
            '未满窗口与无法计时的会话单列。订单必须创建于询问之后，'
            'completed 与 returned 都算成交。本表只描述共同变化，不解释因果。'
        ),
    }
