# -*- coding: utf-8 -*-
"""交付预约的首次回应、轮次结果和到场情况。

运行时的 ``MeetingAppointmentEvent`` 记录首次提议、改期及每轮回应。新预约的
首次回应时延取首次提议到首次回应的真实间隔，后续改期不会污染它；上线前没有
事件链的旧预约继续使用 ``responded_at - created_at`` 的兼容口径，并单独披露。

到场统计仍读当前预约快照。它只报告，不改签到窗口，也不自动判定爽约。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .meeting_events import (
    first_response_seconds,
    has_complete_meeting_history,
    load_meeting_events_by_order,
    proposal_events,
    proposal_response_pairs,
    response_events,
)
from .models import MeetingAppointment, MeetingIncident

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

RESPONSE_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (None, '3 天以上'),
)

LATENESS_BUCKETS = (
    (None, '提前到'),
    (900, '准点'),
    (3600, '迟到 15 分钟内'),
    (86400, '迟到 1 小时内'),
    (None, '迟到 1 小时以上'),
)

MIN_MEASURED_APPOINTMENTS = 12
NOISE_POINTS = 8
CONTRADICTION_LIMIT = 20
STATUS_LABELS = dict(MeetingAppointment.STATUS_CHOICES)
CONFIRMED_STATUS = 'confirmed'
ATTENDANCE_RELATED_REASON = 'no_show'
INCIDENT_STATUS_LABELS = dict(MeetingIncident.STATUS_CHOICES)
INCIDENT_REASON_LABELS = dict(MeetingIncident.REASON_CHOICES)


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _fetch_appointments(*, created_after=None, created_before=None,
                        start_after=None, start_before=None):
    appointments = MeetingAppointment.objects.select_related(
        'order', 'order__item', 'order__buyer', 'order__seller', 'location',
        'proposed_by',
    ).order_by('created_at', 'id')
    if created_after is not None:
        appointments = appointments.filter(created_at__gte=created_after)
    if created_before is not None:
        appointments = appointments.filter(created_at__lte=created_before)
    if start_after is not None:
        appointments = appointments.filter(start_at__gte=start_after)
    if start_before is not None:
        appointments = appointments.filter(start_at__lte=start_before)
    return list(appointments)


def _fetch_incidents(*, created_after=None, created_before=None):
    incidents = MeetingIncident.objects.select_related(
        'appointment', 'appointment__order', 'appointment__order__item',
        'appointment__order__buyer', 'appointment__order__seller',
        'reported_by', 'accused',
    ).order_by('created_at', 'id')
    if created_after is not None:
        incidents = incidents.filter(created_at__gte=created_after)
    if created_before is not None:
        incidents = incidents.filter(created_at__lte=created_before)
    return list(incidents)


def _legacy_response_seconds(appointment):
    if not appointment.responded_at or not appointment.created_at:
        return None
    return max((appointment.responded_at - appointment.created_at).total_seconds(), 0.0)


def _is_confirmed(appointment):
    return appointment.status == CONFIRMED_STATUS


def _has_ended(appointment, now):
    return appointment.end_at is not None and appointment.end_at <= now


def _arrival_offset_seconds(appointment, field_name):
    arrived_at = getattr(appointment, field_name)
    if not arrived_at or not appointment.start_at:
        return None
    return (arrived_at - appointment.start_at).total_seconds()


def _lateness_bucket_index(offset_seconds):
    if offset_seconds is None:
        return None
    if offset_seconds < 0:
        return 0
    for index, (limit, _label) in enumerate(LATENESS_BUCKETS):
        if index == 0:
            continue
        if limit is None or offset_seconds < limit:
            return index
    return len(LATENESS_BUCKETS) - 1


def _bucket_index(seconds):
    for index, (limit, _label) in enumerate(RESPONSE_BUCKETS):
        if limit is None or seconds < limit:
            return index
    return len(RESPONSE_BUCKETS) - 1


def _format_duration(seconds):
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


def _format_offset(seconds):
    if seconds is None:
        return '—'
    if seconds == 0:
        return '准点'
    sign = '提前' if seconds < 0 else '迟到'
    seconds = abs(int(seconds))
    if seconds < 60:
        return f'{sign}{seconds} 分钟'
    if seconds < 3600:
        return f'{sign}{seconds // 60} 分钟'
    return f'{sign}{seconds // 3600} 小时'


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _bucket_rows(seconds_list):
    buckets = [{'label': label, 'count': 0} for _limit, label in RESPONSE_BUCKETS]
    for seconds in seconds_list:
        buckets[_bucket_index(seconds)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(seconds_list))
    return buckets


def _build_round_stats(complete_items):
    """按完整事件链数每轮提议后的第一次接受/拒绝。"""
    accepted = 0
    declined = 0
    unanswered = 0
    proposals = 0
    for events in complete_items:
        proposals_for_order = proposal_events(events)
        proposals += len(proposals_for_order)
        pairs = proposal_response_pairs(events)
        accepted += sum(1 for _proposal, response in pairs if response.event_type == 'accepted')
        declined += sum(1 for _proposal, response in pairs if response.event_type == 'declined')
        unanswered += len(proposals_for_order) - len(pairs)
    answered = accepted + declined
    return {
        'proposal_count': proposals,
        'responded_count': len(
            [pair for events in complete_items for pair in proposal_response_pairs(events)]
        ),
        'accepted_count': accepted,
        'declined_count': declined,
        'unanswered_count': unanswered,
        'accept_rate': _percent(accepted, answered),
        'decline_rate': _percent(declined, answered),
    }


def _build_response_stats(appointments, events_by_order):
    """分别计算完整事件链和旧快照，总计只用于样本覆盖说明。"""
    exact_items = []
    legacy_items = []
    exact_seconds = []
    legacy_seconds = []
    exact_accepted_current = 0
    exact_declined_current = 0
    legacy_accepted_current = 0
    legacy_declined_current = 0

    for appointment in appointments:
        events = events_by_order.get(appointment.order_id, [])
        if has_complete_meeting_history(events):
            exact_items.append(events)
            seconds = first_response_seconds(events)
            if seconds is not None:
                exact_seconds.append(seconds)
            if appointment.status == 'confirmed':
                exact_accepted_current += 1
            elif appointment.status == 'declined':
                exact_declined_current += 1
        else:
            legacy_items.append(appointment)
            seconds = _legacy_response_seconds(appointment)
            if seconds is not None:
                legacy_seconds.append(seconds)
            if appointment.status == 'confirmed':
                legacy_accepted_current += 1
            elif appointment.status == 'declined':
                legacy_declined_current += 1

    responded_count = len(exact_seconds) + len(legacy_seconds)
    pending_count = sum(1 for a in appointments if a.status == 'pending')
    round_stats = _build_round_stats(exact_items)
    return {
        'total': len(appointments),
        'exact_total': len(exact_items),
        'legacy_total': len(legacy_items),
        'responded_count': responded_count,
        'exact_responded_count': len(exact_seconds),
        'legacy_responded_count': len(legacy_seconds),
        'pending_count': pending_count,
        'response_rate': _percent(responded_count, len(appointments)),
        'average_seconds': _mean(exact_seconds),
        'average_label': _format_duration(_mean(exact_seconds)),
        'median_label': _format_duration(_percentile(exact_seconds, 0.5)),
        'p90_label': _format_duration(_percentile(exact_seconds, 0.9)),
        'fastest_label': _format_duration(min(exact_seconds)) if exact_seconds else '—',
        'slowest_label': _format_duration(max(exact_seconds)) if exact_seconds else '—',
        # 兼容现有模板的总体分档；它只含完整事件链。
        'bucket_rows': _bucket_rows(exact_seconds),
        'exact_bucket_rows': _bucket_rows(exact_seconds),
        'legacy_bucket_rows': _bucket_rows(legacy_seconds),
        'legacy_median_label': _format_duration(_percentile(legacy_seconds, 0.5)),
        'accepted_count': exact_accepted_current + legacy_accepted_current,
        'declined_count': exact_declined_current + legacy_declined_current,
        'accept_rate': _percent(
            exact_accepted_current + legacy_accepted_current,
            responded_count,
        ),
        'round_stats': round_stats,
        'is_small_sample': len(exact_seconds) < MIN_MEASURED_APPOINTMENTS,
        'min_measured_appointments': MIN_MEASURED_APPOINTMENTS,
    }


def _merge_lateness_buckets(lateness_rows):
    if not lateness_rows:
        return []
    rows = []
    for index, bucket in enumerate(lateness_rows[0]['bucket_rows']):
        row = {'label': bucket['label']}
        for role_row in lateness_rows:
            key = 'buyer_count' if role_row['field_name'] == 'buyer_arrived_at' else 'seller_count'
            row[key] = role_row['bucket_rows'][index]['count']
        rows.append(row)
    return rows


def _build_attendance_stats(appointments, now):
    ended = [a for a in appointments if _has_ended(a, now)]
    still_open = [a for a in appointments if not _has_ended(a, now)]
    confirmed_ended = [a for a in ended if _is_confirmed(a)]
    buyer_arrived = [a for a in confirmed_ended if a.buyer_arrived_at]
    seller_arrived = [a for a in confirmed_ended if a.seller_arrived_at]
    both_arrived = [a for a in confirmed_ended if a.buyer_arrived_at and a.seller_arrived_at]
    neither_arrived = [a for a in confirmed_ended if not a.buyer_arrived_at and not a.seller_arrived_at]
    one_sided = [
        a for a in confirmed_ended
        if bool(a.buyer_arrived_at) != bool(a.seller_arrived_at)
    ]

    lateness_rows = []
    for field_name, role in (('buyer_arrived_at', '买家'), ('seller_arrived_at', '卖家')):
        offsets = [
            offset for offset in
            (_arrival_offset_seconds(a, field_name) for a in confirmed_ended)
            if offset is not None
        ]
        buckets = [{'label': label, 'count': 0} for _limit, label in LATENESS_BUCKETS]
        for offset in offsets:
            buckets[_lateness_bucket_index(offset)]['count'] += 1
        for bucket in buckets:
            bucket['share'] = _percent(bucket['count'], len(offsets))
        lateness_rows.append({
            'role': role,
            'field_name': field_name,
            'arrived_count': len(offsets),
            'arrival_rate': _percent(len(offsets), len(confirmed_ended)),
            'early_count': sum(1 for offset in offsets if offset < 0),
            'on_time_count': sum(1 for offset in offsets if offset >= 0),
            'late_count': sum(1 for offset in offsets if offset >= 900),
            'average_offset_seconds': _mean(offsets),
            'average_offset_label': _format_offset(_mean(offsets)),
            'median_offset_label': _format_offset(_percentile(offsets, 0.5)),
            'worst_offset_label': _format_offset(max(offsets)) if offsets else '—',
            'bucket_rows': buckets,
        })

    return {
        'ended_count': len(ended),
        'still_open_count': len(still_open),
        'confirmed_ended_count': len(confirmed_ended),
        'buyer_arrived_count': len(buyer_arrived),
        'seller_arrived_count': len(seller_arrived),
        'buyer_arrival_rate': _percent(len(buyer_arrived), len(confirmed_ended)),
        'seller_arrival_rate': _percent(len(seller_arrived), len(confirmed_ended)),
        'both_arrived_count': len(both_arrived),
        'both_arrival_rate': _percent(len(both_arrived), len(confirmed_ended)),
        'neither_arrived_count': len(neither_arrived),
        'one_sided_count': len(one_sided),
        'one_sided_rate': _percent(len(one_sided), len(confirmed_ended)),
        'lateness_rows': lateness_rows,
        'lateness_bucket_rows': _merge_lateness_buckets(lateness_rows),
        'is_small_sample': len(confirmed_ended) < MIN_MEASURED_APPOINTMENTS,
        'min_measured_appointments': MIN_MEASURED_APPOINTMENTS,
    }


def _accused_arrived_at(appointment, accused):
    if not appointment or not accused:
        return None
    order = appointment.order
    if accused == order.buyer:
        return appointment.buyer_arrived_at
    if accused == order.seller:
        return appointment.seller_arrived_at
    return None


def _build_incident_crosscheck(incidents, now):
    relevant = [i for i in incidents if i.reason == ATTENDANCE_RELATED_REASON]
    rows = []
    for incident in relevant:
        appointment = incident.appointment
        accused = incident.accused
        accused_arrived_at = _accused_arrived_at(appointment, accused)
        reporter_arrived_at = _accused_arrived_at(appointment, incident.reported_by)
        if accused_arrived_at:
            verdict = 'contradicted'
            verdict_label = '被指控方有到场登记'
        elif reporter_arrived_at:
            verdict = 'supported'
            verdict_label = '提交人到场、被指控方未登记'
        else:
            verdict = 'no_record'
            verdict_label = '双方都没有到场登记'
        rows.append({
            'incident_id': incident.id,
            'order_id': appointment.order_id,
            'item_title': appointment.order.item.title,
            'reason_label': incident.get_reason_display(),
            'status_label': incident.get_status_display(),
            'is_closed': incident.status in {'resolved', 'dismissed'},
            'reporter': incident.reported_by.username if incident.reported_by else '—',
            'accused': accused.username if accused else '—',
            'accused_arrived_at': accused_arrived_at,
            'accused_arrived_label': (
                timezone.localtime(accused_arrived_at).strftime('%Y-%m-%d %H:%M')
                if accused_arrived_at else '未登记'
            ),
            'reporter_arrived_label': (
                timezone.localtime(reporter_arrived_at).strftime('%Y-%m-%d %H:%M')
                if reporter_arrived_at else '未登记'
            ),
            'start_at': appointment.start_at,
            'submitted_at': incident.created_at,
            'verdict': verdict,
            'verdict_label': verdict_label,
            'incident_status': incident.status,
        })

    rows.sort(key=lambda row: (row['is_closed'], row['submitted_at'], row['incident_id']))
    by_verdict = {}
    for row in rows:
        by_verdict.setdefault(row['verdict'], []).append(row)
    open_rows = [row for row in rows if not row['is_closed']]
    return {
        'total': len(rows),
        'other_reason_count': len(incidents) - len(relevant),
        'contradicted_count': len(by_verdict.get('contradicted', [])),
        'supported_count': len(by_verdict.get('supported', [])),
        'no_record_count': len(by_verdict.get('no_record', [])),
        'contradicted_rate': _percent(
            len(by_verdict.get('contradicted', [])), len(rows),
        ),
        'open_count': len(open_rows),
        'contradiction_limit': CONTRADICTION_LIMIT,
        'contradicted_rows': by_verdict.get('contradicted', [])[:CONTRADICTION_LIMIT],
        'no_record_rows': by_verdict.get('no_record', [])[:CONTRADICTION_LIMIT],
        'is_small_sample': len(rows) < MIN_MEASURED_APPOINTMENTS,
        'min_measured_appointments': MIN_MEASURED_APPOINTMENTS,
    }


def _build_decline_stats(appointments, events_by_order):
    """完整链读历史拒绝事件，旧快照读当前拒绝字段。"""
    records = []
    for appointment in appointments:
        events = events_by_order.get(appointment.order_id, [])
        if has_complete_meeting_history(events):
            records.extend(
                event.decline_reason or '' for event in response_events(events)
                if event.event_type == 'declined'
            )
        elif appointment.status == 'declined':
            records.append(appointment.decline_reason or '')
    reasons = {}
    for reason in records:
        reason = reason.strip()
        reasons[reason] = reasons.get(reason, 0) + 1
    rows = [
        {'reason': reason or '（空）', 'count': count}
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    ]
    filled_count = sum(1 for reason in records if reason.strip())
    return {
        'declined_count': len(records),
        'filled_count': filled_count,
        'fill_rate': _percent(filled_count, len(records)),
        'distinct_count': len(rows),
        'is_constant_value': len(rows) <= 1,
        'reason_rows': rows,
    }


def _summary(response_stats, attendance_stats, crosscheck):
    parts = []
    if response_stats['total']:
        parts.append(
            f"周期内 {response_stats['total']} 次交付安排，"
            f"完整事件链 {response_stats['exact_total']} 次；"
            f"得到首次回应 {response_stats['responded_count']} 次"
            f"（{response_stats['response_rate']}%）"
        )
        if response_stats['exact_responded_count']:
            parts.append(f"，完整链首次回应中位 {response_stats['median_label']}")
        if response_stats['legacy_total']:
            parts.append(
                f"；旧快照 {response_stats['legacy_total']} 次，"
                f"其中 {response_stats['legacy_responded_count']} 次可读快照回应时间，单独计算"
            )
    else:
        parts.append('当前周期还没有交付时间安排。')

    if attendance_stats['confirmed_ended_count']:
        parts.append(
            f"。已过约定结束时间的确认预约 {attendance_stats['confirmed_ended_count']} 次，"
            f"双方都到场 {attendance_stats['both_arrived_count']} 次"
            f"（{attendance_stats['both_arrival_rate']}%）"
        )
        if attendance_stats['one_sided_count']:
            parts.append(f"，只有一方到场 {attendance_stats['one_sided_count']} 次")
        if attendance_stats['neither_arrived_count']:
            parts.append(f"，双方都没到场 {attendance_stats['neither_arrived_count']} 次")
        if attendance_stats['is_small_sample']:
            parts.append(
                f"；不足 {attendance_stats['min_measured_appointments']} 次确认预约，"
                f"占比只作参考"
            )
    else:
        parts.append('。当前没有已过约定结束时间的确认预约，到场统计暂无可核对的样本。')

    if crosscheck['total']:
        parts.append(
            f"。周期内 {crosscheck['total']} 条「对方未到场」异常里，"
            f"{crosscheck['contradicted_count']} 条的被指控方其实有到场登记"
        )
        if crosscheck['other_reason_count']:
            parts.append(f"（另有 {crosscheck['other_reason_count']} 条其他类型异常不参与到场对照）")
    return ''.join(parts)


def build_meeting_attendance(days=30, now=None):
    """统计首次回应、轮次结果、到场和异常记录，只读不改业务数据。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    appointments = _fetch_appointments(created_after=start, created_before=now)
    incidents = _fetch_incidents(created_after=start, created_before=now)
    events_by_order = load_meeting_events_by_order(a.order_id for a in appointments)

    response_stats = _build_response_stats(appointments, events_by_order)
    attendance_stats = _build_attendance_stats(appointments, now)
    crosscheck = _build_incident_crosscheck(incidents, now)
    decline_stats = _build_decline_stats(appointments, events_by_order)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(appointments),
        'summary': _summary(response_stats, attendance_stats, crosscheck),
        'response_stats': response_stats,
        'attendance_stats': attendance_stats,
        'incident_crosscheck': crosscheck,
        'decline_stats': decline_stats,
        'status_labels': STATUS_LABELS,
        'incident_status_labels': INCIDENT_STATUS_LABELS,
        'incident_reason_labels': INCIDENT_REASON_LABELS,
    }