# -*- coding: utf-8 -*-
"""失物招领举报处理质量：提交线、处理线与当前积压分开读。

失物招领是站内唯一一个要求陌生人凭一段描述交出物品的地方，所以它有自己独立的举报目标
`LostFoundReport`——不是 `Report` 加一个可空外键。上一轮补上的是单条举报的闭环（提交、
审核、通知举报人）；本模块不处理举报，只回答运营侧四个问题：

1. 周期内有多少举报提交上来，都是什么理由；
2. 现在还有多少条没人看，最久的一条等了多久；
3. 已处理的举报里，成立和驳回各占多少；
4. 「举报人对发布者不可见」这条边界，有没有被某一行代码破坏。

三个窗口分开：提交按 ``created_at``，处理按 ``reviewed_at``，积压按当前时刻。待处理
举报没有处理时刻，是右删失样本，不进处理时长分布——把它当成 0 秒会把「处理很快」这个
结论直接写错。

驳回率不叫「举报准确率」。它的分母是已处理举报，而「该有多少举报被提交」在观测数据里
没有分母：没人举报的冒充记录不会留下任何痕迹。所以这里只报事实，不评价举报人准不准。

边界那一栏的读法要说清楚。举报处理只给**举报人**发一条 ``report_update`` 通知，跳转
地址固定是「我的失物招领举报」。这个地址在站内只有这一处会生成，所以它可以当作这条
通道的指纹：拿它去问「通知都发给了谁」，再和「谁提交过举报」「谁发布过被举报的记录」
两个集合比一比，就知道有没有人从这条通道里看见了不该看的东西。**报的是集合之间的
差集，不是任何一条通知的内容**——通知正文里带着记录标题，而标题是自由文本。

本模块只读。不展示正文和用户名，不读补充说明和审核备注的任何一个字符，只数长度；
不改状态、不发通知、不关记录。
"""

from datetime import time as dt_time, timedelta

from django.urls import reverse
from django.utils import timezone

from .models import LostFoundPost, LostFoundReport, Notification

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

REASON_LABELS = dict(LostFoundReport.REASON_CHOICES)
STATUS_LABELS = dict(LostFoundReport.STATUS_CHOICES)

# 已处理举报的处理时长分档。治理时限是 24 小时（见 governance_sla.SLA_TARGETS），
# 所以档位必须在一天之内再切一刀：全落进「超过一天」就分不出当天看完和拖了三天的
# 差别，而这两件事在运营眼里完全是两个动作。待处理举报的等待年龄用同一组分档——
# 两个问题的尺度是一样的，都是「一件事在人手上压了多久」。
LATENCY_BUCKETS = (
    (3600, '1 小时内', '当班就能看完'),
    (21600, '1-6 小时', '半天内处理完'),
    (86400, '6-24 小时', '压在一天时限上'),
    (259200, '1-3 天', '已经超出治理时限'),
    (None, '3 天以上', '没人跟这条举报'),
)

# 待处理举报里，等待超过一天就算越过治理时限。和 SLA_TARGETS 的 24 小时一致。
OVERDUE_SECONDS = 86400


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
    """按最粗的可读单位渲染，最小单位是分钟。

    0 秒不能说成「0 分钟」——同一次审核里写完结论的那一瞬间就是这个值，写成
    0 分钟会让人以为有数据缺了。这里是「不到 1 分钟」。
    """
    if seconds is None:
        return '—'
    if seconds < 60:
        return '不到 1 分钟'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分钟'
    if seconds < 86400:
        return f'{int(seconds // 3600)} 小时'
    return f'{int(seconds // 86400)} 天'


def _period_start(days, today):
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _report_queryset():
    """只取统计需要的列，避免把被举报记录的标题整表带进运营看板内存。"""
    # 只 select_related 到 post：发布者那一层本模块一个字段都不读（见模块开头
    # 的边界说明），把它 join 进来只会让 SQL 更宽。
    return LostFoundReport.objects.select_related('post').only(
        'id', 'reason', 'detail', 'status', 'created_at', 'reviewed_at',
        'review_note', 'reporter_id',
        'post_id', 'post__title', 'post__status', 'post__expires_at',
    )


def _post_is_exposed(report, now):
    """被举报的记录现在还挂在公开列表里吗。

    只有这一种举报是「每多等一小时就多一分危险」的：冒充失主的人还在被人联系。
    记录已经关闭或过期的，伤害窗口已经关上，时限要求也就松一档。
    """
    post = report.post
    if post.status != 'active':
        return False
    return post.expires_at is None or post.expires_at > now


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


def _reason_rows(records, now):
    rows = [
        {'reason': reason, 'label': label, 'count': 0, 'exposed_count': 0}
        for reason, label in REASON_LABELS.items()
    ]
    row_by_reason = {row['reason']: row for row in rows}
    for report in records:
        row = row_by_reason.get(report.reason)
        if row is None:
            continue
        row['count'] += 1
        if _post_is_exposed(report, now):
            row['exposed_count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(records))
        row['is_small_sample'] = row['count'] < MIN_DIRECTION_SAMPLE
    return rows


def _submission_stats(records, now):
    detail_lengths = []
    exposed_count = 0
    for report in records:
        detail_lengths.append(len((report.detail or '').strip()))
        if _post_is_exposed(report, now):
            exposed_count += 1
    return {
        'submitted_count': len(records),
        'reporter_count': len({report.reporter_id for report in records}),
        # 被举报记录还在公开列表里的条数。它和「提交量」是两个分母：前者回答
        # 「有多少危险窗口还开着」，后者回答「运营收到了多少信号」。
        'exposed_count': exposed_count,
        'exposed_share': _percent(exposed_count, len(records)),
        'detail': _text_stats(detail_lengths),
    }


def _outcome_stats(records):
    counts = {status: 0 for status in ('resolved', 'rejected')}
    for report in records:
        if report.status in counts:
            counts[report.status] += 1

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
        'resolved_rate': _percent(counts['resolved'], len(records)),
        'rejected_rate': _percent(counts['rejected'], len(records)),
        'is_small_sample': len(records) < MIN_DIRECTION_SAMPLE,
    }


def _handling_stats(records):
    """从提交到处理完成隔了多久，以及处理备注写没写。"""
    durations = []
    note_lengths = []
    for report in records:
        durations.append(_elapsed_seconds(report.created_at, report.reviewed_at) or 0.0)
        note = (report.review_note or '').strip()
        if note:
            note_lengths.append(len(note))

    return {
        'measured_count': len(records),
        'buckets': _bucket_rows(durations, LATENCY_BUCKETS),
        'mean_label': _format_elapsed(_mean(durations)),
        'median_label': _format_elapsed(_percentile(durations, 0.5)),
        'p90_label': _format_elapsed(_percentile(durations, 0.9)),
        'over_sla_count': sum(1 for value in durations if value > OVERDUE_SECONDS),
        'over_sla_rate': _percent(
            sum(1 for value in durations if value > OVERDUE_SECONDS), len(durations),
        ),
        # 备注只数长度，不读内容。备注是给下一个处理人看的内部说明，
        # 不是给举报人的结论，把它当作文本统计会把内部判断变成运营指标。
        'note': {
            'filled_count': len(note_lengths),
            'fill_rate': _percent(len(note_lengths), len(records)),
            **_text_stats(note_lengths),
        },
    }


def _pending_stats(records, now):
    """当前还没处理的举报：等多久、清单长什么样。"""
    waits = []
    rows = []
    for report in records:
        seconds = _elapsed_seconds(report.created_at, now)
        waits.append(seconds)
        rows.append({
            'report_id': report.pk,
            'reason_label': REASON_LABELS.get(report.reason, report.reason),
            'submitted_at': report.created_at,
            'wait_label': _format_elapsed(seconds),
            'wait_seconds': seconds,
            'is_over_sla': seconds > OVERDUE_SECONDS,
            'post_is_exposed': _post_is_exposed(report, now),
            'detail_length': len((report.detail or '').strip()),
        })
    rows.sort(key=lambda row: -row['wait_seconds'])

    return {
        'measured_count': len(records),
        'buckets': _bucket_rows(waits, LATENCY_BUCKETS),
        'oldest_label': _format_elapsed(max(waits) if waits else None),
        'over_one_day_count': sum(1 for value in waits if value > OVERDUE_SECONDS),
        'exposed_count': sum(1 for row in rows if row['post_is_exposed']),
        'rows': rows,
        'row_total': len(rows),
        'row_limit': PENDING_LIST_LIMIT,
    }


def _boundary_stats(now):
    """举报人这条线，有没有把不该看见的人牵进来。

    通道指纹是通知的跳转地址：全站只有 ``review_lost_found_report`` 会给举报人发
    ``report_update``，并且固定跳「我的失物招领举报」。商品举报走的是另一个地址，
    所以这个过滤不会把两者混在一起。
    """
    channel_url = reverse('my_lost_found_reports')
    channel = Notification.objects.filter(
        kind='report_update', target_url=channel_url,
    )
    notification_count = channel.count()
    recipients = set(
        channel.values_list('recipient_id', flat=True).distinct()
    )
    # 不按统计周期过滤：边界是这条通道自打上线以来的性质，不是某一个周期的事。
    reporters = set(LostFoundReport.objects.values_list('reporter_id', flat=True).distinct())
    publishers = set(
        LostFoundPost.objects.filter(reports__isnull=False)
        .values_list('reporter_id', flat=True).distinct()
    )

    # 同一个人既发布过被举报的记录、又提交过别的举报时，光看通知表分不清他收到
    # 的是哪一条。这类人单独数，不计进泄漏——猜错会把一条正常通知说成事故。
    ambiguous = recipients & reporters & publishers
    # 只发布过、从没举报过的人收到举报人的通知，是明确的越界：他不可能有等待
    # 结论的举报。和「既不是举报人也不是发布者」分开报，是因为这两种越界的
    # 修法不同——前者说明写入路径认错了人，后者说明整个通道被接错了地方。
    publisher_recipients = (recipients & publishers) - ambiguous
    # 到这里，三类收件人互斥且完整：举报人里没发布过的 + 上面这三类 = 全部收件人。
    # 所以面板上这三行可以加起来对账，不会出现同一个人被数两遍。
    unexpected = recipients - reporters - publishers

    return {
        'channel_notification_count': notification_count,
        'channel_recipient_count': len(recipients),
        'reporter_count': len(reporters),
        'publisher_count': len(publishers),
        'unexpected_recipient_count': len(unexpected),
        'publisher_recipient_count': len(publisher_recipients),
        'ambiguous_recipient_count': len(ambiguous),
        # 通知随用户一起级联删除，人也可能换号，所以这一栏只能证明「有泄漏」，
        # 不能证明「没有泄漏」。它是下界，不是合格证。
        'is_lower_bound': True,
    }


def _handled_without_notice(handled):
    """处理过的举报，举报人有没有被告知结果。

    闭环的最后一环是通知：审核写进去了，举报人却永远不知道结论，那这条举报对他
    来说和石沉大海没有区别。

    这个数正常应该是 0，而它之所以值得每天看，是因为「只有一条写入路径」是
    `LostFoundReportAdmin` 用 `readonly_fields` 硬守下来的——后台改不了状态，只有
    运营页面的表单能写。哪天有人为了让某个脚本能跑通而放开一个字段，这里会第一
    个变红，而用户侧的症状只是「举报没回音」，没人会来报障。

    通知和举报之间没有外键，只能按人比集合；通知又随用户级联删除。所以这是
    「一个通知都没收到的举报人」人数的下界，不是准确值。
    """
    handled_reporters = {report.reporter_id for report in handled}
    notified = set(Notification.objects.filter(
        kind='report_update', target_url=reverse('my_lost_found_reports'),
        recipient_id__in=handled_reporters,
    ).values_list('recipient_id', flat=True).distinct())
    return len(handled_reporters - notified)


def _summary(submission, pending, outcome, handling, boundary, unnotified):
    if not (submission['submitted_count'] or pending['measured_count']):
        return '当前周期没有收到失物招领举报，也没有待处理的积压。'

    parts = []
    if submission['submitted_count']:
        parts.append(
            f"周期内提交 {submission['submitted_count']} 条失物招领举报"
            f"（{submission['reporter_count']} 人提交，其中 "
            f"{submission['exposed_count']} 条指向仍在小程序列表里的记录）"
        )
    if pending['measured_count']:
        parts.append(
            f"；当前待处理 {pending['measured_count']} 条，最久等了 "
            f"{pending['oldest_label']}，超过一天 {pending['over_one_day_count']} 条"
        )
    if outcome['measured_count']:
        parts.append(
            f"；周期内处理 {outcome['measured_count']} 条，"
            f"驳回 {outcome['counts']['rejected']} 条"
        )
        if handling['over_sla_count']:
            parts.append(f"，其中 {handling['over_sla_count']} 条超过 1 天时限")
    if unnotified:
        parts.append(f"；{unnotified} 名举报人处理完了却没收到任何通知")
    if boundary['publisher_recipient_count'] or boundary['unexpected_recipient_count']:
        parts.append('；举报进展通知出现了不该收到的收件人，边界需要排查')
    if outcome['measured_count'] and outcome['is_small_sample']:
        parts.append(f"。已处理样本不足 {MIN_DIRECTION_SAMPLE} 条，比例只作参考")
    return ''.join(parts) + '。'


def build_lost_found_report_quality(days=30, now=None):
    """把失物招领举报的提交、处理与积压事实读成一份只读报告。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()
    start = _period_start(days, timezone.localtime(now).date())

    submitted = list(_report_queryset().filter(
        created_at__gte=start, created_at__lte=now,
    ).order_by('created_at', 'id'))
    handled = list(_report_queryset().filter(
        status__in=('resolved', 'rejected'),
        reviewed_at__gte=start, reviewed_at__lte=now,
    ).order_by('reviewed_at', 'id'))
    pending = list(_report_queryset().filter(status__in=('pending', 'reviewing')).order_by(
        'created_at', 'id',
    ))

    submission = _submission_stats(submitted, now)
    outcome = _outcome_stats(handled)
    handling = _handling_stats(handled)
    pending_stats = _pending_stats(pending, now)
    boundary = _boundary_stats(now)
    unnotified = _handled_without_notice(handled)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(submitted or handled or pending),
        'summary': _summary(
            submission, pending_stats, outcome, handling, boundary, unnotified,
        ),
        'submission': submission,
        'reason_rows': _reason_rows(submitted, now),
        'outcome': outcome,
        'handling': handling,
        'pending': pending_stats,
        'boundary': boundary,
        'handled_without_notice': unnotified,
        'overdue_seconds': OVERDUE_SECONDS,
        'min_direction_sample': MIN_DIRECTION_SAMPLE,
    }
