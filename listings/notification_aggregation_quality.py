# -*- coding: utf-8 -*-
"""通知聚合的质量：合并掉的是打扰，还是信息。

``create_notification`` 收到 ``dedupe_key`` 之后，在 ``dedupe_window_seconds`` 窗口内找到
同接收人、同类型、同键的**未读**行就合并：``occurrence_count += 1``、``last_occurred_at = now``，
并覆盖 ``actor`` / ``message`` / ``target_url``，``title`` 不动；``dedupe_forever`` 分支连
「未读」这个条件都不要。

读这两个字段的地方只有一个：``analytics.py`` 里的 ``compression_rate``——
``(event_count - row_count) / event_count``。它是一个比例，而比例天生看不出样本：
「合并率 60%」既可以是同一件事两分钟内响了三次（聚拢），也可以是同一个提醒拖了四天、
用户一次没点开（堆积）。**两种完全相反的含义，在看板上是同一个数字。**

``notification_response.py`` 把 ``occurrence_count`` 和 ``last_occurred_at`` 放进了
``PERIOD_FIELDS``，但拿它们只做了一件事：什么也没做。本模块把这两个字段和阅读行为连起来：
聚合过的行 vs 从没被聚合过的行，阅读率与处理时长对照；聚合跨度的分布；按类型拆聚合率。

本模块只报告：不改 ``create_notification``、不改 ``dedupe_window_seconds`` 默认值、不加迁移。
"""

from datetime import timedelta

from django.utils import timezone

from .models import Notification

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一个类型的规模至少要这么多条，才允许对它下「这一类在反复响」的判断。和通知类型版图、
# 搜索位置质量、求购匹配质量、跟进任务接手等待同一个量级：几十条样本上几个百分点的差距
# 是噪声。口径与门槛全站统一，读者在一处学会「12 条」的含义后，不该在另一处重新学一个数。
MIN_BUCKET_SAMPLE = 12

# 聚合次数分档。1 次是「键设了但窗口内没有第二次」，2–3 轻度，4–8 中度，9 及以上重度。
# 重度档没有上界：库里出现过 9 次以上的行本身就值得运营知道，再往上切分只会让每档的
# 样本更薄。
OCCURRENCE_BANDS = (
    (1, 1, '1 次（从未被合并）'),
    (2, 3, '2–3 次'),
    (4, 8, '4–8 次'),
    (9, None, '9 次及以上'),
)

# 跨度分桶：第一条到最后一条隔了多久。跨度和次数是两个独立的维度——9 次里 8 次发生在
# 10 分钟内，和 9 次摊在 5 天里，是两件不同的事。桶按秒给上界，None 是最后一档。
SPAN_BUCKETS = (
    (600, '10 分钟内'),
    (3600, '10 分钟到 1 小时'),
    (86400, '1 小时到 1 天'),
    (None, '超过 1 天'),
)

# 模块需要读的列。保持显式：365 天视图上 Notification 可能很大，只取用得上的。
# 不读 message——通知内容是收发双方的私域内容，本模块连正文都不取出来。
PERIOD_FIELDS = (
    'kind',
    'is_read',
    'read_at',
    'created_at',
    'occurrence_count',
    'last_occurred_at',
    'dedupe_key',
    'target_url',
)


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _mean(values):
    return sum(values) / len(values) if values else None


def _percentile(values, fraction):
    """Nearest-rank percentile，与 notification_response 同名函数同一个算法。

    两份私有工具各自可读，好过一个共享的隐藏依赖：notification_response 的分位数用在
    全站通知时效上，这里只用在一组对照里，语义不同。
    """
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime`` 类本身。
    这条沿用通知类型版图与延后通知回归模块的同名函数。
    """
    from datetime import time as dt_time
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(timezone.datetime.combine(start_date, dt_time.min))


def _format_duration(seconds):
    """把秒数渲染成最粗但仍可读的单位，与 notification_response 同一套刻度。"""
    if seconds is None:
        return '—'
    if seconds < 60:
        return f'{round(seconds)} 秒'
    if seconds < 3600:
        return f'{round(seconds / 60)} 分钟'
    if seconds < 86400:
        return f'{round(seconds / 3600)} 小时'
    return f'{round(seconds / 86400)} 天'


def _is_handled(row):
    """这一行是否带着可用的阅读时间。

    ``is_read=True`` 而 ``read_at IS NULL`` 的是阅读时间上线之前标记的老数据——通知类型
    版图模块已经如实漏过这一档，本模块同样处理：不算已读，也不并进未读。
    """
    return bool(row['is_read'] and row['read_at'])


def _response_seconds(row):
    """从送达（``created_at``）到阅读（``read_at``）的秒数。

    未读行返回 None：一条还没被读的通知，它的处理时长此刻不存在，不是零；当 0 算会凭空
    拉低均值。右删失样本单独数，不并进时长统计。
    """
    if not _is_handled(row):
        return None
    return max((row['read_at'] - row['created_at']).total_seconds(), 0.0)


def _span_seconds(row):
    """从第一条到最后一条隔了多久（秒）。

    只在 ``occurrence_count > 1`` 的行上有意义。``last_occurred_at`` 为空的合并行
    （合并发生在该字段上线之前的老数据）返回 None，如实漏过去。
    """
    if row['occurrence_count'] <= 1 or not row['last_occurred_at']:
        return None
    return max((row['last_occurred_at'] - row['created_at']).total_seconds(), 0.0)


def _band_index(count):
    for index, (low, high, _label) in enumerate(OCCURRENCE_BANDS):
        if high is None or count <= high:
            if count >= low:
                return index
    return len(OCCURRENCE_BANDS) - 1


def _span_index(seconds):
    for index, (limit, _label) in enumerate(SPAN_BUCKETS):
        if limit is None or seconds < limit:
            return index
    return len(SPAN_BUCKETS) - 1


def _fetch_period_rows(days, now):
    """周期内创建的通知，按创建时间与主键显式排序。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。

    只取**带聚合键**的行。``dedupe_key == ''`` 的通知走的是一条不同的路径（
    ``create_notification`` 末尾直接 create，不查重），把两者混在一起会让「1 次」这一档
    里塞满从未参与过聚合机制的行，稀释掉真正想看的对照。
    """
    start = _period_start(days, timezone.localtime(now).date())
    return list(
        Notification.objects.filter(
            created_at__gte=start, created_at__lte=now,
        )
        .exclude(dedupe_key='')
        .values(*PERIOD_FIELDS)
        .order_by('created_at', 'id')
    )


def _handled_rows(rows):
    return [row for row in rows if _is_handled(row)]


def _group_stats(rows):
    """一组行的行数、阅读数、阅读率、处理时长、未读数。

    阅读率的分母是**行数**而不是事件数：一条压着 9 件事的未读通知，用户只需要一次点击
    就能消掉它，但它压着的事一件都没被处理。用行数衡量触达，用事件数衡量信息量，两者
    分开报，不混在一个分母里。

    未读行单独数。它是右删失样本：此刻未读，不等于永远不读。
    """
    handled = _handled_rows(rows)
    latencies = [
        seconds for seconds in
        (_response_seconds(row) for row in handled)
        if seconds is not None
    ]
    return {
        'row_count': len(rows),
        'event_count': sum(row['occurrence_count'] for row in rows),
        'read_count': len(handled),
        'unread_count': len(rows) - len(handled),
        'read_rate': _percent(len(handled), len(rows)),
        'avg_seconds': _mean(latencies),
        'avg_label': _format_duration(_mean(latencies)),
        'p50_seconds': _percentile(latencies, 0.5),
        'p90_seconds': _percentile(latencies, 0.9),
        'p90_label': _format_duration(_percentile(latencies, 0.9)),
    }


def _build_occurrence_bands(rows):
    """按聚合次数分档，每档报行数、阅读率、平均跨度。"""
    buckets = [[] for _ in OCCURRENCE_BANDS]
    for row in rows:
        buckets[_band_index(row['occurrence_count'])].append(row)

    result = []
    for index, (low, high, label) in enumerate(OCCURRENCE_BANDS):
        band_rows = buckets[index]
        if not band_rows:
            # 空档报 0 行、报 None 比率：这一档一条都没有，说不出阅读率。报 0% 会被读成
            # 「发了没人读」，而这里的事实是「这一档没有样本」。
            result.append({
                'label': label,
                'low': low,
                'high': high,
                'row_count': 0,
                'read_count': 0,
                'read_rate': None,
                'avg_span_label': '—',
                'is_empty': True,
            })
            continue
        stats = _group_stats(band_rows)
        spans = [
            seconds for seconds in
            (_span_seconds(row) for row in band_rows)
            if seconds is not None
        ]
        result.append({
            'label': label,
            'low': low,
            'high': high,
            'row_count': stats['row_count'],
            'read_count': stats['read_count'],
            'read_rate': stats['read_rate'],
            'avg_span_label': _format_duration(_mean(spans)),
            'is_empty': False,
        })
    return result


def _build_span_rows(rows):
    """聚合跨度的分布：第一条到最后一条隔了多久。"""
    buckets = [[] for _ in SPAN_BUCKETS]
    for row in rows:
        seconds = _span_seconds(row)
        if seconds is None:
            continue
        buckets[_span_index(seconds)].append(row)

    total = sum(len(bucket) for bucket in buckets)
    result = []
    for index, (limit, label) in enumerate(SPAN_BUCKETS):
        count = len(buckets[index])
        result.append({
            'label': label,
            'row_count': count,
            # 拿不到的分母报 None 不报 0：一行合并行都没有的时候，占比不存在。
            'share': _percent(count, total) if total else None,
            'is_empty': not count,
        })
    return result


def _build_aggregation_comparison(rows):
    """聚合过的行 vs 从没被聚合过的行——本模块的核心对照。

    两组之间差着**类型的构成**：被聚合的类型（求购匹配、关注搜索汇总、生命周期提醒）
    本身就可能比单次通知更容易被跳过。本模块只报「低」，不报「因为低」——因果要控制
    构成才能说，而控制构成不是这一张表能做的事。
    """
    single = [row for row in rows if row['occurrence_count'] <= 1]
    merged = [row for row in rows if row['occurrence_count'] > 1]
    groups = []
    for rows_in_group, label, description in (
        (single, '从没被聚合过（1 次）', '键设了，但窗口内没有第二次；或者这是唯一一次触发'),
        (merged, '被聚合过（2 次及以上）', '同一接收人、同类型、同键的事件被合并到一行'),
    ):
        stats = _group_stats(rows_in_group)
        stats.update({
            'label': label,
            'description': description,
            # 任一侧少于门槛，这一组的比率只报数不下判。两组阅读率差几个百分点在小样本上
            # 是噪声，把它读成「聚合有害」比不报更糟。
            'is_small_sample': 0 < stats['row_count'] < MIN_BUCKET_SAMPLE,
            'is_empty': not stats['row_count'],
        })
        groups.append(stats)

    single_stats, merged_stats = groups
    delta = None
    if single_stats['read_rate'] is not None and merged_stats['read_rate'] is not None:
        delta = round(merged_stats['read_rate'] - single_stats['read_rate'], 1)
    return {
        'groups': groups,
        'read_rate_delta': delta,
        # 两侧都过门槛才允许把差距读成一个信号；否则只报数。
        'comparable': bool(
            single_stats['row_count'] >= MIN_BUCKET_SAMPLE
            and merged_stats['row_count'] >= MIN_BUCKET_SAMPLE
        ),
    }


def _build_kind_rows(rows):
    """按类型拆聚合：哪一类在反复响。"""
    kind_labels = dict(Notification.KIND_CHOICES)
    grouped = {}
    for row in rows:
        grouped.setdefault(row['kind'], []).append(row)

    result = []
    for kind, kind_rows in grouped.items():
        sent = len(kind_rows)
        stats = _group_stats(kind_rows)
        merged_rows = [row for row in kind_rows if row['occurrence_count'] > 1]
        result.append({
            'kind': kind,
            'label': kind_labels.get(kind, kind),
            'row_count': stats['row_count'],
            'event_count': stats['event_count'],
            'merged_row_count': len(merged_rows),
            'read_rate': stats['read_rate'],
            # 聚合率 = (事件数 - 行数) / 事件数，即多少条事件被合并掉了。与 analytics.py
            # 同一个算法，这里只作为类型拆解的分母说明，不单独成榜。
            'aggregation_rate': _percent(
                max(stats['event_count'] - stats['row_count'], 0), stats['event_count'],
            ),
            'avg_occurrence': round(stats['event_count'] / sent, 2) if sent else None,
            'max_occurrence': max((row['occurrence_count'] for row in kind_rows), default=0),
            'avg_span_label': _format_duration(_mean([
                seconds for seconds in
                (_span_seconds(row) for row in merged_rows)
                if seconds is not None
            ])),
            'is_small_sample': 0 < sent < MIN_BUCKET_SAMPLE,
            'is_empty': False,
        })
    result.sort(key=lambda row: (-row['event_count'], row['kind']))
    return result


def _build_structure_rows(rows):
    """合并行的结构事实：落点缺失。

    合并会覆盖 ``message`` 和 ``target_url``，而 ``title`` 停留在第一次——用户点进去看到
    的东西，和标题说的可能不是一件事。这里只报 ``target_url`` 为空的比例这个结构事实，
    **不读正文、不比较标题与正文是不是一回事**：那要解析自然语言，超出本模块的口径。
    """
    merged = [row for row in rows if row['occurrence_count'] > 1]
    if not merged:
        return {
            'merged_row_count': 0,
            'missing_target_count': 0,
            'missing_target_rate': None,
            'is_empty': True,
        }
    missing = sum(1 for row in merged if not row['target_url'])
    return {
        'merged_row_count': len(merged),
        'missing_target_count': missing,
        'missing_target_rate': _percent(missing, len(merged)),
        'is_empty': False,
    }


def _summary(current, comparison, kind_rows):
    """一段人话，说明此刻的聚合是在省打扰还是在堆噪音。"""
    if not current['keyed_row_count']:
        return (
            '当前周期还没有带聚合键的通知。聚合是通知系统里唯一一条写入时合并的路径，'
            '它此刻没有发生——这些数都不存在，不是零。'
        )
    parts = [
        f"周期内 {current['keyed_row_count']} 行通知带聚合键，承载 {current['event_count']} 次事件，"
        f"合并掉了 {current['compressed_event_count']} 次。"
    ]
    if comparison['comparable']:
        single_rate = comparison['groups'][0]['read_rate']
        merged_rate = comparison['groups'][1]['read_rate']
        delta = comparison['read_rate_delta']
        if delta is not None and delta < 0:
            parts.append(
                f"被聚合过的行阅读率 {merged_rate}%，低于从没被聚合过的 {single_rate}%。"
                '这是两组之间的差距，不等于聚合本身有害——两组还差着类型的构成。'
            )
        elif delta is not None and delta > 0:
            parts.append(
                f"被聚合过的行阅读率 {merged_rate}%，高于从没被聚合过的 {single_rate}%。"
                '合并没有让这些行更难被读到。'
            )
        else:
            parts.append('两组阅读率没有差别。')
    hot = [
        row for row in kind_rows
        if not row['is_small_sample'] and (row['aggregation_rate'] or 0) > 0
    ]
    if hot:
        labels = '、'.join(row['label'] for row in hot[:3])
        parts.append(f"反复合并的类型集中在：{labels}。")
    return ''.join(parts)


def _build_current(rows):
    keyed_row_count = len(rows)
    event_count = sum(row['occurrence_count'] for row in rows)
    merged_row_count = sum(1 for row in rows if row['occurrence_count'] > 1)
    return {
        'keyed_row_count': keyed_row_count,
        'event_count': event_count,
        'merged_row_count': merged_row_count,
        'compressed_event_count': max(event_count - keyed_row_count, 0),
        # 聚合率只作为分母说明：它已经在 analytics.py 里孤零零地挂着了，重复报一遍
        # 不会让人以为这里有新信息。有信息的是它的对照与跨度。
        'aggregation_rate': _percent(
            max(event_count - keyed_row_count, 0), event_count,
        ) if event_count else None,
    }


def build_notification_aggregation_quality(days=30, now=None):
    """统计通知聚合的质量：合并掉的是打扰，还是信息。只报告，不改写入路径。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    rows = _fetch_period_rows(days, now)
    current = _build_current(rows)
    comparison = _build_aggregation_comparison(rows)
    occurrence_bands = _build_occurrence_bands(rows)
    span_rows = _build_span_rows(rows)
    kind_rows = _build_kind_rows(rows)
    structure = _build_structure_rows(rows)

    return {
        'period_days': days,
        'period_start': _period_start(days, timezone.localtime(now).date()),
        'period_end': now,
        'has_data': bool(rows),
        'summary': _summary(current, comparison, kind_rows),
        'current': current,
        'comparison': comparison,
        'occurrence_bands': occurrence_bands,
        'span_rows': span_rows,
        'kind_rows': kind_rows,
        'structure': structure,
        'min_bucket_sample': MIN_BUCKET_SAMPLE,
    }
