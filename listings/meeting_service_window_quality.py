"""交付开放时段风险审计。

这个模块只回答一个问题：当前仍待发生或待回应的交付安排，放在地点登记的
每周开放规则与单日例外里重新判断时，落在哪里。

它刻意不做任何修复动作。预约本身允许双方协商到登记时段之外；这里的“风险”
只表示“登记信息和交易安排需要人工核对”，不是违规判定，也不代表门禁实时
状态。
"""

from collections import defaultdict

from django.db.models import Prefetch
from django.utils import timezone

from .campus_location_service_hours import (
    STATE_OPEN,
    STATE_UNREGISTERED,
    evaluate_location_service_window,
)
from .models import (
    CampusLocationServiceException,
    CampusLocationServiceWindow,
    MeetingAppointment,
)

RISK_ROW_LIMIT = 20

STATUS_LABELS = {
    'pending': '待对方确认',
    'confirmed': '已确认',
}

CLASS_LABELS = {
    'open': '开放时段内',
    'weekly_outside': '每周时段外',
    'closed_exception': '临时关闭',
    'replacement_outside': '替换时段外',
    'unregistered': '未登记时段',
    'no_location': '无交付地点',
}


def _active_window_queryset():
    return CampusLocationServiceWindow.objects.filter(is_active=True)


def _active_exception_queryset():
    return CampusLocationServiceException.objects.filter(
        is_active=True,
    ).prefetch_related('exception_windows')


def _classify(evaluation):
    if evaluation.state == STATE_UNREGISTERED:
        return 'unregistered'
    if evaluation.state == STATE_OPEN:
        return 'open'

    exceptions = evaluation.exceptions
    if any(
        item.exception_type == CampusLocationServiceException.CLOSED
        for item in exceptions
    ):
        return 'closed_exception'
    if any(
        item.exception_type == CampusLocationServiceException.REPLACEMENT
        for item in exceptions
    ):
        return 'replacement_outside'
    return 'weekly_outside'


def _reason(kind, evaluation):
    if kind == 'no_location':
        return '交付安排没有地点，无法核对开放信息。'
    if kind == 'unregistered':
        return '地点未登记每周开放时段，请自行确认现场安排。'
    if kind == 'closed_exception':
        return '当天临时关闭：' + '；'.join(evaluation.exception_reasons)
    if kind == 'replacement_outside':
        return '安排不在当天替换时段内：' + '；'.join(evaluation.exception_reasons)
    if kind == 'weekly_outside':
        return '安排不在登记每周开放时段内，请核对临时开放或长期规则。'
    return '安排完整落在登记开放时段内。'


def _future_appointments(now):
    return MeetingAppointment.objects.filter(
        status__in=('pending', 'confirmed'),
        start_at__gt=now,
    ).select_related(
        'location', 'order',
    ).prefetch_related(
        Prefetch('location__service_windows', queryset=_active_window_queryset()),
        Prefetch('location__service_exceptions', queryset=_active_exception_queryset()),
    ).order_by('start_at', 'pk')


def _build_exception_config(now):
    """Count active future exceptions with and without a same-day appointment."""

    today = timezone.localtime(now).date()
    exceptions = CampusLocationServiceException.objects.filter(
        is_active=True,
        effective_date__gte=today,
    ).select_related('location').order_by('effective_date', 'location__name')

    appointment_dates = defaultdict(set)
    appointments = MeetingAppointment.objects.filter(
        status__in=('pending', 'confirmed'),
        start_at__gt=now,
        location__isnull=False,
    ).values_list('location_id', 'start_at')
    for location_id, start_at in appointments:
        appointment_dates[location_id].add(timezone.localtime(start_at).date())

    matched = 0
    unused_location_ids = set()
    for exception in exceptions:
        if exception.effective_date in appointment_dates.get(exception.location_id, set()):
            matched += 1
        else:
            unused_location_ids.add(exception.location_id)

    total = len(exceptions)
    return {
        'future_count': total,
        'matched_count': matched,
        'unused_count': total - matched,
        'unused_location_count': len(unused_location_ids),
    }


def build_meeting_service_window_quality(days=30, *, now=None):
    """Build the forward-looking service-window audit.

    ``days`` is accepted because the operations dashboard shares one builder
    registry. This panel intentionally ignores it: the question is about all
    arrangements still ahead, not about re-slicing the past.
    """

    del days
    moment = timezone.now() if now is None else now
    appointments = list(_future_appointments(moment))

    counts = {kind: 0 for kind in CLASS_LABELS}
    location_stats = defaultdict(lambda: {
        'location_id': None,
        'location_name': '',
        'total_count': 0,
        'open_count': 0,
        'weekly_outside_count': 0,
        'closed_exception_count': 0,
        'replacement_outside_count': 0,
        'unregistered_count': 0,
    })
    risk_rows = []

    for appointment in appointments:
        location = appointment.location
        if location is None:
            kind = 'no_location'
            evaluation = None
        else:
            evaluation = evaluate_location_service_window(
                location, appointment.start_at, appointment.end_at,
            )
            kind = _classify(evaluation)

        counts[kind] += 1
        if location is not None:
            stat = location_stats[location.pk]
            stat.update({
                'location_id': location.pk,
                'location_name': location.name,
            })
            stat['total_count'] += 1
            if kind != 'no_location':
                stat[f'{kind}_count'] += 1

        if kind != 'open':
            risk_rows.append({
                'order_id': appointment.order_id,
                'location_name': location.name if location is not None else '未填写',
                'start_at': appointment.start_at,
                'end_at': appointment.end_at,
                'status': appointment.status,
                'status_label': STATUS_LABELS.get(appointment.status, appointment.status),
                'kind': kind,
                'kind_label': CLASS_LABELS[kind],
                'reason': _reason(kind, evaluation),
                'rule_summary': evaluation.summary if evaluation is not None else '—',
                'is_exception': kind in {'closed_exception', 'replacement_outside'},
            })

    location_rows = []
    for stat in location_stats.values():
        exception_count = (
            stat['closed_exception_count'] + stat['replacement_outside_count']
        )
        registered_outside_count = exception_count + stat['weekly_outside_count']
        attention_count = registered_outside_count + stat['unregistered_count']
        location_rows.append({
            **stat,
            'exception_count': exception_count,
            'registered_outside_count': registered_outside_count,
            'attention_count': attention_count,
        })
    location_rows.sort(key=lambda row: (
        -row['attention_count'], -row['total_count'], row['location_name'], row['location_id'],
    ))

    exception_config = _build_exception_config(moment)
    measurable_count = counts['open'] + counts['weekly_outside'] + (
        counts['closed_exception'] + counts['replacement_outside']
    ) + counts['unregistered']
    registered_outside_count = (
        counts['weekly_outside'] + counts['closed_exception']
        + counts['replacement_outside']
    )
    attention_count = registered_outside_count + counts['unregistered']

    if not appointments:
        summary = '当前没有待核对的未来交付安排。'
    elif attention_count:
        summary = (
            f'{attention_count} 条未来安排需要对照地点开放信息人工确认，'
            f'其中 {registered_outside_count} 条落在已登记规则外，'
            f'{counts["unregistered"]} 条使用未登记每周时段的地点。'
        )
    else:
        summary = '当前可评估的未来交付安排均落在登记开放时段内。'

    if exception_config['future_count']:
        summary += (
            f' 未来已配置 {exception_config["future_count"]} 条单日例外，'
            f'其中 {exception_config["unused_count"]} 条当前没有同日交付安排。'
        )

    return {
        'summary': summary,
        'has_data': bool(appointments or exception_config['future_count']),
        'period_note': '本面板按构建时刻向前看，页面顶部的统计周期参数不会改变它的分母。',
        'risk_row_limit': RISK_ROW_LIMIT,
        'risk_rows': risk_rows,
        'risk_page_rows': risk_rows[:RISK_ROW_LIMIT],
        'risk_total_count': len(risk_rows),
        'metrics': {
            'total_count': len(appointments),
            'measurable_count': measurable_count,
            'no_location_count': counts['no_location'],
            'unregistered_count': counts['unregistered'],
            'open_count': counts['open'],
            'weekly_outside_count': counts['weekly_outside'],
            'closed_exception_count': counts['closed_exception'],
            'replacement_outside_count': counts['replacement_outside'],
            'registered_outside_count': registered_outside_count,
            'attention_count': attention_count,
            **exception_config,
        },
        'location_rows': location_rows,
        'boundary_note': (
            '只读审计：登记时段不等同门禁实时状态，时段外安排也不是违规；'
            '系统不自动改约、不提醒买卖双方、不修改地点配置。'
        ),
    }
