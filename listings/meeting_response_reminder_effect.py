# -*- coding: utf-8 -*-
"""观察交付安排待回应提醒发出后的阅读与协商动作。

台账里的每一条 MeetingResponseReminderEvent 都是一个稳定的发送样本。提醒
发出后，最值得回看的不是“系统又发了多少”，而是接收人是否读过，以及同一提议
回合随后是否出现接受、拒绝或再次改期。

五条边界固定了这个面板能说什么：

1. 发送分母只来自周期内的台账。旧通知没有台账时单列为未计入，不从去重键
   倒推一个看似完整的样本。
2. 阅读只有 Notification.is_read 与 read_at 同时成立才算已读；通知被删除后，
   发送事实仍在台账里，但阅读状态明确记为不可观测。
3. 回应只取提醒时间之后、同一提议回合里最早的一次接受、拒绝或改期。新提议
   开始下一轮，不把后一轮的动作算回上一轮。
4. 固定观察 72 小时。窗口未结束时仍未回应的是删失样本，不能写成“没有回应”；
   超过窗口后才出现的动作单列为晚回应，不进入窗口内回应计数；它在窗口内仍按未回应计入已过窗分母。
5. 阅读与动作交叉表只纳入阅读状态可判定、且已过观察窗的样本。观察中样本
   不进分母，窗后动作按窗口内未行动计，通知不可观测或阅读字段冲突时也不
   假装成“未读”。
6. 待人工核对清单只列截至当前仍未观测到同一提议回合动作的样本。观察中
   样本仍在等待，已过窗未行动样本优先；窗后已经出现的动作不进入当前清单。

这里只报告观察值，不宣称提醒导致了动作，也不据此自动接受、拒绝、改期、
提升提醒频率或给接收人排名。
"""

from datetime import timedelta
from statistics import median

from django.db.models import Q
from django.utils import timezone

from .meeting_response_reminders import NOTIFICATION_KIND
from .models import (
    MeetingAppointmentEvent,
    MeetingResponseReminderEvent,
    MeetingResponseReminderReview,
    Notification,
)


EFFECT_WINDOW_HOURS = 72
MIN_SEGMENT_SAMPLES = 12
PENDING_REVIEW_PAGE_LIMIT = 20
ACTION_EVENT_TYPES = frozenset({'accepted', 'declined', 'rescheduled'})
ROLE_LABELS = (('buyer', '买家'), ('seller', '卖家'))
ACTION_LABELS = {
    'accepted': '接受安排',
    'declined': '拒绝安排',
    'rescheduled': '再次改期',
}
LATENCY_BUCKETS = (
    ('within_1h', '1 小时内', 1),
    ('within_6h', '1–6 小时', 6),
    ('within_24h', '6–24 小时', 24),
    ('within_window', '24–72 小时', EFFECT_WINDOW_HOURS),
)
NOTICE_ACTION_STATES = (
    ('read', '已读'),
    ('unread', '未读'),
)
DECIDABLE_NOTICE_STATES = frozenset({'read', 'unread'})
NOTICE_ORDER_STATES = (
    ('read_before_action', '先读后动'),
    ('action_before_read', '先动后读'),
    ('same_time', '同一时刻'),
    ('unread_action', '未读行动'),
)
NOTICE_STATE_LABELS = {
    'read': '已读',
    'unread': '未读',
    'missing': '通知不可用',
    'inconsistent': '字段异常',
}
PENDING_REVIEW_STATUS_LABELS = {
    'no_action': '已过窗，尚无动作',
    'open': '观察中，尚无动作',
}
PENDING_REVIEW_OUTCOME_LABELS = dict(
    MeetingResponseReminderReview.OUTCOME_CHOICES,
)
PENDING_REVIEW_FILTER_OPTIONS = (
    ('unreviewed', '未核对'),
    ('waiting', '继续等待'),
    ('resolved', '已处理'),
    ('data_issue', '数据异常'),
    ('no_followup', '无需跟进'),
    ('all', '全部'),
)
PENDING_REVIEW_FILTER_KEYS = frozenset(
    key for key, _label in PENDING_REVIEW_FILTER_OPTIONS
)


def normalize_pending_review_filter(value):
    """把 URL 中的核对筛选收敛到固定集合，未知值回到未核对。"""
    return value if value in PENDING_REVIEW_FILTER_KEYS else 'unreviewed'


def _period_bounds(days, now):
    """按本地日期切出与运营看板其它面板一致的周期。"""
    today = timezone.localdate(now)
    start_date = today - timedelta(days=days - 1)
    start = timezone.make_aware(
        timezone.datetime.combine(start_date, timezone.datetime.min.time()),
    )
    return start, start_date


def _percent(value, total):
    if not total:
        return None
    return round(value * 100.0 / total, 1)


def _label_hours(hours):
    if hours is None:
        return '—'
    if hours < 1:
        return '%.1f 分钟' % (hours * 60)
    return '%.1f 小时' % hours


def _load_events(reminders):
    """一次取回所有相关订单的事件链，避免按提醒逐条查询。"""
    order_ids = {reminder.proposal_event.order_id for reminder in reminders}
    if not order_ids:
        return {}
    events = MeetingAppointmentEvent.objects.filter(
        order_id__in=order_ids,
    ).order_by('order_id', 'occurred_at', 'pk')
    grouped = {}
    for event in events:
        grouped.setdefault(event.order_id, []).append(event)
    return grouped


def _first_action_after(events, proposal, sent_at):
    """返回提醒之后、同一提议回合里的首个有效动作。

    只有在完整链中仍由这条提议开启的回合才能回答“后来发生了什么”。事件表
    中看不到更早的提议时，本函数不替旧数据补造历史。遇到下一版提议、取消或
    更早的回应就停止，避免把下一轮动作错记到这一轮。
    """
    if not proposal.appointment_id:
        return None
    try:
        proposal_index = next(
            index for index, event in enumerate(events) if event.pk == proposal.pk
        )
    except StopIteration:
        return None

    for event in events[proposal_index + 1:]:
        if event.appointment_id != proposal.appointment_id:
            continue
        if event.event_type in {'proposed', 'cancelled'}:
            break
        if event.event_type in ACTION_EVENT_TYPES:
            if event.occurred_at < sent_at:
                return None
            return event
    return None


def _format_local_datetime(value):
    if value is None:
        return '—'
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M')


def _build_pending_review_rows(observations, now):
    """把仍未行动且需要人工查看的提醒排序成可执行清单。

    已过观察窗的样本优先，再按提醒等待时间从长到短排列；仍在窗口内的样本
    排在后面。人工复核只决定当前筛选与展示状态，不改变观察样本或业务记录。
    """
    reminders = [
        observation['reminder'] for observation in observations
        if observation['status'] in PENDING_REVIEW_STATUS_LABELS
    ]
    review_histories = {}
    reminder_ids = [reminder.pk for reminder in reminders]
    if reminder_ids:
        reviews = MeetingResponseReminderReview.objects.filter(
            reminder_id__in=reminder_ids,
        ).order_by('reminder_id', '-reviewed_at', '-pk').only(
            'reminder_id', 'outcome', 'data_issue_reason', 'note',
            'reviewed_at',
        )
        for review in reviews:
            review_histories.setdefault(review.reminder_id, []).append({
                'outcome': review.outcome,
                'outcome_label': PENDING_REVIEW_OUTCOME_LABELS[review.outcome],
                'data_issue_reason': review.data_issue_reason,
                'data_issue_reason_label': (
                    review.get_data_issue_reason_display()
                    if review.data_issue_reason else ''
                ),
                'note': review.note,
                'reviewed_at_label': _format_local_datetime(review.reviewed_at),
            })

    rows = []
    for observation in observations:
        if observation['status'] not in PENDING_REVIEW_STATUS_LABELS:
            continue
        reminder = observation['reminder']
        proposal = reminder.proposal_event
        proposal_wait_hours = max(
            (now - proposal.occurred_at).total_seconds() / 3600.0,
            0.0,
        )
        reminder_age_hours = max(
            (now - reminder.sent_at).total_seconds() / 3600.0,
            0.0,
        )
        location_label = proposal.location_name
        if not location_label and proposal.location_id and proposal.location:
            location_label = proposal.location.name
        review_history = review_histories.get(reminder.pk, [])
        review = review_history[0] if review_history else None
        rows.append({
            'order_id': proposal.order_id,
            'role_label': dict(ROLE_LABELS).get(
                observation['role'], observation['role'],
            ),
            'proposal_at_label': _format_local_datetime(proposal.occurred_at),
            'proposal_start_label': _format_local_datetime(
                proposal.proposed_start_at,
            ),
            'proposal_end_label': _format_local_datetime(
                proposal.proposed_end_at,
            ),
            'location_label': location_label or '—',
            'sent_at_label': _format_local_datetime(reminder.sent_at),
            'proposal_wait_label': _label_hours(proposal_wait_hours),
            'reminder_age_label': _label_hours(reminder_age_hours),
            'notice_label': NOTICE_STATE_LABELS[observation['notice_state']],
            'status': observation['status'],
            'status_label': PENDING_REVIEW_STATUS_LABELS[observation['status']],
            'reminder_age_hours': reminder_age_hours,
            'sent_at': reminder.sent_at,
            'reminder_id': reminder.pk,
            'is_reviewed': review is not None,
            'review_outcome': review['outcome'] if review is not None else '',
            'review_outcome_label': (
                review['outcome_label'] if review is not None else '未核对'
            ),
            'data_issue_reason': (
                review['data_issue_reason'] if review is not None else ''
            ),
            'data_issue_reason_label': (
                review['data_issue_reason_label'] if review is not None else ''
            ),
            'reviewed_at_label': (
                review['reviewed_at_label'] if review is not None else '—'
            ),
            'review_note': review['note'] if review is not None else '',
            'review_history_count': len(review_history),
            'review_history': review_history,
        })
    rows.sort(key=lambda row: (
        row['status'] != 'no_action',
        -row['reminder_age_hours'],
        row['sent_at'],
        row['reminder_id'],
    ))
    return rows


def _notice_state(reminder):
    notification = reminder.notification
    if notification is None:
        return 'missing'
    if notification.is_read:
        return 'read' if notification.read_at is not None else 'inconsistent'
    if notification.read_at is not None:
        return 'inconsistent'
    return 'unread'


def _latency_bucket(hours):
    lower = 0.0
    for key, _label, upper in LATENCY_BUCKETS:
        if lower < hours <= upper:
            return key
        lower = upper
    return None


def _build_segment(key, label, observations):
    sent = len(observations)
    available = sum(row['notice_state'] != 'missing' for row in observations)
    read_state_known = sum(
        row['notice_state'] in DECIDABLE_NOTICE_STATES for row in observations
    )
    read = sum(row['notice_state'] == 'read' for row in observations)
    inconsistent_read_state = sum(
        row['notice_state'] == 'inconsistent' for row in observations
    )
    missing = sent - available
    accepted = sum(
        row['action_type'] == 'accepted' and row['within_window']
        for row in observations
    )
    declined = sum(
        row['action_type'] == 'declined' and row['within_window']
        for row in observations
    )
    rescheduled = sum(
        row['action_type'] == 'rescheduled' and row['within_window']
        for row in observations
    )
    responded = accepted + declined + rescheduled
    late = sum(
        row['action_type'] is not None and not row['within_window']
        for row in observations
    )
    open_samples = sum(row['status'] == 'open' for row in observations)
    no_action = sum(row['status'] == 'no_action' for row in observations)
    closed = responded + no_action + late
    latencies = [
        row['response_hours'] for row in observations
        if row['within_window'] and row['response_hours'] is not None
    ]
    return {
        'key': key,
        'label': label,
        'sent_count': sent,
        'notice_available_count': available,
        'read_state_known_count': read_state_known,
        'read_notice_count': read,
        'inconsistent_read_state_count': inconsistent_read_state,
        'missing_notice_count': missing,
        'notice_read_rate': _percent(read, read_state_known),
        'responded_count': responded,
        'accepted_count': accepted,
        'declined_count': declined,
        'rescheduled_count': rescheduled,
        'closed_count': closed,
        'response_rate': _percent(responded, closed),
        'open_count': open_samples,
        'no_action_count': no_action,
        'late_action_count': late,
        'median_response_hours': round(median(latencies), 1) if latencies else None,
        'median_response_label': _label_hours(median(latencies)) if latencies else '—',
        'comparable': closed >= MIN_SEGMENT_SAMPLES,
    }


def _build_latency_rows(observations):
    counts = {key: 0 for key, _label, _upper in LATENCY_BUCKETS}
    for row in observations:
        if not row['within_window'] or row['response_hours'] is None:
            continue
        bucket = _latency_bucket(row['response_hours'])
        if bucket:
            counts[bucket] += 1
    total = sum(counts.values())
    return [
        {
            'key': key,
            'label': label,
            'count': counts[key],
            'share': _percent(counts[key], total),
        }
        for key, label, _upper in LATENCY_BUCKETS
    ]


def _build_notice_action_rows(observations):
    """按可观测阅读状态构建窗口内行动与未行动的 2×2 交叉观察。"""
    rows = []
    for key, label in NOTICE_ACTION_STATES:
        cohort = [
            row for row in observations
            if row['notice_state'] == key and row['status'] != 'open'
        ]
        action_count = sum(row['within_window'] for row in cohort)
        closed = len(cohort)
        no_action_count = closed - action_count
        rows.append({
            'key': key,
            'label': label,
            'closed_count': closed,
            'action_count': action_count,
            'no_action_count': no_action_count,
            'action_rate': _percent(action_count, closed),
            'comparable': (
                action_count >= MIN_SEGMENT_SAMPLES
                and no_action_count >= MIN_SEGMENT_SAMPLES
            ),
        })
    return rows


def _notice_action_order(row):
    """只描述同一提醒的阅读与窗口内动作的时点先后。"""
    if not row['within_window'] or row['notice_state'] not in DECIDABLE_NOTICE_STATES:
        return None
    if row['notice_state'] == 'unread':
        return 'unread_action'
    read_at = row['read_at']
    action_at = row['action_at']
    if read_at is None or action_at is None:
        return None
    if read_at < action_at:
        return 'read_before_action'
    if action_at < read_at:
        return 'action_before_read'
    return 'same_time'


def _build_notice_order_rows(observations):
    counts = {key: 0 for key, _label in NOTICE_ORDER_STATES}
    for row in observations:
        key = _notice_action_order(row)
        if key:
            counts[key] += 1
    total = sum(counts.values())
    return [
        {
            'key': key,
            'label': label,
            'count': counts[key],
            'share': _percent(counts[key], total),
            'comparable': total >= MIN_SEGMENT_SAMPLES,
        }
        for key, label in NOTICE_ORDER_STATES
    ]


def _build_sample_quality(all_segment, notice_action_rows, legacy_notice_count):
    """给出数据覆盖状态；异常只提示核对，不据此判断提醒效果。"""
    sent = all_segment['sent_count']
    notice_action_closed_count = sum(
        row['closed_count'] for row in notice_action_rows
    )
    missing = all_segment['missing_notice_count']
    inconsistent = all_segment['inconsistent_read_state_count']
    open_samples = all_segment['open_count']
    closed = all_segment['closed_count']
    issues = []
    if missing:
        issues.append('通知不可观测 %d 条' % missing)
    if inconsistent:
        issues.append('阅读字段不一致 %d 条' % inconsistent)
    if legacy_notice_count:
        issues.append('台账外通知 %d 条' % legacy_notice_count)
    if not all(row['comparable'] for row in notice_action_rows):
        issues.append(
            '交叉组合尚未同时达到每格 %d 条门槛' % MIN_SEGMENT_SAMPLES
        )
    if open_samples:
        issues.append('仍在观察中 %d 条' % open_samples)

    if not sent:
        status = 'empty'
        label = '待积累'
    elif missing or inconsistent or legacy_notice_count:
        status = 'needs_review'
        label = '需核对'
    elif (
        not all(row['comparable'] for row in notice_action_rows)
        or open_samples
    ):
        status = 'accumulating'
        label = '数据积累中'
    else:
        status = 'complete'
        label = '样本可观察'

    if issues:
        note = '；'.join(issues) + '。这些提示只描述数据边界，不据此调整提醒。'
    else:
        note = '通知与交叉样本均达到当前展示门槛，仍只观察关联，不据此调整提醒。'

    return {
        'status': status,
        'label': label,
        'note': note,
        'notice_coverage_rate': _percent(
            all_segment['notice_available_count'], sent,
        ),
        'notice_action_closed_count': notice_action_closed_count,
        'notice_action_closed_rate': _percent(notice_action_closed_count, closed),
    }


def build_meeting_response_reminder_effect(
    *, days=30, now=None, review_status='unreviewed',
):
    """构建页面与 CSV 共用的提醒效果与人工核对面板。"""
    now = now or timezone.now()
    review_status = normalize_pending_review_filter(review_status)
    start, start_date = _period_bounds(days, now)
    window_ends_before = now - timedelta(hours=EFFECT_WINDOW_HOURS)
    reminders = list(
        MeetingResponseReminderEvent.objects.filter(
            sent_at__gte=start, sent_at__lte=now,
        ).select_related(
            'proposal_event__location', 'notification',
        ).order_by('sent_at', 'pk')
    )
    events_by_order = _load_events(reminders)

    observations = []
    for reminder in reminders:
        action = _first_action_after(
            events_by_order.get(reminder.proposal_event.order_id, ()),
            reminder.proposal_event,
            reminder.sent_at,
        )
        response_hours = None
        action_type = None
        within_window = False
        if action is not None:
            action_type = action.event_type
            response_hours = max(
                (action.occurred_at - reminder.sent_at).total_seconds() / 3600.0,
                0.0,
            )
            within_window = response_hours <= EFFECT_WINDOW_HOURS

        if within_window:
            status = 'responded'
        elif action is not None:
            status = 'late_action'
        elif reminder.sent_at <= window_ends_before:
            status = 'no_action'
        else:
            status = 'open'

        observations.append({
            'reminder': reminder,
            'role': reminder.recipient_role,
            'notice_state': _notice_state(reminder),
            'read_at': (
                reminder.notification.read_at
                if reminder.notification is not None else None
            ),
            'action_type': action_type,
            'action_at': action.occurred_at if action is not None else None,
            'response_hours': response_hours,
            'within_window': within_window,
            'status': status,
        })

    all_segment = _build_segment('all', '全部', observations)
    role_rows = [
        _build_segment(role, label, [row for row in observations if row['role'] == role])
        for role, label in ROLE_LABELS
    ]
    latency_rows = _build_latency_rows(observations)
    pending_review_candidates = _build_pending_review_rows(observations, now)
    pending_review_total_count = len(pending_review_candidates)
    pending_review_status_counts = {
        'unreviewed': sum(
            not row['is_reviewed'] for row in pending_review_candidates
        ),
        'waiting': sum(
            row['review_outcome'] == 'waiting'
            for row in pending_review_candidates
        ),
        'resolved': sum(
            row['review_outcome'] == 'resolved'
            for row in pending_review_candidates
        ),
        'data_issue': sum(
            row['review_outcome'] == 'data_issue'
            for row in pending_review_candidates
        ),
        'no_followup': sum(
            row['review_outcome'] == 'no_followup'
            for row in pending_review_candidates
        ),
        'all': pending_review_total_count,
    }
    pending_review_all_rows = [
        row for row in pending_review_candidates
        if (
            review_status == 'all'
            or (
                review_status == 'unreviewed' and not row['is_reviewed']
            )
            or row['review_outcome'] == review_status
        )
    ]
    pending_review_rows = pending_review_all_rows[:PENDING_REVIEW_PAGE_LIMIT]
    pending_review_filter_options = [
        {
            'key': key,
            'label': label,
            'count': pending_review_status_counts[key],
            'selected': key == review_status,
        }
        for key, label in PENDING_REVIEW_FILTER_OPTIONS
    ]
    notice_action_rows = _build_notice_action_rows(observations)
    notice_order_rows = _build_notice_order_rows(observations)
    notice_order_known_count = sum(row['count'] for row in notice_order_rows)
    notice_action_closed_count = sum(
        row['closed_count'] for row in notice_action_rows
    )

    linked_notification_ids = [
        reminder.notification_id for reminder in reminders
        if reminder.notification_id is not None
    ]
    legacy_notices = Notification.objects.filter(
        kind=NOTIFICATION_KIND,
        created_at__gte=start,
        created_at__lte=now,
    )
    if linked_notification_ids:
        legacy_notices = legacy_notices.exclude(pk__in=linked_notification_ids)
    legacy_notice_count = legacy_notices.count()
    sample_quality = _build_sample_quality(
        all_segment, notice_action_rows, legacy_notice_count,
    )

    if not reminders:
        summary = (
            '周期内还没有交付安排待回应提醒台账。旧通知不会按去重键补造发送样本；'
            '新提醒发送后才会进入这里。'
        )
    elif all_segment['closed_count'] < MIN_SEGMENT_SAMPLES:
        summary = (
            '已过观察窗的样本少于 %d 条，只报告计数与时延，不判断提醒效果方向。'
            % MIN_SEGMENT_SAMPLES
        )
    else:
        summary = (
            '已过观察窗的 %d 条提醒中，%d 条在 72 小时内出现同一提议回合的后续动作；'
            '这是观察值，不表示动作由提醒引起。'
            % (all_segment['closed_count'], all_segment['responded_count'])
        )

    return {
        'has_data': bool(reminders),
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'period_start_date': start_date,
        'window_hours': EFFECT_WINDOW_HOURS,
        'window_ends_before': window_ends_before,
        'min_samples': MIN_SEGMENT_SAMPLES,
        'summary': summary,
        'sample_note': (
            '发送分母来自周期内不可变提醒台账；'
            '窗口回应率 = 72 小时内出现接受、拒绝或改期的关闭样本 ÷ 已过观察窗样本；'
            '通知阅读率 = 已读 ÷ 阅读状态可判定样本。'
        ),
        'source_note': (
            '旧通知缺少台账时不计入效果分母，单列为未计入；通知删除后仍保留发送样本，'
            '但阅读状态记为不可观测；is_read 与 read_at 冲突时记为字段不一致。'
        ),
        'causality_note': (
            '后续动作只说明发生在提醒之后，不能证明由提醒引起；本面板不自动推进预约、'
            '不提高发送频率，也不按用户或角色排名。'
        ),
        'notice_action_note': (
            '交叉分母只含阅读状态可判定且已过观察窗的样本；观察中不进入，窗后动作按'
            '窗口内未行动计，通知不可观测或阅读字段不一致也不归入未读。已读未行动或'
            '未读但行动只是线索，不能单独证明文案失效或阅读记录有误。'
        ),
        'notice_order_note': (
            '时点表只比较同一提醒的 read_at 与窗口内动作 occurred_at；阅读字段不一致、'
            '通知删除、未行动和窗后动作不进入。阅读发生在动作之后仍保留为观察事实；'
            '先后关系不等于因果，也不据此调整提醒。'
        ),
        'pending_review_note': (
            '待人工核对清单只列截至当前仍没有同一提议回合动作的提醒；已过观察窗的'
            '优先，其余仍在等待。每次核对追加一条历史记录，页面按最新结论筛选和展示，'
            '默认聚焦未核对。人工结论不改变订单、预约、通知或提醒发送台账，也不会'
            '自动回应或推进预约。页面展示当前筛选前 20 条，CSV 导出当前筛选完整清单。'
        ),
        'review_status': review_status,
        'review_status_label': dict(PENDING_REVIEW_FILTER_OPTIONS)[review_status],
        'review_filter_options': pending_review_filter_options,
        'review_outcome_options': MeetingResponseReminderReview.OUTCOME_CHOICES,
        'review_data_issue_reason_options': (
            MeetingResponseReminderReview.DATA_ISSUE_REASON_CHOICES
        ),
        'pending_review_count': len(pending_review_all_rows),
        'pending_review_rows': pending_review_rows,
        'pending_review_overflow_count': max(
            len(pending_review_all_rows) - len(pending_review_rows), 0,
        ),
        'pending_review_all_rows': pending_review_all_rows,
        'pending_review_total_count': pending_review_total_count,
        'pending_review_unreviewed_count': pending_review_status_counts['unreviewed'],
        'pending_review_reviewed_count': (
            pending_review_total_count - pending_review_status_counts['unreviewed']
        ),
        'pending_review_waiting_count': pending_review_status_counts['waiting'],
        'pending_review_resolved_count': pending_review_status_counts['resolved'],
        'pending_review_data_issue_count': pending_review_status_counts['data_issue'],
        'pending_review_no_followup_count': pending_review_status_counts['no_followup'],
        'pending_review_empty_note': (
            '当前筛选下没有仍无同一回合动作的提醒。'
            if pending_review_all_rows else (
                '当前没有仍无同一回合动作、需要人工核对的提醒。'
                if not pending_review_total_count else
                '当前筛选下没有符合条件的核对记录，可切换到“全部”查看其他状态。'
            )
        ),
        'quality_status': sample_quality['status'],
        'quality_label': sample_quality['label'],
        'quality_note': sample_quality['note'],
        'notice_coverage_rate': sample_quality['notice_coverage_rate'],
        'notice_action_closed_count': notice_action_closed_count,
        'notice_action_closed_rate': sample_quality['notice_action_closed_rate'],
        'action_labels': ACTION_LABELS,
        'sent_count': all_segment['sent_count'],
        'notice_available_count': all_segment['notice_available_count'],
        'read_state_known_count': all_segment['read_state_known_count'],
        'read_notice_count': all_segment['read_notice_count'],
        'inconsistent_read_state_count': all_segment['inconsistent_read_state_count'],
        'notice_read_rate': all_segment['notice_read_rate'],
        'missing_notice_count': all_segment['missing_notice_count'],
        'closed_count': all_segment['closed_count'],
        'responded_count': all_segment['responded_count'],
        'accepted_count': all_segment['accepted_count'],
        'declined_count': all_segment['declined_count'],
        'rescheduled_count': all_segment['rescheduled_count'],
        'response_rate': all_segment['response_rate'],
        'open_count': all_segment['open_count'],
        'no_action_count': all_segment['no_action_count'],
        'late_action_count': all_segment['late_action_count'],
        'median_response_hours': all_segment['median_response_hours'],
        'median_response_label': all_segment['median_response_label'],
        'comparable': all_segment['comparable'],
        'role_rows': role_rows,
        'notice_action_rows': notice_action_rows,
        'notice_order_rows': notice_order_rows,
        'notice_order_known_count': notice_order_known_count,
        'latency_rows': latency_rows,
        'legacy_notice_count': legacy_notice_count,
    }
