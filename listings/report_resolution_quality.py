# -*- coding: utf-8 -*-
"""商品举报处理得公不公平，第一次被运营读到。

`Report` 是站内最早的治理队列，也是用户唯一一条**不经过对方同意就能递到平台面前**的通道。
`governance_sla` 只报了它多久被处理，`analytics` 报了新增与待处理的计数——三个数加起来，
运营知道这条队列忙不忙，不知道它公不公平：

- 哪一类举报更容易被驳回；
- 被驳回的用户收到了有理由的拒绝吗；
- 被驳回的举报集中在少数商品上，还是摊成一片；
- 这条通道是应急用的，还是一批人的常规手段。

`dispute_resolution_quality` 为**争议**做了全套（按 reason 拆驳回率、证据提交间隔、
处理意见填写率与长度、发起方向分布），而 `Report` 这张比争议更早、更日常、离用户更近的表，
一处都没有被统计过。`review_note` 这个字段的全部读者只有两处：`admin.py` 的 `search_fields`
（后台搜索框）和 `views.py:1608` 的写入路径。**一个字段被写、被搜索，从来没被统计过。**

本模块把它们读出来，只报告，不改写任何一条举报、任何一份审核意见。

几处口径上的取舍比数字本身更要紧：

**驳回率按 reason 拆，不按总数报。** 四种举报原因的举证难度完全不是一回事。把它们平均成
一个总驳回率，这个数一旦波动，运营第一反应是去查处理人，而不是去查是哪一类举报在涨。
按 reason 拆开之后，「诈骗」类驳回率上升和「广告」类驳回率上升是两条完全不同的线索。

**已完结的举报才算进驳回率的分母。** 分母只收 `resolved` 与 `rejected`。还挂在
`pending` / `reviewing` 的是右删失样本：举报还没判，谈不上被驳回还是被支持。

**审核意见只报「写了没写」和长度，一个字符内容都不读。** `review_note` 是审核人写给举报人
的一段自由文本，可能写着电话、微信、楼栋号，也可能写着审核人的判断和情绪。这个模块只关心
一件事：这段理由有没有被写下来。理由写得好不好，那是复核人该看的，本模块不替代复核人。
按结局拆填写率——被驳回的举报如果普遍不写意见，用户收到的是一个没有理由的拒绝。

**不把驳回率和处理人挂钩。** 按 `reviewer` 拆驳回率是一份考官成绩单，本模块不报：审核标准
的松紧是运营该统一管的事，把它摊成个人数字只会让审核人挑软的处理。

**不列商品名、不列卖家名。** 一份「被驳回举报最多的商品榜」是一份公开处刑清单，不是看板。
运营要的是「集中在少数几件上」还是「摊在一大片上」这个形状——前者说明有几件商品有问题，
后者说明举报标准可能有问题。

**不排名举报人、不报任何人的用户名。** 把「谁最爱举报」做成一张排行榜，平台就从仲裁者变成
了一个记录谁难相处的小本子。报的是分布形状：绝大多数人只举报过一次，说明这条通道是**应急**
性质的；有一批人反复在用，说明它是某些用户的**常规**手段——两种形状对应完全不同的治理动作。
注意 `Report` 上有 `UniqueConstraint(fields=['item', 'reporter'])`，所以「同一用户对同一商品
重复举报」这个数结构上恒为 0，报出来是一个永远为 0 的常数，没有信息量，本模块不报。

**不读 `detail` 的任何内容。** 举报人的补充说明是自由文本，可能写着电话、微信、楼栋号。

**不把「举报多」读成「用户爱找茬」。** 举报是用户唯一一条不经对方同意就能递到平台面前的
通道，用得多可能说明用户信任这条通道。本模块只报形状，不做人物评价。
"""

from collections import namedtuple
from datetime import timedelta

from django.utils import timezone

from .models import Report

# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
)

# 给出方向前至少要有这么多条已完结的举报。举报比争议更低频：一个校园平台一个月
# 可能只有寥寥几条。门槛和争议处理质量同一个量级，不能再低——低于这个数，一条举报
# 就是一个百分点，报出来是噪声。
MIN_CLOSED_REPORTS = 8

# 一个 reason 的已完结样本至少要这么多条，才允许对它下「这一类更容易被驳回」的判断。
# 和通知类型版图、通知聚合质量、搜索位置质量同一个数：全站统一的「12 条」。
MIN_BUCKET_SAMPLE = 12

# 两组占比相差不满这么多个百分点，视为噪声。和争议处理质量同一个数。
NOISE_POINTS = 8

# 审核意见长度分档。一段「已处理」和一段写清了核实依据的意见，在长度上是能分开的。
NOTE_LENGTH_BUCKETS = (
    (5, '5 字以内'),
    (15, '6-15 字'),
    (40, '16-40 字'),
    (None, '40 字以上'),
)

# 已完结状态。只有判完了才有「驳回」可言。
CLOSED_STATUSES = ('resolved', 'rejected')

# 还在流程里的状态：右删失样本，单独数出规模，不混入任何比率的分母。
OPEN_STATUSES = ('pending', 'reviewing')

# 举报人的发单数分档：只发过一条的是应急，反复在用的说明这条通道对他是常规手段。
# 商品「还在售」的取值。写死 'active' 是一个安静的 bug：Item.STATUS_CHOICES 里
# 从来没有 'active'，拿它去比会把 reserved/sold/expired 连同 available 一起算成
# 「已不在售」，在售数直接归零，而看板上只显示一个 0，看不出哪里错了。
LISTED_STATUS = 'available'
# 已经不在售的状态：被预订、已售出、已过期。
UNLISTED_STATUSES = ('reserved', 'sold', 'expired')

REPORTER_COUNT_BANDS = (
    (1, 1, '只举报过 1 次'),
    (2, 3, '2-3 次'),
    (4, 9, '4-9 次'),
    (10, None, '10 次及以上'),
)

# 模块需要读的列。保持显式：365 天视图上 Report 可能很大，只取用得上的。
# 不读 detail、不读 review_note——两处都是自由文本，可能写着电话、微信、楼栋号。
PERIOD_FIELDS = (
    'id',
    'reason',
    'status',
    'review_note',
    'reviewed_at',
    'created_at',
    'item_id',
    'reporter_id',
)

# 一条举报，连它指向的商品状态一起取回来。一次查完固定下来：驳回率、意见填写率、
# 集中度、举报人分布各读一遍，按记录现算会让每条举报打好几遍库，
# 而看板是一次打开全算的。
ReportRecord = namedtuple('ReportRecord', [
    'report', 'item_status', 'item_seller_id',
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
    return record.report['status'] in CLOSED_STATUSES


def _is_rejected(record):
    return record.report['status'] == 'rejected'


def _note_text(record):
    """审核意见去空白后的文本。

    只取长度和空否，一个字符内容都不读——模板和测试也只该看到长度。
    """
    return (record.report['review_note'] or '').strip()


def _fetch_records(days, now):
    """周期内创建的举报，连它指向的商品状态一起取回来。

    商品状态用来回答「被驳回的举报指向的商品还在不在售」：一件已经被下架的商品被举报，
    和一件正在卖的商品被举报，是两件不同的运营动作。

    按创建时间与主键显式排序：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    """
    start = _period_start(days, timezone.localtime(now).date())
    rows = list(
        Report.objects.filter(created_at__gte=start, created_at__lte=now)
        .values(*PERIOD_FIELDS)
        .order_by('created_at', 'id')
    )
    # 商品状态一次查完。商品可能已经被删（on_delete=CASCADE 会连坐删掉举报，
    # 所以正常路径下 item_id 一定指向一件还在的商品），取不到就不取。
    from .models import Item
    item_ids = {row['item_id'] for row in rows if row['item_id']}
    item_map = {}
    if item_ids:
        for item in Item.objects.filter(id__in=item_ids).values('id', 'status', 'seller_id'):
            item_map[item['id']] = item
    return [
        ReportRecord(
            report=row,
            item_status=item_map.get(row['item_id'], {}).get('status'),
            item_seller_id=item_map.get(row['item_id'], {}).get('seller_id'),
        )
        for row in rows
    ]


def _build_reason_rows(records):
    """驳回率按 reason 拆。

    四种举报原因的举证难度完全不是一回事。把它们平均成一个总驳回率，这个数一旦波动，
    运营第一反应是去查处理人，而不是去查是哪一类举报在涨。

    行取自 `REASON_CHOICES` 与库里的并集——库里出现了选择项之外的原因（脏数据、或者
    以后加了选择项而老数据还在）也要露出来。只报一边，就等于假设另一边是齐的。
    """
    from .models import Report as ReportModel
    reason_labels = dict(ReportModel.REASON_CHOICES)
    grouped = {}
    for record in records:
        grouped.setdefault(record.report['reason'], []).append(record)

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
            # 空档也要报出来：周期内一条都没有的举报原因，报出来是「这一类没人用」，
            # 而漏掉它会让读者以为这一类也是齐的。
            'is_empty': not reason_records,
            'is_small_sample': 0 < len(closed) < MIN_BUCKET_SAMPLE,
        })
    # 已完结样本多的排在前面：最该被读的那一类在最上面。
    result.sort(key=lambda row: (-row['closed_count'], row['reason']))
    return result


def _build_outcome_stats(records):
    """总结局：处理了多少、驳回多少、还在流程里多少。"""
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
        # 空分母报 None：一条已完结举报都没有的时候，驳回率不存在，不是零。
        'rejection_rate': _percent_or_none(len(rejected), len(closed)),
        'has_closed': bool(closed),
    }


def _build_note_stats(records):
    """审核意见只报「写了没写」和长度，一个字符内容都不读。

    按结局拆填写率。被驳回的举报如果普遍不写意见，用户收到的是一个没有理由的拒绝——
    这是这套治理里最伤人的一种体验，单独数出来，不混进填写率。

    **不报「长度和驳回率的关系」。** 长意见是不是更难被申诉，观测数据里分不开：
    意见写得长可能只是这类举报本身复杂。只报形状，不给方向。
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
        ('resolved', '已处理', ('resolved',)),
        ('rejected', '已驳回', ('rejected',)),
    ):
        group = [
            record for record in closed
            if record.report['status'] in statuses
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
            'is_empty': not group,
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
    """已处理与已驳回两组的意见填写率之差（百分点）。

    拿不到任一侧的分母时报 None：一组一条已完结举报都没有的时候，这个差不存在。
    """
    rates = [row['fill_rate'] for row in by_outcome]
    if any(rate is None for rate in rates):
        return None
    return round(rates[1] - rates[0], 1)


def _build_concentration(records):
    """被驳回的举报集中在哪些商品、哪些卖家身上。

    **只报规模与集中度，不列商品名、不列卖家名。** 一份「被驳回举报最多的商品榜」是一份
    公开处刑清单，不是看板。运营要的是「集中在少数几件上」还是「摊在一大片上」这个形状
    ——前者说明有几件商品有问题，后者说明举报标准可能有问题。
    """
    rejected = [
        record for record in records
        if _is_closed(record) and _is_rejected(record)
    ]
    if not rejected:
        return {
            'rejected_count': 0,
            'item_count': 0,
            'seller_count': 0,
            'inactive_item_count': 0,
            'repeat_item_count': 0,
            'top_item_count': 0,
            'is_empty': True,
        }

    item_counts = {}
    seller_ids = set()
    inactive_items = set()
    for record in rejected:
        item_id = record.report['item_id']
        item_counts[item_id] = item_counts.get(item_id, 0) + 1
        if record.item_seller_id:
            seller_ids.add(record.item_seller_id)
        # 商品已被删或取不到状态时不猜，只统计拿得到的。
        if record.item_status in UNLISTED_STATUSES:
            inactive_items.add(item_id)

    counts = sorted(item_counts.values(), reverse=True)
    top_item_count = counts[0] if counts else 0
    # 重复出现的商品：被驳回的举报里指向同一件商品超过一次。
    repeat_item_count = sum(1 for value in counts if value > 1)

    return {
        'rejected_count': len(rejected),
        'item_count': len(item_counts),
        'seller_count': len(seller_ids),
        'inactive_item_count': len(inactive_items),
        'repeat_item_count': repeat_item_count,
        'top_item_count': top_item_count,
        # 集中度：被驳回的举报指向的商品平均数。接近 1 说明一举报对一商品，
        # 明显大于 1 说明有几件商品在被反复举报后被驳回。
        'items_per_report': round(len(rejected) / len(item_counts), 2),
        'is_empty': False,
    }


def _build_reporter_rows(records):
    """举报的人是不是同一批人。

    **不排名、不报任何人的用户名。** 把「谁最爱举报」做成一张排行榜，平台就从仲裁者变成了
    一个记录谁难相处的小本子。报的是分布形状：绝大多数人只举报过一次，说明这条通道是
    **应急**性质的；有一批人反复在用，说明它是某些用户的**常规**手段。

    受 `UniqueConstraint(fields=['item', 'reporter'])` 限制，**不报「同一用户对同一商品的
    重复举报」**——那个数结构上恒为 0，报出来是一个永远为 0 的常数，没有信息量。
    """
    counts = {}
    for record in records:
        reporter_id = record.report['reporter_id']
        if not reporter_id:
            continue
        counts[reporter_id] = counts.get(reporter_id, 0) + 1

    buckets = [{'label': label, 'count': 0} for _low, _high, label in REPORTER_COUNT_BANDS]
    for count in counts.values():
        buckets[_band_index(count)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent_or_none(bucket['count'], len(counts))

    # 只举报过一次的人占比。这是「应急性质」最直接的一个数。
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


def _summary(outcome, reason_rows, note, concentration, reporters):
    """一段人话，说明这条治理队列此刻公不公平。"""
    if not outcome['total_count']:
        return (
            '当前周期还没有举报记录。商品举报是站内最早的治理队列，'
            '它此刻没有发生——这些数都不存在，不是零。'
        )
    parts = [
        f"周期内 {outcome['total_count']} 条举报，已完结 {outcome['closed_count']} 条"
        f"（处理 {outcome['resolved_count']} 条、驳回 {outcome['rejected_count']} 条），"
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
        parts.append(f"样本够的举报原因里，「{top['label']}」驳回率最高（{top['rejection_rate']}%）。")
    if not note['is_empty']:
        parts.append(
            f"审核意见填写率 {note['fill_rate']}%"
            f"（已驳回的 {note['by_outcome'][1]['fill_rate'] if note['by_outcome'][1]['fill_rate'] is not None else '—'}%"
            f" vs 已处理的 {note['by_outcome'][0]['fill_rate'] if note['by_outcome'][0]['fill_rate'] is not None else '—'}%）。"
        )
    if not concentration['is_empty']:
        parts.append(
            f"被驳回的 {concentration['rejected_count']} 条指向 {concentration['item_count']} 件商品、"
            f"{concentration['seller_count']} 位卖家。"
        )
    if not reporters['is_empty']:
        parts.append(
            f"{reporters['reporter_count']} 位举报人里 {reporters['once_only_rate']}% 只举报过一次。"
        )
    return ''.join(parts)


def build_report_resolution_quality(days=30, now=None):
    """统计商品举报的处理质量：哪一类更容易被驳回、理由写没写、集中在哪、谁在举报。只报告。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    records = _fetch_records(days, now)
    outcome = _build_outcome_stats(records)
    reason_rows = _build_reason_rows(records)
    note = _build_note_stats(records)
    concentration = _build_concentration(records)
    reporters = _build_reporter_rows(records)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(records),
        'summary': _summary(outcome, reason_rows, note, concentration, reporters),
        'outcome': outcome,
        'reason_rows': reason_rows,
        'note': note,
        'concentration': concentration,
        'reporters': reporters,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
        'min_closed_reports': MIN_CLOSED_REPORTS,
        'noise_points': NOISE_POINTS,
    }
