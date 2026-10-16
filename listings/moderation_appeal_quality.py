# -*- coding: utf-8 -*-
"""内容申诉处理质量：提交线、处理线与当前积压分开读。

上一轮补齐的是单条申诉闭环。这个模块不处理申诉，只回答运营侧四个问题：

1. 作者收到确认违规后，有多少人真的提出申诉；
2. 待处理申诉现在积压多少、等了多久；
3. 已处理申诉维持了多少、改判了多少、改判后有没有恢复；
4. 原复核与申诉处理是不是换过人。

所有窗口分成三条：提交按 ``submitted_at``，处理按 ``resolved_at``，积压按当前
时刻。待处理申诉没有处理时刻，是右删失样本，不进入处理时长分布。申诉率和成立率
不合成“审核准确率”：前者的分母是确认违规记录，后者的分母是已处理申诉，两个数
指向的动作完全不同。

本模块只读。不展示正文和用户名，不读申诉说明与处理说明的任何一个字符，只数长度；
不自动改规则、不改队列顺序、不关闭任何申诉。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from chat_messages.models import ModerationAppeal, ModerationEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 与其他治理面板保持同一个量级：不足 12 条只给计数，不用百分比讲方向。
MIN_DIRECTION_SAMPLE = 12

# 待处理清单页面最多列这么多条，CSV 使用同一构建器输出的完整清单。
PENDING_LIST_LIMIT = 20

REASON_LABELS = {
    'not_violation': '内容不违规',
    'missing_context': '缺少语境',
    'other': '其他情况',
}

STATUS_LABELS = {
    'pending': '待处理',
    'upheld': '维持原判',
    'accepted': '成立并恢复',
    'accepted_without_restore': '成立但未恢复',
}

CHANNEL_LABELS = {
    'comment': '商品留言',
    'private_message': '私信',
}

# 时长分档与内容审核规则面板一致，便于两类治理队列互相对照。
LATENCY_BUCKETS = (
    (600, '十分钟内', '提交后很快被处理'),
    (3600, '十分钟到一小时', '当天完成，节奏跟得上'),
    (86400, '一小时到一天', '隔了数小时，需要看积压原因'),
    (None, '超过一天', '申诉已经明显排队'),
)


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _mean(values):
    return sum(values) / len(values) if values else None


def _rounded_mean(values):
    value = _mean(values)
    return round(value, 1) if value is not None else None


def _percentile(values, fraction):
    """Nearest-rank percentile；小样本不插值，读数保持可解释。"""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _elapsed_seconds(started_at, ended_at):
    if started_at is None or ended_at is None:
        return None
    # 后台补录或手工修数据可能让起点晚于终点。负数没有业务含义，夹到零，
    # 不让一条脏数据把均值和分档一起拉歪。
    return max(0.0, (ended_at - started_at).total_seconds())


def _format_elapsed(seconds):
    if seconds is None:
        return '—'
    if seconds < 600:
        return '十分钟内'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分钟'
    if seconds < 86400:
        return f'{int(seconds // 3600)} 小时'
    return f'{int(seconds // 86400)} 天'


def _period_start(days, today):
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _appeal_queryset():
    """只取统计需要的列，避免把被拦截正文整表带进运营看板内存。"""
    return ModerationAppeal.objects.select_related('event').only(
        'id', 'appellant_id', 'event_id', 'event__channel', 'event__author_id',
        'reason', 'statement', 'submitted_at', 'status',
        'original_reviewed_by_id', 'original_reviewed_at',
        'resolved_by_id', 'resolved_at', 'resolution_note',
    )


def _bucket_index(seconds, buckets):
    for index, (limit, _label, _hint) in enumerate(buckets):
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _bucket_rows(seconds_list, buckets):
    rows = [
        {'label': label, 'hint': hint, 'count': 0}
        for _limit, label, hint in buckets
    ]
    for seconds in seconds_list:
        rows[_bucket_index(seconds, buckets)]['count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(seconds_list))
    return rows


def _text_stats(values):
    return {
        'measured_count': len(values),
        'mean': _rounded_mean(values),
        'median': _percentile(values, 0.5),
        'p90': _percentile(values, 0.9),
        'max': max(values) if values else 0,
    }


def _submission_stats(records, start, original_decision_count, cohort_appeal_count):
    delays = []
    statement_lengths = []
    missing_snapshot_count = 0
    historical_decision_count = 0
    for appeal in records:
        if appeal.original_reviewed_at is None:
            missing_snapshot_count += 1
        else:
            delays.append(_elapsed_seconds(
                appeal.original_reviewed_at, appeal.submitted_at,
            ))
            if appeal.original_reviewed_at < start:
                historical_decision_count += 1
        statement_lengths.append(len((appeal.statement or '').strip()))

    return {
        'submitted_count': len(records),
        'appellant_count': len({appeal.appellant_id for appeal in records}),
        'historical_decision_count': historical_decision_count,
        'missing_snapshot_count': missing_snapshot_count,
        'original_decision_count': original_decision_count,
        'cohort_appeal_count': cohort_appeal_count,
        # 项目没有申诉时效：同一批确认违规可能在本周期结束后才申诉，
        # 所以这里只能是当前周期可见的下界，不用于考核任何人。
        'appeal_rate': _percent(cohort_appeal_count, original_decision_count),
        'delay': {
            'measured_count': len(delays),
            'mean_label': _format_elapsed(_mean(delays)),
            'median_label': _format_elapsed(_percentile(delays, 0.5)),
            'p90_label': _format_elapsed(_percentile(delays, 0.9)),
        },
        'statement': _text_stats(statement_lengths),
    }


def _reason_rows(records):
    rows = [
        {'reason': reason, 'label': label, 'count': 0, 'appellant_count': 0}
        for reason, label in REASON_LABELS.items()
    ]
    row_by_reason = {row['reason']: row for row in rows}
    appellants = {reason: set() for reason in REASON_LABELS}
    for appeal in records:
        if appeal.reason not in row_by_reason:
            continue
        row = row_by_reason[appeal.reason]
        row['count'] += 1
        appellants[appeal.reason].add(appeal.appellant_id)
    for row in rows:
        row['appellant_count'] = len(appellants[row['reason']])
        row['share'] = _percent(row['count'], len(records))
        row['is_small_sample'] = row['count'] < MIN_DIRECTION_SAMPLE
    return rows


def _outcome_stats(records):
    counts = {status: 0 for status in STATUS_LABELS if status != 'pending'}
    for appeal in records:
        if appeal.status in counts:
            counts[appeal.status] += 1

    accepted_total = counts['accepted'] + counts['accepted_without_restore']
    rows = [
        {
            'status': status,
            'label': STATUS_LABELS[status],
            'count': counts[status],
            'share': _percent(counts[status], len(records)),
        }
        for status in counts
    ]
    return {
        'measured_count': len(records),
        'counts': counts,
        'rows': rows,
        'upheld_rate': _percent(counts['upheld'], len(records)),
        'accepted_rate': _percent(accepted_total, len(records)),
        'accepted_total': accepted_total,
        'accepted_without_restore_rate': _percent(
            counts['accepted_without_restore'], accepted_total,
        ),
        'is_small_sample': len(records) < MIN_DIRECTION_SAMPLE,
    }


def _resolution_stats(records):
    durations = []
    note_lengths = []
    filled_count = 0
    same_reviewer_count = 0
    different_reviewer_count = 0
    unknown_reviewer_count = 0

    for appeal in records:
        durations.append(_elapsed_seconds(appeal.submitted_at, appeal.resolved_at))
        note = (appeal.resolution_note or '').strip()
        if note:
            filled_count += 1
            note_lengths.append(len(note))
        if not appeal.original_reviewed_by_id or not appeal.resolved_by_id:
            unknown_reviewer_count += 1
        elif appeal.original_reviewed_by_id == appeal.resolved_by_id:
            same_reviewer_count += 1
        else:
            different_reviewer_count += 1

    known_reviewer_count = same_reviewer_count + different_reviewer_count
    return {
        'duration': {
            'measured_count': len(durations),
            'mean_label': _format_elapsed(_mean(durations)),
            'median_label': _format_elapsed(_percentile(durations, 0.5)),
            'p90_label': _format_elapsed(_percentile(durations, 0.9)),
            'rows': _bucket_rows(durations, LATENCY_BUCKETS),
        },
        'note': {
            'filled_count': filled_count,
            'fill_rate': _percent(filled_count, len(records)),
            **_text_stats(note_lengths),
        },
        'reviewer': {
            'same_count': same_reviewer_count,
            'different_count': different_reviewer_count,
            'unknown_count': unknown_reviewer_count,
            'known_count': known_reviewer_count,
            'same_rate': _percent(same_reviewer_count, known_reviewer_count),
            'different_rate': _percent(different_reviewer_count, known_reviewer_count),
        },
    }


def _pending_stats(records, now):
    waits = [
        _elapsed_seconds(appeal.submitted_at, now) for appeal in records
    ]
    missing_snapshot_count = sum(
        1 for appeal in records if appeal.original_reviewed_at is None
    )
    rows = []
    for appeal in records:
        rows.append({
            'appeal_id': appeal.pk,
            'event_id': appeal.event_id,
            'channel': appeal.event.channel,
            'channel_label': CHANNEL_LABELS.get(
                appeal.event.channel, appeal.event.channel,
            ),
            'reason': appeal.reason,
            'reason_label': REASON_LABELS.get(appeal.reason, appeal.reason),
            'submitted_at': appeal.submitted_at,
            'wait_label': _format_elapsed(_elapsed_seconds(appeal.submitted_at, now)),
            'statement_length': len((appeal.statement or '').strip()),
            'has_original_reviewer': bool(appeal.original_reviewed_by_id),
        })

    return {
        'measured_count': len(records),
        'missing_snapshot_count': missing_snapshot_count,
        'oldest_label': _format_elapsed(max(waits) if waits else None),
        'over_one_day_count': sum(1 for value in waits if value >= 86400),
        'rows': rows,
        'row_total': len(rows),
        'row_limit': PENDING_LIST_LIMIT,
    }


def _summary(submission, pending, outcome, resolution):
    if not (
        submission['submitted_count'] or pending['measured_count']
        or outcome['measured_count'] or submission['original_decision_count']
    ):
        return '当前周期还没有内容申诉，也没有可申诉的确认违规结论。'

    parts = [
        f"周期内提交 {submission['submitted_count']} 条内容申诉"
        f"（同期确认违规 {submission['original_decision_count']} 条）"
    ]
    if pending['measured_count']:
        parts.append(
            f"；当前待处理 {pending['measured_count']} 条，最久等了 "
            f"{pending['oldest_label']}"
        )
    if outcome['measured_count']:
        parts.append(
            f"；周期内处理 {outcome['measured_count']} 条，"
            f"成立 {outcome['accepted_total']} 条"
        )
        if outcome['counts']['accepted_without_restore']:
            parts.append(
                f"，其中 {outcome['counts']['accepted_without_restore']} 条缺恢复材料"
            )
    if submission['missing_snapshot_count']:
        parts.append(
            f"；{submission['missing_snapshot_count']} 条缺原复核时间快照，不进提交间隔"
        )
    if outcome['measured_count'] and outcome['is_small_sample']:
        parts.append(f"。已处理样本不足 {MIN_DIRECTION_SAMPLE} 条，比例只作参考")
    return ''.join(parts) + '。'


def build_moderation_appeal_quality(days=30, now=None):
    """把内容申诉的提交、处理与积压事实读成一份只读报告。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localtime(now).date())

    submitted = list(_appeal_queryset().filter(
        submitted_at__gte=start, submitted_at__lte=now,
    ))
    resolved = list(_appeal_queryset().filter(
        status__in=('upheld', 'accepted', 'accepted_without_restore'),
        resolved_at__gte=start, resolved_at__lte=now,
    ))
    pending = list(_appeal_queryset().filter(status='pending').order_by(
        'submitted_at', 'id',
    ))

    # 原判分母按第一次确认违规时间归期。已被申诉改判的事件状态会变成 dismissed，
    # 只有申诉表里的 original_reviewed_at 快照还能把它找回，不能只查当前 confirmed。
    confirmed_ids = set(ModerationEvent.objects.filter(
        author__isnull=False,
        status='confirmed',
        reviewed_at__gte=start,
        reviewed_at__lte=now,
    ).values_list('id', flat=True))
    appealed_ids = set(ModerationAppeal.objects.filter(
        event__author__isnull=False,
        original_reviewed_at__gte=start,
        original_reviewed_at__lte=now,
    ).values_list('event_id', flat=True))
    original_decision_ids = confirmed_ids | appealed_ids
    original_decision_count = len(original_decision_ids)
    cohort_appeal_count = len(appealed_ids & original_decision_ids)

    submission = _submission_stats(
        submitted, start, original_decision_count, cohort_appeal_count,
    )
    outcome = _outcome_stats(resolved)
    resolution = _resolution_stats(resolved)
    pending_stats = _pending_stats(pending, now)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(
            submitted or resolved or pending or original_decision_count,
        ),
        'summary': _summary(submission, pending_stats, outcome, resolution),
        'submission': submission,
        'reason_rows': _reason_rows(submitted),
        'outcome': outcome,
        'resolution': resolution,
        'pending': pending_stats,
        'min_direction_sample': MIN_DIRECTION_SAMPLE,
    }
