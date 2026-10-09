"""Measure how quickly each notification type is actually handled.

A notification that nobody reads is worse than no notification at all: it
trains users to ignore the badge. The dashboard already knows how many
notifications were sent and how many are still unread, but it could not tell
"delivered" apart from "handled". This module adds the missing dimension by
reading the read_at timestamp that every mark-read path now writes.

Latency is derived in Python rather than inside a queryset expression.
Subtracting two datetime columns in SQL is not portable across the database
backends this project may run on, and the notification table is small enough
that one pass over the period rows costs less than the extra queries a
database-side expression would need.
"""

from datetime import timedelta

from django.utils import timezone

from .models import Notification


# Buckets used to describe the handling latency distribution. They are coarse
# on purpose: the point is to see whether a notification type is handled at
# all, not to report a precise number of minutes.
LATENCY_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (604800, '1 周内'),
    (None, '1 周以上'),
)

# Types where a slow response has a direct business cost, so they are called
# out separately instead of being buried in a long table.
BUSINESS_CRITICAL_KINDS = {
    'order_created',
    'order_status',
    'order_expiring',
    'order_expired',
    'order_dispute',
    'meeting_incident',
    'gift_application',
    'demand_match',
    'demand_response',
}

# Everything the aggregation needs. Keeping the list explicit means the module
# never loads the whole model, which matters on the 365 day view.
PERIOD_FIELDS = (
    'kind',
    'is_read',
    'read_at',
    'created_at',
    'occurrence_count',
    'last_occurred_at',
)

STALE_AFTER = timedelta(days=7)


def response_seconds_of(row):
    """Seconds between delivery and reading for a single notification row."""
    read_at = row.get('read_at')
    created_at = row.get('created_at')
    if not read_at or not created_at:
        return None
    return max((read_at - created_at).total_seconds(), 0.0)


def with_response_seconds(queryset):
    """Evaluate a notification queryset into rows carrying a latency in seconds.

    Unread rows, and rows written before read_at existed, evaluate to None and
    are skipped by every caller that measures latency.
    """
    rows = []
    for row in queryset.values(*PERIOD_FIELDS):
        row['response_seconds'] = response_seconds_of(row)
        rows.append(row)
    return rows


def _bucket_label(seconds):
    """Return the human label for a latency measured in seconds."""
    for limit, label in LATENCY_BUCKETS:
        if limit is None or seconds < limit:
            return label
    return LATENCY_BUCKETS[-1][1]


def _bucket_index(seconds):
    for index, (limit, _label) in enumerate(LATENCY_BUCKETS):
        if limit is None or seconds < limit:
            return index
    return len(LATENCY_BUCKETS) - 1


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


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _handled_rows(rows):
    """Rows that carry a usable read_at timestamp."""
    return [row for row in rows if row['is_read'] and row['read_at']]


def _build_kind_rows(rows):
    """Aggregate handling latency and reach for every notification type."""
    kind_labels = dict(Notification.KIND_CHOICES)
    grouped = {}
    for row in rows:
        grouped.setdefault(row['kind'], []).append(row)

    result = []
    for kind, kind_rows in grouped.items():
        sent = len(kind_rows)
        read_rows = _handled_rows(kind_rows)
        latencies = [row['response_seconds'] for row in read_rows if row['response_seconds'] is not None]
        avg_seconds = _mean(latencies)
        result.append({
            'kind': kind,
            'label': kind_labels.get(kind, kind),
            'sent': sent,
            'read': len(read_rows),
            'unread': sent - len(read_rows),
            'read_rate': round(len(read_rows) / sent * 100, 1) if sent else 0,
            'avg_seconds': avg_seconds,
            'avg_label': _format_duration(avg_seconds),
            'fastest_seconds': min(latencies) if latencies else None,
            'fastest_label': _format_duration(min(latencies)) if latencies else '—',
            'slowest_seconds': max(latencies) if latencies else None,
            'slowest_label': _format_duration(max(latencies)) if latencies else '—',
            'is_business_critical': kind in BUSINESS_CRITICAL_KINDS,
            'needs_attention': _kind_needs_attention(sent, len(read_rows), avg_seconds),
        })
    result.sort(key=lambda row: (-row['sent'], row['kind']))
    return result


def _kind_needs_attention(sent, read, avg_seconds):
    """Whether a type is under-read or too slow to be worth sending."""
    if not sent:
        return False
    read_rate = read / sent
    if read_rate < 0.5:
        return True
    if avg_seconds and avg_seconds > STALE_AFTER.total_seconds():
        return True
    return False


def _build_latency_distribution(read_rows):
    """Bucket handled notifications by how long they took to be read."""
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


def _hour_counts(rows, field):
    """Count rows by local hour of a datetime field."""
    counts = {}
    for row in rows:
        moment = row.get(field)
        if not moment:
            continue
        hour = timezone.localtime(moment).hour
        counts[hour] = counts.get(hour, 0) + 1
    return counts


def _build_hour_profile(rows, read_rows):
    """Compare when notifications are sent against when they are handled."""
    sent_by_hour = _hour_counts(rows, 'created_at')
    read_by_hour = _hour_counts(read_rows, 'read_at')
    profile = []
    for hour in range(24):
        profile.append({
            'hour': hour,
            'label': f'{hour:02d}:00',
            'sent': sent_by_hour.get(hour, 0),
            'read': read_by_hour.get(hour, 0),
        })
    return profile


def _build_handled_trend(read_rows, dates):
    """Handled notifications per day, keyed by the day they were read."""
    daily = {}
    for row in read_rows:
        day = timezone.localdate(row['read_at'])
        daily[day] = daily.get(day, 0) + 1
    return [{'date': day, 'handled': daily.get(day, 0)} for day in dates]


def build_notification_response_insights(*, days=30, now=None):
    """Build the notification handling-latency view for the operations dashboard.

    The result answers three questions the plain counters could not:

    1. Is a notification type actually being read, and how quickly?
    2. How does the handling time distribute, rather than only its average?
    3. Are notifications being sent at hours when nobody reads them?

    Response time is derived from the stored read_at timestamp, so it reflects
    real behaviour and not an assumption about when a user was online.
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    start_date = today - timedelta(days=days - 1)
    start = timezone.make_aware(timezone.datetime.combine(start_date, timezone.datetime.min.time()))
    dates = [start_date + timedelta(days=index) for index in range(days)]

    rows = with_response_seconds(
        Notification.objects.filter(created_at__gte=start, created_at__lte=now)
    )
    read_rows = _handled_rows(rows)

    kind_rows = _build_kind_rows(rows)
    buckets, bucket_total = _build_latency_distribution(read_rows)
    hour_profile = _build_hour_profile(rows, read_rows)

    sent = len(rows)
    handled = len(read_rows)
    unread = sum(1 for row in rows if not row['is_read'])
    # A notification still unread after a week is treated as effectively
    # ignored rather than pending, which keeps "handling time" honest.
    stale_cutoff = now - STALE_AFTER
    stale_unread = sum(
        1 for row in rows if not row['is_read'] and row['created_at'] <= stale_cutoff
    )

    all_latencies = [
        row['response_seconds'] for row in read_rows if row['response_seconds'] is not None
    ]
    median_seconds = _percentile(all_latencies, 0.5)
    p90_seconds = _percentile(all_latencies, 0.9)

    handled_trend = _build_handled_trend(read_rows, dates)

    critical_rows = [row for row in kind_rows if row['is_business_critical']]
    critical_sent = sum(row['sent'] for row in critical_rows)
    critical_read = sum(row['read'] for row in critical_rows)

    attention_rows = [row for row in kind_rows if row['needs_attention']]

    recommendations = _build_recommendations(
        kind_rows=kind_rows,
        hour_profile=hour_profile,
        stale_unread=stale_unread,
        sent=sent,
        handled=handled,
        median_seconds=median_seconds,
    )

    return {
        'period_days': days,
        'has_data': bool(sent),
        'summary': {
            'sent': sent,
            'handled': handled,
            'unread': unread,
            'stale_unread': stale_unread,
            'handling_rate': round(handled / sent * 100, 1) if sent else 0,
            'median_seconds': median_seconds,
            'median_label': _format_duration(median_seconds),
            'p90_seconds': p90_seconds,
            'p90_label': _format_duration(p90_seconds),
        },
        'critical_summary': {
            'sent': critical_sent,
            'read': critical_read,
            'read_rate': round(critical_read / critical_sent * 100, 1) if critical_sent else 0,
            'count': len(critical_rows),
        },
        'kind_rows': kind_rows,
        'attention_rows': attention_rows,
        'latency_buckets': buckets,
        'latency_total': bucket_total,
        'hour_profile': hour_profile,
        'hour_max': max((point['sent'] for point in hour_profile), default=1) or 1,
        'handled_trend': handled_trend,
        'handled_trend_max': max(
            (point['handled'] for point in handled_trend), default=1,
        ) or 1,
        'recommendations': recommendations,
    }


def _build_recommendations(*, kind_rows, hour_profile, stale_unread, sent, handled, median_seconds):
    """Turn the measured numbers into concrete, actionable advice."""
    recommendations = []
    for row in kind_rows:
        if row['needs_attention'] and row['is_business_critical']:
            recommendations.append(
                f'{row["label"]}已发送 {row["sent"]} 条但只有 {row["read"]} 条被处理，'
                f'平均处理时长 {row["avg_label"]}；这类通知直接影响交易推进，'
                '建议检查是否被免打扰时段拦下，或改成摘要合并发送。'
            )
        elif row['needs_attention']:
            recommendations.append(
                f'{row["label"]}的已读比例为 {row["read_rate"]}%，'
                f'平均处理时长 {row["avg_label"]}；如果长期偏低，'
                '可以降低发送频率或让用户自行关闭这类提醒。'
            )
    if stale_unread:
        recommendations.append(
            f'有 {stale_unread} 条通知超过一周仍未处理，占全部通知的 '
            f'{round(stale_unread / sent * 100, 1) if sent else 0}%；'
            '建议在校验未读提醒时把这类通知折叠，避免未读数长期虚高。'
        )
    busy_hours = [point for point in hour_profile if point['sent'] >= 5 and point['read'] == 0]
    if busy_hours:
        hours_text = '、'.join(point['label'] for point in busy_hours[:3])
        recommendations.append(
            f'{hours_text} 时段发送的通知目前没有处理记录；'
            '如果这些时段正好覆盖用户免打扰时间，可以把非紧急通知挪到其他时间。'
        )
    if median_seconds is not None and median_seconds > 2 * 86400 and not recommendations:
        recommendations.append(
            f'通知的中位处理时长为 {_format_duration(median_seconds)}，整体偏慢；'
            '可以先从交易类通知入手，确认提醒是否足够明确。'
        )
    if not recommendations:
        recommendations.append('当前周期各类通知的处理时效没有明显异常，继续保持。')
    return recommendations