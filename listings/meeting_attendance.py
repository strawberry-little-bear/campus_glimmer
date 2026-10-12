# -*- coding: utf-8 -*-
"""约好的那一次见面，第一次被运营读到。

交付预约从上线那天起就在记两段时间：对方多久回应一个提议，以及双方各自什么时候
登记到场。前一段写在 ``responded_at`` 里，后一段写在 ``buyer_arrived_at`` 与
``seller_arrived_at`` 里。四个字段全都有写入路径，而读取路径只有一处——订单详情页
把它们显示给用户本人看。运营侧一条统计都没有：没有人知道提议交上去之后对方多久
才理，也没有人知道约了时间的订单里到底有几个人真的到了现场。

于是"约好了"和"见着了"在数据里被当成同一件事，而它们显然不是。一个把全部预约都
算成履约的平台，会一直以为自己的交付环节很健康，直到有人开始提交异常。

这个模块把这两段时间读出来，只报告，不改写。它回答四个问题：对方多久回应、有
没有人到、迟到多久，以及一条"对方未到场"的异常到底有没有到场记录作支撑。第四问
是这个模块最该被写下来的理由：提交人说的是主观陈述，而签到记录是系统里另一方亲
手留下的动作，两者对不上的那一条，恰恰是最该被运营看见的。

几个口径上的取舍比数字本身更要紧。到场统计的分母是"已经过了约定结束时间"的预约：
还没到结束时间的约会谈不到"有没有人到"，把它算进分母只会让到场率凭空变小，这是
右删失样本，和订单阶段流模块同一个道理。迟到按签到时间减去约定开始时间算，提前
到不算迟到——一个提前二十分钟到的人不该被记成"准点"，那会让准点率虚高。未接受
原因单独一节报出来，因为这个字段唯一的写入路径是视图里的一句硬编码，用户从来
没有机会填它，所以它的取值分布只可能有一个值；如实报告这件事，比给它编一个
分布图要诚实。
"""

from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import MeetingAppointment, MeetingIncident

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 响应时长分档。按天切而不是按小时：一次交付安排的时间尺度是天，报"回应用了 37
# 小时"对一个按周排期的用户没有意义。
RESPONSE_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (None, '3 天以上'),
)

# 迟到分档。第一档是提前到，它和准点必须分开：把提前到的人记成准点，会让"大家都
# 很守时"这句话变成一句被统计口径制造出来的好话。
LATENESS_BUCKETS = (
    (None, '提前到'),
    (900, '准点'),
    (3600, '迟到 15 分钟内'),
    (86400, '迟到 1 小时内'),
    (None, '迟到 1 小时以上'),
)

# 一组预约至少要这么多条，分档占比才给方向。和其余模块同一个量级：几十条样本上
# 几个百分点的差距是噪声。
MIN_MEASURED_APPOINTMENTS = 12

# 两个方向相差小于这个百分点视为噪声。
NOISE_POINTS = 8

# 矛盾异常清单最多列这么多条。清单是给"现在去核实"用的，不是给归档用的。
CONTRADICTION_LIMIT = 20

STATUS_LABELS = dict(MeetingAppointment.STATUS_CHOICES)

# 已经谈成的一次安排。只有它才谈得上"到没到"：待确认的还没约定成，未接受和已取消
# 的则根本不会发生见面。
CONFIRMED_STATUS = 'confirmed'

# 预约异常里和到场登记直接相关的那一类。其余类型（安全问题、其他）与到没到无关，
# 不参与对照，硬把它们拉进来只会得到一堆没有意义的"无依据"。
ATTENDANCE_RELATED_REASON = 'no_show'

INCIDENT_STATUS_LABELS = dict(MeetingIncident.STATUS_CHOICES)
INCIDENT_REASON_LABELS = dict(MeetingIncident.REASON_CHOICES)


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _fetch_appointments(*, created_after=None, created_before=None,
                        start_after=None, start_before=None):
    """按显式排序取出预约，查询条件由调用方给。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何"第一名"都会随机漂移。
    """
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


def _response_seconds(appointment):
    """从提议交上去到对方回话的秒数，没回应返回 None。

    只认 ``responded_at``：``updated_at`` 也会在重新提议时被写，拿它算会让"多久
    回应"和"多久改主意"混在一起。
    """
    if not appointment.responded_at or not appointment.created_at:
        return None
    return max((appointment.responded_at - appointment.created_at).total_seconds(), 0.0)


def _is_responded(appointment):
    return appointment.responded_at is not None


def _is_confirmed(appointment):
    return appointment.status == CONFIRMED_STATUS


def _has_ended(appointment, now):
    """约定结束时间是否已经过去。

    到场统计的分母只收这一类。还没到结束时间的约会谈不到"有没有人到"，把它算进
    分母会让到场率凭空变小——这是右删失样本，和订单阶段流模块同一个道理。
    """
    return appointment.end_at is not None and appointment.end_at <= now


def _arrival_offset_seconds(appointment, field_name):
    """某个角色的签到时间减去约定开始时间，负数表示提前到，没签到返回 None。"""
    arrived_at = getattr(appointment, field_name)
    if not arrived_at or not appointment.start_at:
        return None
    return (arrived_at - appointment.start_at).total_seconds()


def _lateness_bucket_index(offset_seconds):
    """迟到分档。提前到单独一档，它和准点不是一回事。"""
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
    """把签到相对约定开始时间的偏移讲成人话，负数读成"提前"。"""
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
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _build_response_stats(appointments):
    """对方多久回应一个提议。"""
    responded = [a for a in appointments if _is_responded(a)]
    pending = [a for a in appointments if not _is_responded(a)]
    seconds_list = [s for s in (_response_seconds(a) for a in responded) if s is not None]

    buckets = [{'label': label, 'count': 0} for _limit, label in RESPONSE_BUCKETS]
    for seconds in seconds_list:
        buckets[_bucket_index(seconds)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(seconds_list))

    return {
        'total': len(appointments),
        'responded_count': len(responded),
        'pending_count': len(pending),
        'response_rate': _percent(len(responded), len(appointments)),
        'average_seconds': _mean(seconds_list),
        'average_label': _format_duration(_mean(seconds_list)),
        'median_label': _format_duration(_percentile(seconds_list, 0.5)),
        'p90_label': _format_duration(_percentile(seconds_list, 0.9)),
        'fastest_label': _format_duration(min(seconds_list)) if seconds_list else '—',
        'slowest_label': _format_duration(max(seconds_list)) if seconds_list else '—',
        'bucket_rows': buckets,
        'accepted_count': sum(1 for a in responded if _is_confirmed(a)),
        'declined_count': sum(1 for a in responded if a.status == 'declined'),
        'accept_rate': _percent(
            sum(1 for a in responded if _is_confirmed(a)), len(responded),
        ),
    }


def _merge_lateness_buckets(lateness_rows):
    """把两个角色的迟到分档合成一张表，供模板按行渲染。

    分档本来就该按行对齐：面板上"提前到"这一行要同时看得见买家和卖家两个人各有多少，
    分两张表看的话，运营得自己在脑子里把两行的数字对上，而人脑对行的能力并不好。
    """
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
    """已经过了约定结束时间的预约，双方各自到了没有、迟到了多久。

    分母只收"约定结束时间已经过去"的预约。一个还没到结束时间的约会谈不到有没有
    人到，把它算进分母会让到场率凭空变小；这些样本单独数出来，报成"还没到核对
    时间"，而不是被悄悄并进到场率。
    """
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
    """被指控的那一方在系统里留下的到场登记，没有就返回 None。"""
    if not appointment or not accused:
        return None
    order = appointment.order
    if accused == order.buyer:
        return appointment.buyer_arrived_at
    if accused == order.seller:
        return appointment.seller_arrived_at
    return None


def _build_incident_crosscheck(incidents, now):
    """提交人说的"对方未到场"，和系统里另一方的到场登记对得上吗。

    这是这个模块最该被写下来的部分。提交人说的是一句主观陈述，而签到记录是系统里
    另一方亲手留下的动作。两者对不上的那一条，不是要判定谁在说谎——平台没有能力也
    不该去判定——而是要被运营看见：说没到的人自己也没到，或者说没到的那一方其实在
    约定开始前三十分钟就登记了。这类记录不改变任何状态，只是从"一个待处理工单"
    变成"一个待核实的矛盾"。

    只对照"对方未到场"这一类异常。安全问题和"其他"与到没到无关，硬把它们拉进来
    只会得到一堆没有意义的"无依据"。
    """
    relevant = [
        incident for incident in incidents
        if incident.reason == ATTENDANCE_RELATED_REASON
    ]
    rows = []
    for incident in relevant:
        appointment = incident.appointment
        accused = incident.accused
        accused_arrived_at = _accused_arrived_at(appointment, accused)
        reporter_arrived_at = _accused_arrived_at(
            appointment, incident.reported_by,
        )
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

    # 显式排序：榜单的先后不能取决于数据库这一次返回行的顺序。先排没处理的，
    # 再排提交时间，同一天提交的按主键。
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


def _build_decline_stats(appointments):
    """未接受原因：如实报告这个字段只有一个取值。

    ``decline_reason`` 的字段名是"未接受原因"，而全库唯一的写入路径是
    ``views.py`` 里的一句硬编码"对方暂未接受这次时间安排"。用户从来没有机会填它，
    表单里也没有这个字段。所以它的取值分布只可能有一个值——这不是一个分布，这是
    一个常量。

    把它单独报出来，而不是顺手画一张分布图：一个只有一个取值的字段，画出来的分布
    图是 100% 的一根柱子，那张图除了让人以为这里有过选择之外不提供任何信息。运营
    该知道的是"这个字段目前没有收集到任何用户输入"，而不是"所有人都因为同一个原因
    拒绝"。
    """
    declined = [a for a in appointments if a.status == 'declined']
    reasons = {}
    for appointment in declined:
        reason = (appointment.decline_reason or '').strip()
        reasons[reason] = reasons.get(reason, 0) + 1
    rows = [
        {'reason': reason or '（空）', 'count': count}
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        'declined_count': len(declined),
        'filled_count': sum(1 for a in declined if (a.decline_reason or '').strip()),
        'fill_rate': _percent(
            sum(1 for a in declined if (a.decline_reason or '').strip()), len(declined),
        ),
        'distinct_count': len(rows),
        'is_constant_value': len(rows) <= 1,
        'reason_rows': rows,
    }


def _summary(response_stats, attendance_stats, crosscheck, decline_stats):
    """一段话把这个模块的读数讲完，样本不足的地方明说。"""
    parts = []
    if response_stats['total']:
        parts.append(
            f"周期内 {response_stats['total']} 次交付安排，"
            f"{response_stats['responded_count']} 次得到了回应"
            f"（{response_stats['response_rate']}%）"
        )
        if response_stats['responded_count']:
            parts.append(f"，回应中位耗时 {response_stats['median_label']}")
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
                f"；不足 {attendance_stats['min_measured_appointments']} 次"
                f"确认预约，占比只作参考"
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
    '''统计交付安排的响应时长、到场情况与异常和到场登记的交叉，只报告不改任何字段。'''
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    appointments = _fetch_appointments(created_after=start, created_before=now)
    incidents = _fetch_incidents(created_after=start, created_before=now)

    response_stats = _build_response_stats(appointments)
    attendance_stats = _build_attendance_stats(appointments, now)
    crosscheck = _build_incident_crosscheck(incidents, now)
    decline_stats = _build_decline_stats(appointments)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(appointments),
        'summary': _summary(response_stats, attendance_stats, crosscheck, decline_stats),
        'response_stats': response_stats,
        'attendance_stats': attendance_stats,
        'incident_crosscheck': crosscheck,
        'decline_stats': decline_stats,
        'status_labels': STATUS_LABELS,
        'incident_status_labels': INCIDENT_STATUS_LABELS,
        'incident_reason_labels': INCIDENT_REASON_LABELS,
    }
