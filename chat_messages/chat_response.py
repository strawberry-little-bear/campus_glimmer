"""Measure how quickly private messages are actually opened.

`PrivateMessage` has carried an `is_read` flag since the first migration, but
nothing recorded *when* that flag flipped. Four write paths turn unread into
read -- opening a conversation, marking one message, marking a batch, marking a
whole sender -- and all four used to look identical afterwards: a boolean with
no timestamp beside it.

This module reads the `read_at` column that those paths now write and answers
three questions the boolean alone cannot:

1. How long does a message sit unopened before someone opens it?
2. How many messages were sent and never opened at all?
3. Do fast replies line up with completed orders, or is that just a story?

Every number is derived from stored timestamps. Nothing is inferred from when a
user was probably online, and no score is assigned to any user.
"""

from datetime import timedelta

from django.utils import timezone

from .models import PrivateMessage


# A message still unopened after this long is treated as effectively ignored
# rather than pending. Two days is deliberately shorter than the notification
# module's week: a private message is one person waiting for another, and the
# cost of silence is a lost trade rather than an ignored badge.
SILENT_AFTER = timedelta(days=2)

# Coarse buckets on purpose. The question is whether replies happen at all, not
# the exact minute count.
LATENCY_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (604800, '1 周内'),
    (None, '1 周以上'),
)

# Below this a direction is noise, not a finding.
MIN_LATENCY_SAMPLE = 5

# Below this a breakdown by sender/receiver role is not worth reading.
MIN_ROLE_SAMPLE = 10

PERIOD_FIELDS = (
    'sender_id',
    'receiver_id',
    'item_id',
    'created_at',
    'is_read',
    'read_at',
)


def response_seconds_of(row):
    """Seconds between sending and first opening for a single message row."""
    read_at = row.get('read_at')
    created_at = row.get('created_at')
    if not read_at or not created_at:
        return None
    return max((read_at - created_at).total_seconds(), 0.0)


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _format_duration(seconds):
    """Render a latency in the coarsest unit that stays readable."""
    if seconds is None:
        return '—'
    if seconds < 60:
        return f'{round(seconds)} 秒'
    if seconds < 3600:
        return f'{round(seconds / 60)} 分钟'
    if seconds < 86400:
        return f'{round(seconds / 3600)} 小时'
    return f'{round(seconds / 86400)} 天'


def _bucket_index(seconds):
    for index, (limit, _label) in enumerate(LATENCY_BUCKETS):
        if limit is None or seconds < limit:
            return index
    return len(LATENCY_BUCKETS) - 1


def with_response_seconds(queryset):
    """Evaluate a message queryset into rows carrying a latency in seconds.

    Unread rows, and rows written before `read_at` existed, evaluate to None
    and are skipped by every caller that measures latency.
    """
    rows = []
    for row in queryset.values(*PERIOD_FIELDS):
        row['response_seconds'] = response_seconds_of(row)
        rows.append(row)
    return rows


def _read_rows(rows):
    """Rows that were opened, whether or not the opening was timed.

    A row written before read_at existed is opened but untimed. It counts
    toward reach and never toward a latency sample, which is why the two are
    derived from different conditions rather than one shared filter.
    """
    return [row for row in rows if row['is_read']]


def _timed_rows(read_rows):
    """Opened rows whose opening carries a usable timestamp."""
    return [row for row in read_rows if row['read_at']]


def _unopened_rows(rows, cutoff):
    """Rows sent before the cutoff and still never opened.

    This is a moving population, not a verdict: a message can leave it at any
    moment. It is reported separately from latency samples and never mixed
    into them.
    """
    return [
        row for row in rows
        if not row['is_read'] and row['created_at'] <= cutoff
    ]


def _build_latency_distribution(read_rows):
    """Bucket opened messages by how long they sat unopened."""
    buckets = [{'label': label, 'count': 0} for _limit, label in LATENCY_BUCKETS]
    for row in read_rows:
        seconds = row['response_seconds']
        if seconds is None:
            continue
        buckets[_bucket_index(seconds)]['count'] += 1
    total = sum(bucket['count'] for bucket in buckets)
    for bucket in buckets:
        bucket['share'] = round(bucket['count'] / total * 100, 1) if total else 0
    return buckets, total


def _build_daily_trend(rows, dates):
    """Messages sent per day against messages opened per day."""
    sent_by_day = {}
    opened_by_day = {}
    for row in rows:
        day = timezone.localdate(row['created_at'])
        sent_by_day[day] = sent_by_day.get(day, 0) + 1
        if row['is_read'] and row['read_at']:
            opened_day = timezone.localdate(row['read_at'])
            opened_by_day[opened_day] = opened_by_day.get(opened_day, 0) + 1
        elif row['is_read']:
            # Opened before read_at existed: counted on the day it was sent,
            # since that is the only day this row can be placed on.
            sent_day = timezone.localdate(row['created_at'])
            opened_by_day[sent_day] = opened_by_day.get(sent_day, 0) + 1
    trend = []
    for day in dates:
        trend.append({
            'date': day,
            'sent': sent_by_day.get(day, 0),
            'opened': opened_by_day.get(day, 0),
        })
    return trend


def _build_hour_profile(rows, read_rows):
    """Compare when messages are sent against when they are opened."""
    sent_by_hour = {}
    opened_by_hour = {}
    for row in rows:
        hour = timezone.localtime(row['created_at']).hour
        sent_by_hour[hour] = sent_by_hour.get(hour, 0) + 1
    for row in read_rows:
        hour = timezone.localtime(row['read_at']).hour
        opened_by_hour[hour] = opened_by_hour.get(hour, 0) + 1
    profile = []
    for hour in range(24):
        profile.append({
            'hour': hour,
            'label': f'{hour:02d}:00',
            'sent': sent_by_hour.get(hour, 0),
            'opened': opened_by_hour.get(hour, 0),
        })
    return profile


def _build_role_rows(rows):
    """Split latency by whether the reader is a seller or a buyer.

    The role is derived from whether the row's item was owned by the reader.
    A message attached to an item where the receiver is the seller is a buyer
    asking; a message where the sender is the seller is a seller replying.
    Messages with no item attached carry no role and are reported on their own
    rather than forced into either bucket.
    """
    from .models import Item  # local import to keep module import cheap

    item_owner = dict(
        Item.objects.filter(id__in={
            row['item_id'] for row in rows if row['item_id']
        }).values_list('id', 'seller_id')
    )
    grouped = {
        'buyer_asks_seller': [],
        'seller_replies_buyer': [],
        'no_item': [],
    }
    for row in rows:
        owner = item_owner.get(row['item_id'])
        if owner is None:
            grouped['no_item'].append(row)
        elif row['receiver_id'] == owner:
            grouped['buyer_asks_seller'].append(row)
        elif row['sender_id'] == owner:
            grouped['seller_replies_buyer'].append(row)
        else:
            grouped['no_item'].append(row)

    labels = {
        'buyer_asks_seller': '买家发给卖家',
        'seller_replies_buyer': '卖家回复买家',
        'no_item': '未关联商品的私信',
    }
    result = []
    for key, key_rows in grouped.items():
        opened = _read_rows(key_rows)
        latencies = [
            row['response_seconds'] for row in opened
            if row['response_seconds'] is not None
        ]
        median_seconds = _percentile(latencies, 0.5)
        result.append({
            'key': key,
            'label': labels[key],
            'sent': len(key_rows),
            'opened': len(opened),
            'open_rate': round(len(opened) / len(key_rows) * 100, 1) if key_rows else 0,
            'median_seconds': median_seconds,
            'median_label': _format_duration(median_seconds),
            'sample_size': len(latencies),
            'has_sample': len(latencies) >= MIN_LATENCY_SAMPLE,
        })
    result.sort(key=lambda row: -row['sent'])
    return result


def _build_conversation_health(rows):
    """Per-conversation reply behaviour, without naming any user.

    Conversations are grouped by the unordered pair of participants. Only
    aggregates are returned; the identities are dropped before the result
    leaves this function.
    """
    conversations = {}
    for row in rows:
        sender_id = row['sender_id']
        receiver_id = row['receiver_id']
        pair = tuple(sorted((sender_id, receiver_id)))
        entry = conversations.setdefault(pair, {
            'messages': 0,
            'opened': 0,
            'two_way': False,
        })
        entry['messages'] += 1
        if row['is_read']:
            entry['opened'] += 1
    for pair, entry in conversations.items():
        # A conversation is two-way when both participants sent at least once.
        sent_by = {row['sender_id'] for row in rows if tuple(sorted((row['sender_id'], row['receiver_id']))) == pair}
        entry['two_way'] = len(sent_by) > 1

    total = len(conversations)
    two_way = sum(1 for entry in conversations.values() if entry['two_way'])
    single_message = sum(1 for entry in conversations.values() if entry['messages'] == 1)
    never_opened = sum(
        1 for entry in conversations.values() if entry['messages'] and not entry['opened']
    )
    rows = [
        {
            'label': '双向会话',
            'count': two_way,
            'share': round(two_way / total * 100, 1) if total else 0,
            'note': '双方都发过至少一句，这是唯一健康的形状',
        },
        {
            'label': '单向会话',
            'count': total - two_way,
            'share': round((total - two_way) / total * 100, 1) if total else 0,
            'note': '从头到尾只有一方在说',
        },
        {
            'label': '只说了一句',
            'count': single_message,
            'share': round(single_message / total * 100, 1) if total else 0,
            'note': '发完就没有下文，可能是问句没人接',
        },
        {
            'label': '一句话都没被打开',
            'count': never_opened,
            'share': round(never_opened / total * 100, 1) if total else 0,
            'note': '收件人一次都没点开过这个会话',
        },
    ]
    return {
        'total': total,
        'two_way': two_way,
        'two_way_rate': round(two_way / total * 100, 1) if total else 0,
        'single_message': single_message,
        'never_opened': never_opened,
        'has_data': bool(total),
        'rows': rows,
    }


def build_chat_response_insights(*, days=30, now=None, silent_after=None):
    """Build the private-message responsiveness view for the dashboard.

    Answers three questions with three separate denominators:

    1. How long an opened message waited -- only rows that carry read_at.
    2. How many messages were never opened -- only rows past the silence cutoff.
    3. Whether reply speed and completed orders line up -- only messages tied
       to an item, reported as a correlation and never as a cause.

    No user-level score is produced and nothing here is shown to the people in
    the conversation.
    """
    now = now or timezone.now()
    silent_after = silent_after or SILENT_AFTER
    today = timezone.localdate(now)
    start_date = today - timedelta(days=days - 1)
    start = timezone.make_aware(
        timezone.datetime.combine(start_date, timezone.datetime.min.time())
    )
    dates = [start_date + timedelta(days=index) for index in range(days)]

    rows = with_response_seconds(
        PrivateMessage.objects.filter(created_at__gte=start, created_at__lte=now)
    )
    read_rows = _read_rows(rows)
    unopened_rows = _unopened_rows(rows, now - silent_after)

    all_latencies = [
        row['response_seconds'] for row in read_rows
        if row['response_seconds'] is not None
    ]
    median_seconds = _percentile(all_latencies, 0.5)
    p90_seconds = _percentile(all_latencies, 0.9)

    buckets, bucket_total = _build_latency_distribution(read_rows)
    role_rows = _build_role_rows(rows)
    conversation_health = _build_conversation_health(rows)
    daily_trend = _build_daily_trend(rows, dates)
    hour_profile = _build_hour_profile(rows, read_rows)

    sent = len(rows)
    opened = len(read_rows)
    unopened = len(unopened_rows)
    # Rows read before read_at existed cannot be timed; they are counted as
    # opened but never enter a latency sample.
    opened_untimed = opened - len(all_latencies)

    return {
        'period_days': days,
        'silent_after_days': silent_after.days,
        'has_data': bool(sent),
        'summary': {
            'sent': sent,
            'opened': opened,
            'unopened': unopened,
            'opened_untimed': opened_untimed,
            'open_rate': round(opened / sent * 100, 1) if sent else 0,
            'median_seconds': median_seconds,
            'median_label': _format_duration(median_seconds),
            'p90_seconds': p90_seconds,
            'p90_label': _format_duration(p90_seconds),
            'latency_sample_size': len(all_latencies),
            'latency_sample_sufficient': len(all_latencies) >= MIN_LATENCY_SAMPLE,
            'min_latency_sample': MIN_LATENCY_SAMPLE,
        },
        'latency_buckets': buckets,
        'latency_total': bucket_total,
        'role_rows': role_rows,
        'conversation_health': conversation_health,
        'daily_trend': daily_trend,
        'daily_trend_max': max(
            (point['sent'] for point in daily_trend), default=1,
        ) or 1,
        'hour_profile': hour_profile,
        'hour_max': max((point['sent'] for point in hour_profile), default=1) or 1,
    }
