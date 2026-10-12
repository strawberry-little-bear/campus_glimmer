# -*- coding: utf-8 -*-
"""交易争议处理得公不公平，第一次被运营读到。

`OrderDispute` 是站内唯一一条「一笔交易走不下去，双方自己谈不拢，请平台介入」的记录。
`governance_sla.py` 已经把它并进了治理队列，和商品举报、交付预约异常、借用逾期催收放在
一张表里比时效——那个模块回答的是「这条争议多久被处理」。它从来没有回答过另外三个
问题，而这三个问题比时效更接近「治理公不公平」：哪一类争议更容易被驳回；证据提交得
晚不晚；处理意见写了没有。更麻烦的是从来没有人从「谁发起」这个方向看过争议，而它是
这套治理里唯一的方向信号。

这个模块把它们读出来，只报告，不改写任何一条争议、任何一条证据、任何一份处理意见。
几处口径上的取舍比数字本身更要紧，它们都在下面的注释里。

驳回率按 `reason` 拆，不按总数报。五种争议的举证难度完全不是一回事：「没收到商品」
要看交付确认，「商品与描述不符」要看聊天记录和实物照片。把它们平均成一个总驳回率，
这个数一旦波动，运营第一反应是去查处理人，而不是去查是哪一类争议在涨。按 reason
拆开之后，「不符」类驳回率上升和「没收到」类驳回率上升是两条完全不同的线索。

证据只报「进来得多晚」，不读内容、不判充分性。按 `OrderDisputeEvidence.created_at`
减它所属争议的 `created_at` 算提交间隔，只报进来了多少、进来得多晚、进来在什么状态下。
不读 `note`（自由文本，可能写着电话、微信、楼栋号），不数附件个数，不判断「证据够
不够」——平台没有能力判断一段聊天记录能不能证明「商品与描述不符」，这个判断只有处理
人能做。也不按 `uploaded_by` 拆：谁传的附件本身不带方向，拆开只会得到两个同样没有
解释力的数。

「有没有附件」和「驳回不驳回」只做对照，不给因果。带至少一条附件的争议和不带附件的
争议各自算驳回率。一个发起时就附了截图的争议可能本身事实更清楚、更容易被支持；一个
什么附件都没有的争议也可能只是用户不知道怎么上传。平台没有能力把「证据起了作用」和
「这事本来就好判」分开，把它做成因果会直接变成一个对具体用户的负面标签——「你上次
没传截图所以被驳回了」——那是这套治理最不该变成的东西。两组各至少 8 条才给方向，
差值不满 8 个百分点视为噪声。

发起方向只报「谁发起的」，不报任何人的名字。`opened_by` 是买家还是卖家，按比例报，
不按用户聚合、不排名、不报任何人的用户名。把「谁最爱发起争议」做成一张排行榜，平台
就从仲裁者变成了一个记录谁难相处的小本子。方向分布报的是流程本身是否对双方可用：
如果只有一方发得起、发了总被驳，那流程就是偏的；如果双方发起率相当、驳回率也相当，
那流程是中的。这是流程的性质，不是人的性质。

处理意见只报「写了没写」和长度，一个字符内容都不读。`resolution_note` 报填写率和长度
分档。它是处理人写给双方的一段自由文本，可能写着电话、微信、楼栋号，也可能写着处理人
的判断和情绪。这个模块只关心一件事：这段理由有没有被写下来。至于理由写得好不好，那是
复核人该看的，本模块不替代复核人。

已完结的争议才算进驳回率。分母只收 `status` 为 `resolved` 或 `rejected` 的争议。还挂在
`open` / `reviewing` 的是右删失样本：争议还没判，谈不上被驳回还是被支持，把它算进分母
会让驳回率凭空变小。这部分单独数出规模，不丢也不混。这和订单阶段流、归还握手、交易
评价覆盖率是同一个道理。

处理时长只报两段，不报差值。一条争议从发起到结案，中间可能有一段是在等某一方补证据。
本模块报「从发起到第一条附件进来隔了多久」和「从发起到结案隔了多久」，**不报「等材料
花了多久」这个差值**：结案慢可能是材料来得晚，也可能是处理人忙，还可能是双方在谈，
平台没有能力把这三件事分开，报一个差值等于把处理人的效率问题算成了用户的问题。两段
各自单独报，运营自己判断。
"""

from collections import namedtuple
from datetime import timedelta

from django.utils import timezone

from .models import OrderDispute, OrderDisputeEvidence


# 统计窗口的可选天数，和看板其他模块保持一致。
PERIOD_CHOICES = (
    (7, '近 7 天'),
    (30, '近 30 天'),
    (90, '近 90 天'),
)

# 给出方向前至少要有这么多条已完结的争议。争议是低频事件，一个校园平台一个月
# 可能只有寥寥几条，所以门槛定得比交易评价低，但不能再低：低于这个数，一个
# 百分点的波动就是一条争议，报出来是噪声。
MIN_CLOSED_DISPUTES = 8

# 两组占比相差不满这么多个百分点，视为噪声。争议量小，同样的绝对差值在这里
# 比在交易评价里更容易出现，所以门槛和那边一致，不放松。
NOISE_POINTS = 8

# 证据提交间隔的分档。按天走，理由和交易评价一样：补证据是一个「想起来才做」
# 的动作，小时级的抖动没有运营含义。
EVIDENCE_LATENCY_BUCKETS = (
    (3600, '1 小时内'),
    (86400, '1 天内'),
    (259200, '3 天内'),
    (604800, '1 周内'),
    (None, '1 周以上'),
)

# 处理意见长度分档。一段「经核实，情况属实」和一段写清了核实依据的意见，
# 在长度上是能分开的。
NOTE_LENGTH_BUCKETS = (
    (10, '10 字以内'),
    (30, '11-30 字'),
    (80, '31-80 字'),
    (None, '80 字以上'),
)

# 已完结状态。只有判完了才有「驳回」可言。
CLOSED_STATUSES = ('resolved', 'rejected')

# 还在流程里的状态：右删失样本，单独数出规模，不混入任何比率的分母。
OPEN_STATUSES = ('open', 'reviewing')

# 清单上限。清单是给「现在去核实」用的，不是给归档用的。
LIST_LIMIT = 20


# 一条争议，连它的证据和处理意见一起取回来。一次查完固定下来：驳回率、证据
# 时延、方向分布、意见填写率各读一遍，按记录现算会让每条争议打好几遍库，
# 而看板是一次打开全算的。
DisputeRecord = namedtuple('DisputeRecord', [
    'dispute', 'evidence', 'first_evidence_at', 'evidence_count',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _format_duration(seconds):
    """把一个时长渲染成最粗但仍可读的单位。"""
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


def _bucket_index(seconds, buckets):
    """分档下标。桶是 (上限, 标签) 的元组，互斥、左闭右开。

    上限为 None 的那一档是最后一档，兜住所有超过前一档的样本。
    """
    for index, bucket in enumerate(buckets):
        limit = bucket[0]
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime``
    类本身。
    """
    from datetime import time as dt_time
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _fetch_disputes(*, created_after=None, created_before=None):
    """按显式排序取出争议，连它们的证据一起取回来。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「最早一条」都会随机漂移。

    证据单独一次查完再按争议分组，不要按记录逐个查——那会让每条争议多打一次库。
    """
    disputes = OrderDispute.objects.select_related(
        'order', 'order__item', 'opened_by',
    ).order_by('created_at', 'id')
    if created_after is not None:
        disputes = disputes.filter(created_at__gte=created_after)
    if created_before is not None:
        disputes = disputes.filter(created_at__lte=created_before)
    disputes = list(disputes)

    if not disputes:
        return []

    # 证据按争议分组，组内按时间排序，第一条就是最早进来的那条。附件本身不带
    # 方向，所以这里不按 uploaded_by 拆，也不读 note 的任何一个字符。
    evidence = {}
    for row in OrderDisputeEvidence.objects.filter(
        dispute__in=disputes,
    ).order_by('created_at', 'id'):
        evidence.setdefault(row.dispute_id, []).append(row)

    records = []
    for dispute in disputes:
        rows = evidence.get(dispute.id, [])
        first_at = rows[0].created_at if rows else None
        records.append(DisputeRecord(
            dispute=dispute,
            evidence=rows,
            first_evidence_at=first_at,
            evidence_count=len(rows),
        ))
    return records


def _build_outcome_stats(records):
    """已完结争议的驳回率，按 reason 拆开，不按总数报。

    分母只收 `resolved` / `rejected`。还挂在 `open` / `reviewing` 的是右删失
    样本：争议还没判，谈不上被驳回还是被支持，把它算进分母会让驳回率凭空变小，
    这部分单独数出规模，不丢也不混。
    """
    closed = [record for record in records if record.dispute.status in CLOSED_STATUSES]
    censored = [record for record in records if record.dispute.status in OPEN_STATUSES]
    rejected = [record for record in closed if record.dispute.status == 'rejected']

    rows = []
    for key, label in OrderDispute.REASON_CHOICES:
        group = [record for record in closed if record.dispute.reason == key]
        group_rejected = [
            record for record in group if record.dispute.status == 'rejected'
        ]
        if not group:
            continue
        rows.append({
            'key': key,
            'label': label,
            'closed_count': len(group),
            'resolved_count': len(group) - len(group_rejected),
            'rejected_count': len(group_rejected),
            'rejected_rate': _percent(len(group_rejected), len(group)),
            # 没有证据的那部分单独数出来：它和驳回率是两回事，但运营要能一起看。
            'no_evidence_count': sum(
                1 for record in group if not record.evidence_count
            ),
        })
    rows.sort(key=lambda row: -row['closed_count'])

    return {
        'rows': rows,
        'closed_count': len(closed),
        'resolved_count': len(closed) - len(rejected),
        'rejected_count': len(rejected),
        'rejected_rate': _percent(len(rejected), len(closed)),
        'censored_count': len(censored),
        'censored_note': (
            f'{len(censored)} 条争议还在 open / reviewing，'
            '是右删失样本，不计入驳回率分母'
        ) if censored else '',
        'is_small_sample': len(closed) < MIN_CLOSED_DISPUTES,
    }


def _build_evidence_stats(records):
    """证据什么时候进来、进来在什么状态下。

    按 `OrderDisputeEvidence.created_at` 减所属争议的 `created_at` 算提交间隔。
    只报进来了多少、进来得多晚、进来在什么状态下，不读 `note` 的任何一个字符，
    不数附件个数，不判断「证据够不够」，也不按 `uploaded_by` 拆——谁传的附件
    本身不带方向，拆开只会得到两个同样没有解释力的数。
    """
    with_evidence = [record for record in records if record.evidence_count]
    without_evidence = [record for record in records if not record.evidence_count]

    latencies = []
    for record in with_evidence:
        delta = record.first_evidence_at - record.dispute.created_at
        latencies.append(max(delta.total_seconds(), 0.0))

    buckets = [{'label': label, 'count': 0, 'note': ''} for _limit, label in EVIDENCE_LATENCY_BUCKETS]
    for seconds in latencies:
        buckets[_bucket_index(seconds, EVIDENCE_LATENCY_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(latencies))
    # 只有「争议还没判的时候证据就进来了」这一档有运营含义：它意味着处理人是
    # 带着材料判的。其余各档只说明用户什么时候想起来补。
    if buckets:
        buckets[0]['note'] = '处理开始时材料已经在手上'

    # 证据进来时争议处于什么状态。争议的 status 会被 resolve_dispute 改写，
    # 但证据带自己的 created_at，所以这里按「证据进来时争议是否已完结」判断：
    # 结案时间早于证据时间的，是结案之后才补的。
    late_count = 0
    for record in with_evidence:
        if record.dispute.resolved_at and record.first_evidence_at > record.dispute.resolved_at:
            late_count += 1

    median_seconds = _percentile(latencies, 0.5)
    p90_seconds = _percentile(latencies, 0.9)
    return {
        'dispute_count': len(records),
        'with_evidence_count': len(with_evidence),
        'without_evidence_count': len(without_evidence),
        'evidence_rate': _percent(len(with_evidence), len(records)),
        'attachment_count': sum(record.evidence_count for record in records),
        'bucket_rows': buckets,
        'measured_count': len(latencies),
        'median_seconds': median_seconds,
        'median_label': _format_duration(median_seconds),
        'p90_seconds': p90_seconds,
        'p90_label': _format_duration(p90_seconds),
        'average_label': _format_duration(_mean(latencies)),
        'fastest_label': _format_duration(min(latencies)) if latencies else '—',
        'slowest_label': _format_duration(max(latencies)) if latencies else '—',
        # 结案之后才补进来的附件：它没有参与这次裁定，报出来是让运营知道
        # 有多少材料是「事后补的」，不参与任何比率的分子分母。
        'after_close_count': late_count,
        'after_close_rate': _percent(late_count, len(with_evidence)),
    }


def _build_evidence_contrast(records):
    """「有没有附件」和「驳回不驳回」只做对照，不给因果。

    两组各至少 MIN_CLOSED_DISPUTES 条已完结争议才给方向，差值不满
    NOISE_POINTS 个百分点视为噪声。一个发起时就附了截图的争议可能本身事实更
    清楚、更容易被支持；一个什么附件都没有的争议也可能只是用户不知道怎么上传。
    平台没有能力把「证据起了作用」和「这事本来就好判」分开，做成因果会变成一个
    对具体用户的负面标签。
    """
    closed = [record for record in records if record.dispute.status in CLOSED_STATUSES]
    with_evidence = [record for record in closed if record.evidence_count]
    without_evidence = [record for record in closed if not record.evidence_count]

    def rejected_rate(group):
        if not group:
            return None
        rejected = sum(1 for record in group if record.dispute.status == 'rejected')
        return _percent(rejected, len(group))

    with_rate = rejected_rate(with_evidence)
    without_rate = rejected_rate(without_evidence)

    has_both_sides = (
        len(with_evidence) >= MIN_CLOSED_DISPUTES
        and len(without_evidence) >= MIN_CLOSED_DISPUTES
    )
    gap = None
    if has_both_sides and with_rate is not None and without_rate is not None:
        gap = round(without_rate - with_rate, 1)

    return {
        'rows': [
            {
                'key': 'with_evidence',
                'label': '带了附件',
                'closed_count': len(with_evidence),
                'rejected_count': sum(
                    1 for record in with_evidence if record.dispute.status == 'rejected'
                ),
                'rejected_rate': with_rate if with_rate is not None else '—',
            },
            {
                'key': 'without_evidence',
                'label': '没带附件',
                'closed_count': len(without_evidence),
                'rejected_count': sum(
                    1 for record in without_evidence if record.dispute.status == 'rejected'
                ),
                'rejected_rate': without_rate if without_rate is not None else '—',
            },
        ],
        'has_both_sides': has_both_sides,
        'gap': gap,
        # 噪声门槛内不给方向。超出也只是对照，不构成因果。
        'is_noise': gap is not None and abs(gap) < NOISE_POINTS,
        'min_closed': MIN_CLOSED_DISPUTES,
    }


def _build_opener_stats(records):
    """争议是谁发起的，只按方向报，不按用户聚合、不排名、不报任何人的用户名。

    把「谁最爱发起争议」做成一张排行榜，平台就从仲裁者变成了一个记录谁难相处的
    小本子。方向分布报的是流程本身是否对双方可用：只有一方发得起、发了总被驳，
    那流程就是偏的；双方发起率相当、驳回率也相当，那流程是中的。这是流程的性质，
    不是人的性质。
    """
    buyer_opened = [record for record in records if record.dispute.opened_by_id == record.dispute.order.buyer_id]
    seller_opened = [record for record in records if record.dispute.opened_by_id == record.dispute.order.seller_id]

    def rejected_rate(group):
        closed = [record for record in group if record.dispute.status in CLOSED_STATUSES]
        if not closed:
            return None
        rejected = sum(1 for record in closed if record.dispute.status == 'rejected')
        return _percent(rejected, len(closed))

    rows = []
    for key, label, group in (
        ('buyer', '买家发起', buyer_opened),
        ('seller', '卖家发起', seller_opened),
    ):
        closed = [record for record in group if record.dispute.status in CLOSED_STATUSES]
        rejected = [record for record in closed if record.dispute.status == 'rejected']
        rows.append({
            'key': key,
            'label': label,
            'opened_count': len(group),
            'opened_rate': _percent(len(group), len(records)),
            'closed_count': len(closed),
            'rejected_count': len(rejected),
            'rejected_rate': _percent(len(rejected), len(closed)),
        })

    return {
        'rows': rows,
        'total': len(records),
        'is_small_sample': len(records) < MIN_CLOSED_DISPUTES,
        'min_closed': MIN_CLOSED_DISPUTES,
    }


def _build_note_stats(records):
    """处理意见只报「写了没写」和长度，一个字符内容都不读。

    `resolution_note` 是处理人写给双方的一段自由文本，可能写着电话、微信、
    楼栋号，也可能写着处理人的判断和情绪。这个模块只关心一件事：这段理由有没有
    被写下来。至于理由写得好不好，那是复核人该看的，本模块不替代复核人。
    """
    closed = [record for record in records if record.dispute.status in CLOSED_STATUSES]
    with_note = [record for record in closed if record.dispute.resolution_note.strip()]
    without_note = [record for record in closed if not record.dispute.resolution_note.strip()]

    lengths = [len(record.dispute.resolution_note.strip()) for record in with_note]
    buckets = [{'label': label, 'count': 0} for _limit, label in NOTE_LENGTH_BUCKETS]
    for length in lengths:
        buckets[_bucket_index(length, NOTE_LENGTH_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(lengths))

    # 驳回却没写意见，是这套治理里最该被看见的一格：双方收到了「已驳回」的
    # 通知，却不知道为什么。单独数出来，不混进填写率。
    rejected_without_note = [
        record for record in without_note if record.dispute.status == 'rejected'
    ]

    return {
        'closed_count': len(closed),
        'with_note_count': len(with_note),
        'without_note_count': len(without_note),
        'fill_rate': _percent(len(with_note), len(closed)),
        'bucket_rows': buckets,
        'average_length': round(_mean(lengths), 1) if lengths else 0,
        'rejected_without_note_count': len(rejected_without_note),
        'rejected_without_note_rate': _percent(len(rejected_without_note), len(without_note)),
        'rows': [
            {
                'key': 'with_note',
                'label': '写了处理意见',
                'count': len(with_note),
                'share': _percent(len(with_note), len(closed)),
            },
            {
                'key': 'without_note',
                'label': '一个字都没写',
                'count': len(without_note),
                'share': _percent(len(without_note), len(closed)),
                'note': '双方只收到状态，不知道为什么' if without_note else '',
            },
        ],
    }


def _build_resolution_latency(records):
    """从发起到结案隔了多久。

    本模块报两段：从发起到第一条附件进来隔了多久（在 `_build_evidence_stats`
    里），和从发起到结案隔了多久（在这里）。**不报「等材料花了多久」这个差值**：
    结案慢可能是材料来得晚，也可能是处理人忙，还可能是双方在谈，平台没有能力
    把这三件事分开，报一个差值等于把处理人的效率问题算成了用户的问题。两段各自
    单独报，运营自己判断。
    """
    closed = [record for record in records if record.dispute.status in CLOSED_STATUSES]
    latencies = []
    for record in closed:
        if not record.dispute.resolved_at:
            continue
        delta = record.dispute.resolved_at - record.dispute.created_at
        latencies.append(max(delta.total_seconds(), 0.0))

    buckets = [{'label': label, 'count': 0} for _limit, label in EVIDENCE_LATENCY_BUCKETS]
    for seconds in latencies:
        buckets[_bucket_index(seconds, EVIDENCE_LATENCY_BUCKETS)]['count'] += 1
    for bucket in buckets:
        bucket['share'] = _percent(bucket['count'], len(latencies))

    # 已完结但找不到 resolved_at 的：属于数据本身不完整，单独数出来，
    # 不拿 updated_at 近似——任何一次保存都会动 updated_at。
    missing_resolved_at = sum(
        1 for record in closed if not record.dispute.resolved_at
    )

    return {
        'measured_count': len(latencies),
        'bucket_rows': buckets,
        'median_label': _format_duration(_percentile(latencies, 0.5)),
        'p90_label': _format_duration(_percentile(latencies, 0.9)),
        'average_label': _format_duration(_mean(latencies)),
        'slowest_label': _format_duration(max(latencies)) if latencies else '—',
        'missing_resolved_at_count': missing_resolved_at,
    }


def _build_slowest_rows(records):
    """结案最慢的几条争议，给「现在去核实」用。"""
    closed = [
        record for record in records
        if record.dispute.status in CLOSED_STATUSES and record.dispute.resolved_at
    ]
    rows = []
    for record in closed:
        delta = record.dispute.resolved_at - record.dispute.created_at
        seconds = max(delta.total_seconds(), 0.0)
        rows.append({
            'dispute': record.dispute,
            'seconds': seconds,
            # label 在这里算好：看板和 CSV 导出都直接用它，不在两边各写一遍
            # 格式化——那正是同一个数字在屏幕上和下载回去的表里长成两个样子
            # 的来源。
            'duration_label': _format_duration(seconds),
        })
    rows.sort(key=lambda row: -row['seconds'])
    return {
        'rows': rows[:LIST_LIMIT],
        'row_total': len(rows),
        'row_limit': LIST_LIMIT,
    }


def _summary(outcome, evidence, contrast, opener, note, latency):
    """一段话把这个模块的读数讲完，样本不足的地方明说。"""
    parts = []
    if outcome['closed_count']:
        parts.append(
            f"周期内 {outcome['closed_count']} 条争议已结案，"
            f"驳回 {outcome['rejected_count']} 条（{outcome['rejected_rate']}%）"
        )
        if outcome['is_small_sample']:
            parts.append(f"，不足 {MIN_CLOSED_DISPUTES} 条，占比只作参考")
    else:
        parts.append('当前周期还没有已结案的交易争议。')

    if outcome['censored_count']:
        parts.append(f"；另有 {outcome['censored_count']} 条还在处理中，不计入分母")

    if outcome['rows']:
        worst = max(outcome['rows'], key=lambda row: row['rejected_rate'])
        if worst['rejected_count'] and not outcome['is_small_sample']:
            parts.append(
                f"。{worst['label']}类驳回率最高，{worst['rejected_count']}/"
                f"{worst['closed_count']} 条"
            )

    if evidence['measured_count']:
        parts.append(
            f"。{evidence['with_evidence_count']} 条争议带了附件，"
            f"最早一条中位 {evidence['median_label']} 进来"
        )
        if evidence['after_close_count']:
            parts.append(
                f"，其中 {evidence['after_close_count']} 条是结案之后才补的"
            )

    if contrast['has_both_sides'] and contrast['gap'] is not None:
        if contrast['is_noise']:
            parts.append(
                f"。带附件和不带附件的驳回率相差 {abs(contrast['gap'])} 个百分点，"
                f"在噪声范围内"
            )
        else:
            parts.append(
                f"。不带附件的争议驳回率比带附件的高 {contrast['gap']} 个百分点"
                f"——这只是对照，不构成因果"
            )

    if note['closed_count']:
        parts.append(f"。{note['fill_rate']}% 的结案写了处理意见")
        if note['rejected_without_note_count']:
            parts.append(
                f"，其中 {note['rejected_without_note_count']} 条驳回一个字都没写，"
                f"双方只收到状态"
            )

    if latency['measured_count']:
        parts.append(f"。结案中位耗时 {latency['median_label']}")
    return ''.join(parts)


def build_dispute_resolution_quality(days=30, now=None):
    """统计交易争议的处理结果、证据时延、发起方向与处理意见填写率，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    records = _fetch_disputes(created_after=start, created_before=now)

    outcome = _build_outcome_stats(records)
    evidence = _build_evidence_stats(records)
    contrast = _build_evidence_contrast(records)
    opener = _build_opener_stats(records)
    note = _build_note_stats(records)
    latency = _build_resolution_latency(records)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(records),
        'summary': _summary(outcome, evidence, contrast, opener, note, latency),
        'outcome': outcome,
        'evidence': evidence,
        'evidence_contrast': contrast,
        'opener': opener,
        'note': note,
        'resolution_latency': latency,
        'slowest_rows': _build_slowest_rows(records),
        'min_closed_disputes': MIN_CLOSED_DISPUTES,
        'noise_points': NOISE_POINTS,
    }
