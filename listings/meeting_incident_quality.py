# -*- coding: utf-8 -*-
"""
交付预约异常处理得公不公平，第一次被运营读到。

`MeetingIncident` 是站内唯一一条「约好了见面，其中一方在现场出了状况」的记录，也是站内
**唯一一条直指线下人身安全**的申诉。而它此前被读到的程度比 `Report` 还浅：
`governance_sla` 报了时效，`analytics` 报了新增计数，`meeting_attendance` 只对照了
`reason == 'no_show'` 一类里「提交人说的」和「系统里另一方的到场登记」——那个模块列的是
具体工单、用户名和商品名，是一张给运营逐条处理的清单。至于这条队列**整体的处理倾向**，
一处都没有被读过。`dispute_resolution_quality` 与 `report_resolution_quality` 各自为
争议和举报做了全套，`MeetingIncident` 这张表一处都没沾上。

`resolution_note` 这个字段的全部读者只有两处：`forms.py` 的表单字段和 `views.py:1727`
的写入路径。**一个字段被表单收集、被写入，从来没被统计过。**

本模块把它们读出来，只报告，不改写任何一条异常、任何一份处理意见。

几处口径上的取舍比数字本身更要紧：

**驳回率按异常类型拆，不按总数报。** 四种类型的举证难度完全不是一回事：「对方未到场」
有系统里的到场登记可以对照，是四种里唯一有客观证据的；「现场安全问题」几乎不可能有系统
证据，全凭提交人陈述。把它们平均成一个总驳回率，这个数一旦波动，运营第一反应是去查处理人，
而不是去查是哪一类异常在涨。

**已完结的异常才算进驳回率的分母。** 分母只收 `resolved` 与 `dismissed`。还挂在
`open` / `reviewing` 的是右删失样本：异常还没判，谈不上被确认还是被驳回。

**处理意见只报「写了没写」和长度，一个字符内容都不读。** `resolution_note` 是处理人写给
双方的一段自由文本，可能写着电话、微信、楼栋号。这个模块只关心一件事：这段结论有没有被
写下来。按结局拆填写率——被驳回的异常如果普遍不写意见，提交人收到的是一个没有理由的驳回，
而这一条比举报更重：异常的另一半是**指控对方**，一条没有理由的驳回，会让提交人觉得自己被
反过来罚了一次。

**提交方向只报「谁在报谁」，不判「哪一方更常被驳」。** `reported_by` 与 `accused` 在每条
异常里都是一对，这对人的身份可以由 `Order` 的 `buyer` / `seller` 推出来。报两侧各自的
总量与驳回率，但**不把它读成一个结论**：买家报卖家和卖家报买家，各自的总量、各自面对的
异常类型构成都可能完全不同。两侧都过样本门槛时只报数字与差值，且必须同时把两侧的总量一起
报出来——一个 100% 对 100% 的差值，如果两侧各只有 1 条，是没有信息量的。

**不排名、不报任何人的用户名。** 把「谁被报得最多」做成一张排行榜，平台就从仲裁者变成了
一个记录谁难相处的小本子。方向分布报的是**流程本身是否对双方可用**，不是人的性质。

**不重复 `meeting_attendance` 的到场对照。** 那个模块对照的是具体工单，本模块只读聚合
数字，不读任何一条工单、不列用户名、不列商品名。

**不把驳回率和处理人挂钩。** 按 `reviewer` 拆驳回率是一份考官成绩单，本模块不报。

**不读 `detail` 和 `resolution_note` 的任何内容。** 两处都是自由文本，可能写着电话、
微信、楼栋号。

**不把「异常多」读成「用户爱找茬」。** 异常是站内唯一一条直指线下安全的申诉，用得多可能
说明用户信任这条通道。本模块只报形状，不做人物评价。注意 `MeetingIncident` 上是
`OneToOneField(appointment)`，所以「同一场约会里双方各报一次异常」这个数结构上恒为 0，
报出来是一个永远为 0 的常数，没有信息量，本模块不报。
"""

from collections import namedtuple
from datetime import timedelta

from django.utils import timezone

from .models import MeetingIncident


# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
)

# 给出方向前至少要有这么多条已完结的异常。异常比举报还低频，门槛和举报、争议
# 处理质量同一个量级，不能再低——低于这个数，一条异常就是一个百分点。
MIN_CLOSED_INCIDENTS = 8

# 一个异常类型的已完结样本至少要这么多条，才允许对它下「这一类更容易被驳回」的
# 判断。和通知类型版图、举报处理质量同一个数：全站统一的「12 条」。
MIN_BUCKET_SAMPLE = 12

# 两组占比相差不满这么多个百分点，视为噪声。和举报、争议处理质量同一个数。
NOISE_POINTS = 8

# 处理意见长度分档。和举报处理质量保持同一把尺子：两处的处理意见是同一种东西，
# 分档不同会让运营在两块面板之间换算。
NOTE_LENGTH_BUCKETS = (
    (5, '5 字以内'),
    (15, '6-15 字'),
    (40, '16-40 字'),
    (None, '40 字以上'),
)

# 已完结状态。只有判完了才有「驳回」可言。
CLOSED_STATUSES = ('resolved', 'dismissed')

# 还在流程里的状态：右删失样本，单独数出规模，不混入任何比率的分母。
OPEN_STATUSES = ('open', 'reviewing')

# 提交方向。reported_by 与 accused 在每条异常里是一对，方向按 Order 的
# buyer / seller 推出来——取不到订单一侧身份时不猜，单独归到「未知」。
DIRECTION_BANDS = (
    ('buyer_reports_seller', '买家报卖家'),
    ('seller_reports_buyer', '卖家报买家'),
    ('unknown', '取不到订单身份'),
)

# 提交人的发单数分档：只发过一条的是应急，反复在用的说明这条通道对他是常规手段。
REPORTER_COUNT_BANDS = (
    (1, 1, '只报过 1 次'),
    (2, 3, '2-3 次'),
    (4, 9, '4-9 次'),
    (10, None, '10 次及以上'),
)

# 模块需要读的列。保持显式：90 天视图上异常可能不少，只取用得上的。
# 不读 detail、不读 resolution_note——两处都是自由文本，可能写着电话、微信、楼栋号。
PERIOD_FIELDS = (
    'id',
    'reason',
    'status',
    'resolution_note',
    'reviewed_at',
    'created_at',
    'reported_by_id',
    'accused_id',
    'appointment_id',
)


# 一条异常，连它所属安排的订单双方身份一起取回来。一次查完固定下来：驳回率、
# 意见填写率、方向分布、提交人分布各读一遍，按记录现算会让每条异常打好几遍库，
# 而看板是一次打开全算的。
IncidentRecord = namedtuple('IncidentRecord', [
    'incident', 'direction', 'buyer_id', 'seller_id',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _percent_or_none(part, whole):
    """拿不到的分母报 None 不报 0。

    0 会被读成「占比是零」，而这里的事实是「分母不存在」。看板模板把 None 显示成
    「—」，和 0 区分开。
    """
    return round(part / whole * 100, 1) if whole else None


def _mean(values):
    return sum(values) / len(values) if values else None


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    """
    from datetime import time as dt_time
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _band_index(count):
    for index, (low, high, _label) in enumerate(REPORTER_COUNT_BANDS):
        if high is None or count <= high:
            if count >= low:
                return index
    return len(REPORTER_COUNT_BANDS) - 1


def _note_length_index(length):
    for index, (limit, _label) in enumerate(NOTE_LENGTH_BUCKETS):
        if limit is None or length < limit:
            return index
    return len(NOTE_LENGTH_BUCKETS) - 1


def _is_closed(record):
    return record.incident['status'] in CLOSED_STATUSES


def _is_rejected(record):
    return record.incident['status'] == 'dismissed'


def _note_text(record):
    """处理意见去空白后的文本。

    只取长度和空否，一个字符内容都不读——模板和测试也只该看到长度。
    """
    return (record.incident['resolution_note'] or '').strip()


def _direction_of(reporter_id, buyer_id, seller_id):
    """提交人是买家还是卖家。

    取不到订单一侧身份时不猜：一条异常的提交人可能已经注销，而 Order 的 buyer /
    seller 是 SET_NULL。猜一个方向比报「未知」更糟——「未知」至少是诚实的。
    """
    if reporter_id and reporter_id == buyer_id:
        return 'buyer_reports_seller'
    if reporter_id and reporter_id == seller_id:
        return 'seller_reports_buyer'
    return 'unknown'


def _fetch_records(days, now):
    """周期内创建的异常，连它所属安排的订单双方身份一起取回来。

    订单身份用来回答「是谁在报谁」：`reported_by` 与 `accused` 是一对用户名，而
    这对人是买家还是卖家，要回到 `Order` 上才读得出来。

    按创建时间与主键显式排序：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    """
    start = _period_start(days, timezone.localtime(now).date())
    rows = list(
        MeetingIncident.objects.filter(created_at__gte=start, created_at__lte=now)
        .values(*PERIOD_FIELDS)
        .order_by('created_at', 'id')
    )
    # 订单身份一次查完。appointment 是 OneToOne，删约会会连坐删掉异常，所以正常路径下
    # appointment_id 一定指向一件还在的安排；订单同样如此。取不到就不取。
    from .models import MeetingAppointment, Order
    appointment_ids = {row['appointment_id'] for row in rows if row['appointment_id']}
    appointment_order = {}
    if appointment_ids:
        for appointment in MeetingAppointment.objects.filter(
            id__in=appointment_ids,
        ).values('id', 'order_id'):
            appointment_order[appointment['id']] = appointment['order_id']
    order_ids = {oid for oid in appointment_order.values() if oid}
    order_map = {}
    if order_ids:
        for order in Order.objects.filter(id__in=order_ids).values(
            'id', 'buyer_id', 'seller_id',
        ):
            order_map[order['id']] = order
    records = []
    for row in rows:
        order = order_map.get(appointment_order.get(row['appointment_id']))
        buyer_id = order['buyer_id'] if order else None
        seller_id = order['seller_id'] if order else None
        records.append(IncidentRecord(
            incident=row,
            direction=_direction_of(row['reported_by_id'], buyer_id, seller_id),
            buyer_id=buyer_id,
            seller_id=seller_id,
        ))
    return records


def _build_reason_rows(records):
    """驳回率按异常类型拆。

    四种类型的举证难度完全不是一回事：「对方未到场」有系统里的到场登记可以对照，是四种里
    唯一有客观证据的；「现场安全问题」几乎不可能有系统证据，全凭提交人陈述。把它们平均成
    一个总驳回率，这个数一旦波动，运营第一反应是去查处理人，而不是去查是哪一类异常在涨。

    行取自 `REASON_CHOICES` 与库里的并集——库里出现了选择项之外的类型（脏数据、或者
    以后加了选择项而老数据还在）也要露出来。只报一边，就等于假设另一边是齐的。
    """
    from .models import MeetingIncident as IncidentModel
    reason_labels = dict(IncidentModel.REASON_CHOICES)
    grouped = {}
    for record in records:
        grouped.setdefault(record.incident['reason'], []).append(record)

    result = []
    for reason in sorted(set(reason_labels) | set(grouped)):
        reason_records = grouped.get(reason, [])
        closed = [record for record in reason_records if _is_closed(record)]
        rejected = [record for record in closed if _is_rejected(record)]
        result.append({
            'reason': reason,
            'label': reason_labels.get(reason, reason),
            'total_count': len(reason_records),
            'closed_count': len(closed),
            'rejected_count': len(rejected),
            'resolved_count': len(closed) - len(rejected),
            'rejection_rate': _percent_or_none(len(rejected), len(closed)),
            'open_count': len(reason_records) - len(closed),
            # 空档也要报出来：周期内一条都没有的异常类型，报出来是「这一类没人用」，
            # 而漏掉它会让读者以为这一类也是齐的。
            'is_empty': not reason_records,
            'is_small_sample': 0 < len(closed) < MIN_BUCKET_SAMPLE,
        })
    # 已完结样本多的排在前面：最该被读的那一类在最上面。
    result.sort(key=lambda row: (-row['closed_count'], row['reason']))
    return result


def _build_outcome_stats(records):
    """总结局：确认了多少、驳回多少、还在流程里多少。"""
    closed = [record for record in records if _is_closed(record)]
    rejected = [record for record in closed if _is_rejected(record)]
    resolved = [record for record in closed if not _is_rejected(record)]
    open_records = [record for record in records if not _is_closed(record)]
    return {
        'total_count': len(records),
        'closed_count': len(closed),
        'rejected_count': len(rejected),
        'resolved_count': len(resolved),
        'open_count': len(open_records),
        # 空分母报 None：一条已完结异常都没有的时候，驳回率不存在，不是零。
        'rejection_rate': _percent_or_none(len(rejected), len(closed)),
        'has_closed': bool(closed),
    }


def _build_note_stats(records):
    """处理意见只报「写了没写」和长度，一个字符内容都不读。

    按结局拆填写率。被驳回的异常如果普遍不写意见，提交人收到的是一个没有理由的驳回——
    而这一条比举报更重：异常的另一半是指控对方，一条没有理由的驳回，会让提交人觉得自己
    被反过来罚了一次。单独数出来，不混进填写率。

    **不报「长度和驳回率的关系」。** 长意见是不是更难被申诉，观测数据里分不开：
    意见写得长可能只是这类异常本身复杂。只报形状，不给方向。
    """
    closed = [record for record in records if _is_closed(record)]
    with_note = [record for record in closed if _note_text(record)]
    without_note = [record for record in closed if not _note_text(record)]

    lengths = [len(_note_text(record)) for record in with_note]
    buckets = [{'label': label, 'count': 0} for _limit, label in NOTE_LENGTH_BUCKETS]
    for length in lengths:
        buckets[_note_length_index(length)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent_or_none(bucket['count'], len(lengths))

    by_outcome = []
    for key, label, statuses in (
        ('resolved', '已确认', ('resolved',)),
        ('dismissed', '已驳回', ('dismissed',)),
    ):
        group = [
            record for record in closed
            if record.incident['status'] in statuses
        ]
        filled = [record for record in group if _note_text(record)]
        by_outcome.append({
            'key': key,
            'label': label,
            'closed_count': len(group),
            'with_note_count': len(filled),
            'fill_rate': _percent_or_none(len(filled), len(group)),
            'average_length': round(_mean(
                [len(_note_text(record)) for record in filled]
            ), 1) if filled else None,
        })

    return {
        'closed_count': len(closed),
        'with_note_count': len(with_note),
        'without_note_count': len(without_note),
        'fill_rate': _percent_or_none(len(with_note), len(closed)),
        'bucket_rows': buckets,
        'average_length': round(_mean(lengths), 1) if lengths else None,
        'by_outcome': by_outcome,
        # 两组填写率之差。两侧都过门槛才允许把它读成一个信号，否则只报数。
        'outcome_gap_points': _outcome_gap(by_outcome),
        'outcome_comparable': all(
            row['closed_count'] >= MIN_BUCKET_SAMPLE for row in by_outcome
        ),
        'is_empty': not closed,
    }


def _outcome_gap(by_outcome):
    """已确认与已驳回两组的处理意见填写率之差（百分点）。

    拿不到任一侧的分母时报 None：一组一条已完结异常都没有的时候，这个差不存在。
    """
    rates = [row['fill_rate'] for row in by_outcome]
    if any(rate is None for rate in rates):
        return None
    return round(rates[1] - rates[0], 1)


def _build_direction_rows(records):
    """是谁在报谁。

    `reported_by` 与 `accused` 在每条异常里都是一对，方向按 `Order` 的 buyer /
    seller 推出来。报两侧各自的总量与驳回率，但**不把它读成一个结论**：买家报卖家和
    卖家报买家，各自的总量、各自面对的异常类型构成都可能完全不同。

    两侧都过样本门槛时才允许把这个差值读成一个信号，而且必须同时把两侧的总量一起报——
    一个 100% 对 100% 的差值，如果两侧各只有 1 条，是没有信息量的。

    取不到订单一侧身份时不猜，单独归到「未知」。猜一个方向比报「未知」更糟。
    """
    rows = []
    for key, label in DIRECTION_BANDS:
        group = [record for record in records if record.direction == key]
        closed = [record for record in closed_group(group)]
        rejected = [record for record in closed if _is_rejected(record)]
        rows.append({
            'key': key,
            'label': label,
            'total_count': len(group),
            'closed_count': len(closed),
            'rejected_count': len(rejected),
            'rejection_rate': _percent_or_none(len(rejected), len(closed)),
            'is_empty': not group,
            'is_small_sample': 0 < len(closed) < MIN_BUCKET_SAMPLE,
        })
    measurable = [row for row in rows if not row['is_small_sample'] and row['rejection_rate'] is not None]
    gap = None
    if len(measurable) >= 2:
        rates = sorted(row['rejection_rate'] for row in measurable)
        gap = round(rates[-1] - rates[0], 1)
    return {
        'rows': rows,
        # 两侧都过门槛才给方向。
        'gap_points': gap,
        'comparable': len(measurable) >= 2,
        'is_empty': not any(row['total_count'] for row in rows),
    }


def closed_group(records):
    return [record for record in records if _is_closed(record)]


def _build_reporter_rows(records):
    """报异常的人是不是同一批人。

    **不排名、不报任何人的用户名。** 把「谁最爱报异常」做成一张排行榜，平台就从仲裁者
    变成了一个记录谁难相处的小本子。报的是分布形状：绝大多数人只报过一次，说明这条通道
    是**应急**性质的；有一批人反复在用，说明它是某些用户的**常规**手段。

    受 `OneToOneField(appointment)` 限制，**不报「同一场约会里双方各报一次」**——
    那个数结构上恒为 0，报出来是一个永远为 0 的常数，没有信息量。
    """
    counts = {}
    for record in records:
        reporter_id = record.incident['reported_by_id']
        if not reporter_id:
            continue
        counts[reporter_id] = counts.get(reporter_id, 0) + 1

    buckets = [{'label': label, 'count': 0} for _low, _high, label in REPORTER_COUNT_BANDS]
    for count in counts.values():
        buckets[_band_index(count)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent_or_none(bucket['count'], len(counts))

    # 只报过一次的人占比。这是「应急性质」最直接的一个数。
    once_only = sum(1 for count in counts.values() if count == 1)
    return {
        'reporter_count': len(counts),
        'report_count': len(records),
        'once_only_count': once_only,
        'once_only_rate': _percent_or_none(once_only, len(counts)),
        'max_count': max(counts.values()) if counts else 0,
        'avg_count': round(_mean(list(counts.values())), 2) if counts else None,
        'band_rows': buckets,
        'is_empty': not counts,
    }


def _summary(outcome, reason_rows, note, direction, reporters):
    """一段人话，说明这条治理队列此刻公不公平。"""
    if not outcome['total_count']:
        return (
            '当前周期还没有交付预约异常。异常是站内唯一一条直指线下安全的申诉，'
            '它此刻没有发生——这些数都不存在，不是零。'
        )
    parts = [
        f"周期内 {outcome['total_count']} 条异常，已完结 {outcome['closed_count']} 条"
        f"（确认 {outcome['resolved_count']} 条、驳回 {outcome['rejected_count']} 条），"
        f"还有 {outcome['open_count']} 条在流程里。"
    ]
    if outcome['has_closed']:
        parts.append(f"整体驳回率 {outcome['rejection_rate']}%。")
    measurable = [
        row for row in reason_rows
        if not row['is_small_sample'] and row['rejection_rate'] is not None
    ]
    if measurable:
        top = max(measurable, key=lambda row: row['rejection_rate'])
        parts.append(f"样本够的异常类型里，「{top['label']}」驳回率最高（{top['rejection_rate']}%）。")
    if not note['is_empty']:
        rejected_fill = note['by_outcome'][1]['fill_rate']
        resolved_fill = note['by_outcome'][0]['fill_rate']
        parts.append(
            f"处理意见填写率 {note['fill_rate']}%"
            f"（已驳回的 {rejected_fill if rejected_fill is not None else '—'}%"
            f" vs 已确认的 {resolved_fill if resolved_fill is not None else '—'}%）。"
        )
    if not direction['is_empty']:
        named = [row for row in direction['rows'] if not row['is_empty'] and row['key'] != 'unknown']
        if named:
            shapes = '、'.join(f"{row['label']} {row['total_count']} 条" for row in named)
            parts.append(f"提交方向：{shapes}。")
    if not reporters['is_empty']:
        parts.append(
            f"{reporters['reporter_count']} 位提交人里 {reporters['once_only_rate']}% 只报过一次。"
        )
    return ''.join(parts)


def build_meeting_incident_quality(days=30, now=None):
    """统计交付预约异常的处理质量：哪一类更容易被驳回、理由写没写、谁在报谁、谁在报。只报告。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    records = _fetch_records(days, now)
    outcome = _build_outcome_stats(records)
    reason_rows = _build_reason_rows(records)
    note = _build_note_stats(records)
    direction = _build_direction_rows(records)
    reporters = _build_reporter_rows(records)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(records),
        'summary': _summary(outcome, reason_rows, note, direction, reporters),
        'outcome': outcome,
        'reason_rows': reason_rows,
        'note': note,
        'direction': direction,
        'reporters': reporters,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'min_closed_incidents': MIN_CLOSED_INCIDENTS,
        'noise_points': NOISE_POINTS,
    }

