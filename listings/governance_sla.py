# -*- coding: utf-8 -*-
"""Turn three separate moderation queues into one answerable question.

The project already handles reports, order disputes and meeting incidents, but
each queue lives on its own page with its own wording, and none of them records
how long handling actually took. The dashboard only knows an absolute backlog
count, so it cannot say whether a report waited ten minutes or ten days, cannot
compare one queue against another, and cannot tell which cases have crossed the
point where a user is still waiting for an answer.

A fourth and a fifth queue were added later, and neither is a model this module
owns. The fourth is a borrow escalated to level three: that is a case waiting on
an operator, and it already carries everything a case needs - when it entered
the queue, how long the item has been overdue, and when it closed. Reading it
here rather than building a mirror table keeps a single write path: the
escalation module still owns the record, this module only reports on it. The
fifth is a report raised against a lost-and-found record, which is a real model
of its own but the same shape: a reporter, a subject, a reason, a status and a
closing timestamp.

This module normalises the queues onto one comparable shape and measures
the dimension none of them had: handling latency. It deliberately does not add
a storage table. All three models already carry a created_at and a closing
timestamp, so a mirror table would only introduce a second write path and a
second chance to drift. The cost is that aggregation walks rows in Python
rather than pushing everything into SQL, which is acceptable because these
queues are small by nature - a campus platform handles hundreds of cases, not
millions.

Response time is measured from the moment a reviewer is written, never from a
page view. An admin opening the queue is not a response; if it counted, "fast
response" would degenerate into "refreshes the page often" and the metric would
be worthless. Latency is derived in Python for the same reason the notification
module does it: subtracting datetime columns is not portable across backends,
and SQLite overflows on the operation.
"""

from datetime import timedelta

from django.utils import timezone

from .models import (
    BorrowReturnEscalation, LostFoundReport, MeetingIncident, OrderDispute, Report,
)
from .report_priority import build_report_priority, open_report_counts


# Each queue's handling deadline. These are deliberately different: a meeting
# incident blocks a live handover and degrades within hours, while a report is
# an offline review that can wait a day or two. Using one shared deadline would
# either drown the incident queue in false alarms or let reports rot.
SLA_TARGETS = {
    'report': timedelta(days=2),
    'dispute': timedelta(days=3),
    'incident': timedelta(hours=24),
    # A level-three borrow has already waited out the whole private channel:
    # the borrower was asked, both sides were told, and seven days of overdue
    # have passed. Another seven days on the operator side bounds the worst case
    # at two weeks of a classmate's item being gone, which is long enough for a
    # dispute to be opened anyway but short enough to still be recoverable.
    'borrow': timedelta(days=7),
    # 失物招领举报和交付预约异常同为 24 小时，但理由完全不同，写下来是因为
    # 两个数字一样不代表可以合并成一条规则。预约异常等的是"这一次见面还能不能
    # 改成不白跑"；失物招领等的是"会不会有人已经拿着假身份去把东西认走了"。
    # 前者过了时限损失的是一次见面的时间，后者过了时限可能连东西都没了，所以
    # 不能因为商品举报能等两天，就默认失物举报也能等两天。
    'lost_found_report': timedelta(hours=24),
}

# How long before the deadline a case is flagged as approaching it. Used to
# surface work that is still on time but about not to be.
SLA_WARNING_RATIO = 0.75

# Latency buckets, coarse on purpose. The question is whether a queue is handled
# at all, not the exact number of minutes.
LATENCY_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (604800, '1 周内'),
    (None, '1 周以上'),
)

QUEUE_LABELS = {
    'report': '商品举报',
    'dispute': '交易争议',
    'incident': '交付预约异常',
    'borrow': '借用逾期催收',
    'lost_found_report': '失物招领举报',
}

# Statuses that mean a case is still waiting for a human. Everything else has
# been closed and only contributes to the latency statistics.
OPEN_STATUSES = ('pending', 'reviewing', 'open')

PERIOD_FIELDS = (
    'status', 'created_at', 'closed_at', 'reviewer_id',
)


def _format_duration(seconds):
    """Render a latency in the coarsest unit that stays readable."""
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} 秒'
    if seconds < 3600:
        return f'{seconds // 60} 分钟'
    if seconds < 86400:
        return f'{seconds // 3600} 小时'
    return f'{seconds // 86400} 天'


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _bucket_index(seconds):
    for index, (limit, _label) in enumerate(LATENCY_BUCKETS):
        if limit is None or seconds < limit:
            return index
    return len(LATENCY_BUCKETS) - 1


def normalise_case(*, kind, status, created_at, closed_at, reviewer_id, target=None, **extra):
    """Map one queue row onto the shared shape the aggregation consumes.

    The extra keyword arguments carry the per-queue payload (title, subject,
    reason and so on). They are passed through untouched, so a queue can add
    fields without this function having to know about them.
    """
    target = target or SLA_TARGETS[kind]
    case = {
        'kind': kind,
        'kind_label': QUEUE_LABELS[kind],
        'status': status,
        'created_at': created_at,
        'closed_at': closed_at,
        'reviewer_id': reviewer_id,
        'is_open': status in OPEN_STATUSES,
        'sla_seconds': target.total_seconds(),
        'sla_label': _format_duration(target.total_seconds()),
    }
    case.update(extra)
    # A case can be loaded twice - once for the period statistics and once for
    # the live backlog - and must then be counted once. Python's id() cannot
    # serve: it is the address of a throwaway dict, so two loads of the same
    # row yield two different keys and the row gets counted twice.
    case['case_key'] = (kind, extra.get('case_id'))
    return case


def _report_cases(queryset, now):
    rows = []
    open_counts = open_report_counts()
    for row in queryset.values(
        'id', 'item_id', 'status', 'created_at', 'reviewed_at', 'reviewer_id', 'reason',
        'item__status', 'item__expires_at', 'item__title', 'item__seller__username',
        'reporter__username',
    ):
        is_open = row['status'] in OPEN_STATUSES
        priority = build_report_priority(
            reason=row['reason'],
            item_status=row['item__status'],
            item_expires_at=row['item__expires_at'],
            open_report_count=open_counts.get(row['item_id'], 1 if is_open else 0),
            is_open=is_open,
            now=now,
        )
        rows.append(normalise_case(
            case_id=row['id'],
            kind='report',
            status=row['status'],
            created_at=row['created_at'],
            closed_at=row['reviewed_at'],
            reviewer_id=row['reviewer_id'],
            reason=row['reason'],
            reason_label=dict(Report.REASON_CHOICES).get(row['reason'], row['reason']),
            subject=row['item__title'],
            counterparty=row['item__seller__username'],
            reporter=row['reporter__username'],
            url=f"/listings/reports/{row['id']}/review/",
            **priority,
        ))
    return rows


def _dispute_cases(queryset):
    rows = []
    for row in queryset.values(
        'id', 'status', 'created_at', 'resolved_at', 'reviewer_id', 'reason',
        'order__item__title', 'order__buyer__username', 'order__seller__username',
        'opened_by__username',
    ):
        rows.append(normalise_case(
            case_id=row['id'],
            kind='dispute',
            status=row['status'],
            created_at=row['created_at'],
            closed_at=row['resolved_at'],
            reviewer_id=row['reviewer_id'],
            reason=row['reason'],
            reason_label=dict(OrderDispute.REASON_CHOICES).get(row['reason'], row['reason']),
            subject=row['order__item__title'],
            counterparty=row['order__seller__username'],
            reporter=row['opened_by__username'],
            url=f"/listings/disputes/{row['id']}/resolve/",
        ))
    return rows


def _incident_cases(queryset):
    rows = []
    for row in queryset.values(
        'id', 'status', 'created_at', 'reviewed_at', 'reviewer_id', 'reason',
        'appointment__order__item__title', 'accused__username', 'reported_by__username',
    ):
        rows.append(normalise_case(
            case_id=row['id'],
            kind='incident',
            status=row['status'],
            created_at=row['created_at'],
            closed_at=row['reviewed_at'],
            reviewer_id=row['reviewer_id'],
            reason=row['reason'],
            reason_label=dict(MeetingIncident.REASON_CHOICES).get(row['reason'], row['reason']),
            subject=row['appointment__order__item__title'],
            counterparty=row['accused__username'],
            reporter=row['reported_by__username'],
            url=f"/listings/governance/incidents/{row['id']}/review/",
        ))
    return rows

def _borrow_cases(queryset):
    """Read escalated borrows as governance cases.

    A case enters this queue at the moment it reaches level three, so the
    creation time is ``last_escalated_at`` and not the row's own created_at.
    The difference matters: an order may sit at level one for a week before it
    is escalated, and measuring the operator's wait from the level-one row
    would charge the governance queue for time the escalation module spent.
    """
    rows = []
    for row in queryset.values(
        'id', 'escalation_level', 'escalation_count', 'last_escalated_at',
        'resolved_at', 'note', 'order__id', 'order__item__title',
        'order__buyer__username', 'order__seller__username',
        'order__return_due_at',
    ):
        # 只把真正离开双方私域的那一级算成治理事项：L1/L2 仍在两个同学之间，
        # 进工作台等于把一次慢还变成公开示众。
        if row['escalation_level'] < 3:
            continue
        rows.append(normalise_case(
            case_id=row['id'],
            kind='borrow',
            status='closed' if row['resolved_at'] else 'open',
            created_at=row['last_escalated_at'],
            closed_at=row['resolved_at'],
            reviewer_id=None,
            subject=row['order__item__title'],
            counterparty=row['order__seller__username'],
            reporter=row['order__buyer__username'],
            reason='overdue_borrow',
            reason_label='借用逾期未归还',
            escalation_level=row['escalation_level'],
            escalation_count=row['escalation_count'],
            return_due_at=row['order__return_due_at'],
            note=row['note'],
            url=f"/listings/order/{row['order__id']}/",
        ))
    return rows


def _lost_found_report_cases(queryset):
    """Read lost-and-found reports as governance cases.

    Unlike ``Report``, this model is not keyed to a listing: the subject is the
    lost-and-found record, and the counterparty is whoever published it. The
    closing timestamp is ``reviewed_at`` because that is the only moment the
    platform records that a human has looked at the report - opening the queue
    is not a response.
    """
    rows = []
    for row in queryset.values(
        'id', 'status', 'created_at', 'reviewed_at', 'reviewer_id', 'reason',
        'post__title', 'post__status', 'post__expires_at',
        'post__reporter__username', 'reporter__username',
    ):
        rows.append(normalise_case(
            case_id=row['id'],
            kind='lost_found_report',
            status=row['status'],
            created_at=row['created_at'],
            closed_at=row['reviewed_at'],
            reviewer_id=row['reviewer_id'],
            reason=row['reason'],
            reason_label=dict(LostFoundReport.REASON_CHOICES).get(row['reason'], row['reason']),
            subject=row['post__title'],
            counterparty=row['post__reporter__username'],
            reporter=row['reporter__username'],
            post_status=row['post__status'],
            post_expires_at=row['post__expires_at'],
            url=f"/listings/lost-found/reports/{row['id']}/review/",
        ))
    return rows


def _fetch_cases(*, days, now):
    """Load every case created in the period, whatever queue it came from."""
    start = now - timedelta(days=days)
    cases = []
    cases += _report_cases(Report.objects.filter(created_at__gte=start, created_at__lte=now), now)
    cases += _dispute_cases(OrderDispute.objects.filter(created_at__gte=start, created_at__lte=now))
    cases += _incident_cases(MeetingIncident.objects.filter(created_at__gte=start, created_at__lte=now))
    cases += _lost_found_report_cases(LostFoundReport.objects.filter(
        created_at__gte=start, created_at__lte=now,
    ))
    cases += _borrow_cases(BorrowReturnEscalation.objects.filter(
        escalation_level__gte=3, last_escalated_at__gte=start, last_escalated_at__lte=now,
    ))
    return cases


def _fetch_open_cases(*, now):
    """Load every case still waiting, regardless of when it was opened.

    Overdue work must not be hidden by the statistics window: a case opened
    before the period and still untouched is exactly the one an operator needs
    to see, so the open backlog is fetched without a date filter.
    """
    cases = []
    cases += _report_cases(Report.objects.filter(status__in=OPEN_STATUSES), now=now)
    cases += _dispute_cases(OrderDispute.objects.filter(status__in=OPEN_STATUSES))
    cases += _incident_cases(MeetingIncident.objects.filter(status__in=OPEN_STATUSES))
    cases += _lost_found_report_cases(LostFoundReport.objects.filter(status__in=OPEN_STATUSES))
    cases += _borrow_cases(BorrowReturnEscalation.objects.filter(
        escalation_level__gte=3, resolved_at__isnull=True,
    ))
    return cases


def _waiting_seconds(case, now):
    """How long a case has been waiting, using the closing time when closed."""
    end = case['closed_at'] or now
    return max((end - case['created_at']).total_seconds(), 0.0)


def _age_seconds(case, now):
    """How long an open case has waited so far, measured against now."""
    return max((now - case['created_at']).total_seconds(), 0.0)


def _breach_level(seconds, sla_seconds):
    """Classify a wait against its queue's deadline."""
    if seconds is None:
        return 'ok'
    if seconds > sla_seconds:
        return 'breached'
    if seconds >= sla_seconds * SLA_WARNING_RATIO:
        return 'warning'
    return 'ok'


def _case_row(case, now):
    """A single case as the queue page and dashboard need it."""
    seconds = _age_seconds(case, now)
    level = _breach_level(seconds, case['sla_seconds']) if case['is_open'] else 'closed'
    return {
        **case,
        'age_seconds': seconds,
        'age_label': _format_duration(seconds),
        'sla_level': level,
        'is_overdue': case['is_open'] and level == 'breached',
        'remaining_seconds': max(case['sla_seconds'] - seconds, 0.0) if case['is_open'] else None,
        'remaining_label': _format_duration(max(case['sla_seconds'] - seconds, 0.0)) if case['is_open'] else '—',
    }


def _queue_row(kind, cases, now):
    """Aggregate one queue: volume, latency and the current backlog."""
    closed = [case for case in cases if not case['is_open']]
    open_cases = [case for case in cases if case['is_open']]
    latencies = [_waiting_seconds(case, now) for case in closed]
    overdue = [case for case in open_cases if _breach_level(_age_seconds(case, now), case['sla_seconds']) == 'breached']
    warning = [
        case for case in open_cases
        if _breach_level(_age_seconds(case, now), case['sla_seconds']) == 'warning'
    ]

    avg_seconds = _mean(latencies)
    median_seconds = _percentile(latencies, 0.5)
    p90_seconds = _percentile(latencies, 0.9)
    sla_seconds = cases[0]['sla_seconds'] if cases else SLA_TARGETS[kind].total_seconds()

    buckets = [{'label': label, 'count': 0} for _limit, label in LATENCY_BUCKETS]
    for seconds in latencies:
        buckets[_bucket_index(seconds)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = round(bucket['count'] / len(latencies) * 100, 1) if latencies else 0

    return {
        'kind': kind,
        'label': QUEUE_LABELS[kind],
        'total': len(cases),
        'closed': len(closed),
        'open': len(open_cases),
        'overdue': len(overdue),
        'warning': len(warning),
        'overdue_rate': round(len(overdue) / len(cases) * 100, 1) if cases else 0,
        'avg_seconds': avg_seconds,
        'avg_label': _format_duration(avg_seconds),
        'median_seconds': median_seconds,
        'median_label': _format_duration(median_seconds),
        'p90_seconds': p90_seconds,
        'p90_label': _format_duration(p90_seconds),
        'fastest_label': _format_duration(min(latencies)) if latencies else '—',
        'slowest_label': _format_duration(max(latencies)) if latencies else '—',
        'sla_label': _format_duration(sla_seconds),
        'sla_seconds': sla_seconds,
        'within_sla_count': sum(1 for seconds in latencies if seconds <= sla_seconds),
        'within_sla_rate': round((len(closed) - sum(1 for s in latencies if s > sla_seconds)) / len(closed) * 100, 1) if closed else None,
        'latency_buckets': buckets,
        'cases': [
            _case_row(case, now)
            for case in sorted(cases, key=lambda item: (item['is_open'] is False, item['created_at']))
        ],
        'open_cases': [_case_row(case, now) for case in sorted(open_cases, key=lambda item: item['created_at'])],
    }


def _summary(queues, now):
    """One sentence describing the state of governance, most urgent first."""
    total_open = sum(queue['open'] for queue in queues)
    total_overdue = sum(queue['overdue'] for queue in queues)
    if total_overdue:
        worst = max(queues, key=lambda queue: queue['overdue'])
        return f'有 {total_overdue} 件治理事项已超过处理时限，其中{worst["label"]}队列 {worst["overdue"]} 件，建议优先处理。'
    if total_open:
        return f'当前 {total_open} 件治理事项都在处理时限内，按队列分别跟进即可。'
    return '当前没有待处理的治理事项，所有队列均已清空。'


def build_governance_sla(*, days=30, now=None):
    """Aggregate the three moderation queues into one latency view.

    Two windows are involved and the difference matters. The statistics cover
    cases created inside the period, so a case that was opened earlier and
    closed during it is not counted as handled work. The backlog, however, is
    loaded without a date filter, because overdue work opened before the
    period is precisely what an operator must not miss.
    """
    now = now or timezone.now()

    period_cases = _fetch_cases(days=days, now=now)
    open_cases = _fetch_open_cases(now=now)

    queues = []
    for kind in ('report', 'lost_found_report', 'dispute', 'incident', 'borrow'):
        # The borrow queue is listed last on purpose. It is the only one whose
        # deadline is not a moderation target but the tail of an escalation
        # ladder, so it belongs after the queues an operator can act on directly.
        # 失物招领举报紧跟商品举报：两者都是"有人举报了一条内容"，处理动作也
        # 一样（看记录、下结论、通知举报人），放在一起才看得出两者的时限差一倍，
        # 而这个差是本模块唯一想让人看见的事。
        kind_period = [case for case in period_cases if case['kind'] == kind]
        kind_open = [case for case in open_cases if case['kind'] == kind]
        # Merge so the queue row shows both the period's throughput and the
        # live backlog, without double counting a case that appears in both.
        merged = {case['case_key']: case for case in kind_period}
        for case in kind_open:
            merged.setdefault(case['case_key'], case)
        queues.append(_queue_row(kind, list(merged.values()), now))

    all_overdue = []
    for queue in queues:
        for case in queue['cases']:
            if case['is_overdue']:
                all_overdue.append(case)

    total_closed = sum(queue['closed'] for queue in queues)
    total_within = sum(queue['within_sla_count'] for queue in queues)

    recommendations = []
    for queue in sorted(queues, key=lambda item: -item['overdue']):
        if queue['overdue']:
            recommendations.append(
                f"{queue['label']}有 {queue['overdue']} 件已超过 {queue['sla_label']}处理时限，"
                f"其中最早一件已等待 {_format_duration(max(c['age_seconds'] for c in queue['cases'] if c['is_overdue']))}。"
            )
        elif queue['open'] and queue['median_seconds'] and queue['median_seconds'] > queue['sla_seconds'] * 0.5:
            recommendations.append(
                f"{queue['label']}虽然都在时限内，但处理时长中位数已达 {queue['median_label']}，接近 {queue['sla_label']}上限。"
            )
    if not recommendations and total_closed:
        recommendations.append('各治理队列都在处理时限内，暂时不需要调整审核人力。')

    return {
        'days': days,
        'queues': queues,
        'queue_labels': QUEUE_LABELS,
        'total_cases': sum(queue['total'] for queue in queues),
        'total_closed': total_closed,
        'total_open': sum(queue['open'] for queue in queues),
        'total_overdue': len(all_overdue),
        'overdue_cases': sorted(all_overdue, key=lambda case: -case['age_seconds']),
        'within_sla_rate': round(total_within / total_closed * 100, 1) if total_closed else None,
        'summary': _summary(queues, now),
        'recommendations': recommendations,
        'has_data': bool(period_cases),
    }
