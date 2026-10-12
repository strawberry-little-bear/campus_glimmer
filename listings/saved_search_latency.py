# -*- coding: utf-8 -*-
"""关注搜索的命中时延，第一次被运营读到。

``SavedSearchMatch`` 一直在写两个时间戳：``matched_at`` 记这件商品什么时候命中这条关注条件，
``notified_at`` 记这条命中什么时候送达用户。写它们是为了让日度和周度汇总有个缓冲区——
否则一周的命中会变成几十条近乎重复的通知。写完之后，``notified_at`` 只被拿去筛「哪些还没
发出去」，``matched_at`` 只被拿去给缓冲区排序。两个字段的差值，从来没有任何一行代码算过。

运营侧现在能回答「发了多少条」「还有多少条没发」，回答不了「一条命中从产生到送达平均等了
多久」「周度节奏是不是等得太久了」「这些等太久的命中，商品是不是早就卖掉了」。而这三个问题
恰好是判断汇总节奏该不该调整的全部依据：一个用户选了周度汇总，一周后收到一条「另有 7 条未
展示」的通知，点进去三件已经下架——他不知道这是节奏问题还是自己条件太宽，运营也不知道。

这个模块把两个时间戳读出来，只报告，不改写任何一条命中。几处口径上的取舍比数字本身更要紧，
它们都在下面的注释里。

分母只收已经送达的命中。还躺在缓冲区里的命中没有送达时刻，谈不上等了多久——那是右删失样本，
和 ``return_handshake``、``handoff_code_usage`` 是同一个道理，单独数出来，不丢也不混。

等待时长一律按天分档，不按小时。一次汇总等待的时间尺度是天，报「平均等了 31 小时」对一个按
周运转的节奏没有意义，而「平均等了 1.3 天」立刻能看出日度正常、周度偏慢。

日度和周度必须分开报，而且各用自己的一套阈值。日度拖过一天是异常，周度拖过一周才值得看，
合成一个平均数会把两者的差异抹平，而那正是要动作的地方。

时延要和送达时商品是否还可交易交叉，这是这个模块真正的价值所在。一条命中等了三周才送到，
而商品第二天就卖掉了，用户收到的通知是无效的；反过来当天就送到、商品还挂一周，这条通知是
有效的。交叉之后回答的是「汇总节奏的等待有没有真的让用户错过东西」，而不是「我们发得快不快」。

商品状态只能按当前状态回看——送达之后商品仍可能被下架或卖出，历史状态没有记录。所以「送达时
已不可交易」这一栏是高估：一条发得很及时、但商品在送达之后才卖掉的命中也会被算进来。宁可高
估，因为运营看到高估的数字会去核实，看到低估的数字会直接相信。

即时节奏不算。它的 ``matched_at`` 和 ``notified_at`` 是同一笔写入的两个字段，差约等于零，
报出来是噪声。本模块只统计 ``daily`` 和 ``weekly``。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from .models import SavedSearch, SavedSearchMatch

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 只统计汇总节奏。即时节奏的命中是当场写、当场发的，两个时间戳差约等于零。
MEASURED_CADENCES = ('daily', 'weekly')

# 每个节奏自己的分档。日度和周度的等待尺度差一个量级，硬用同一套阈值会让
# 其中一档永远落在「正常」里，另一档永远落在「超长」里。
DAILY_LATENCY_BUCKETS = (
    (86400, '当天送达', '当天就汇总发出，用户基本无感'),
    (2 * 86400, '1 天内', '隔了一天，多半是任务排在半夜跑'),
    (3 * 86400, '1-3 天', '拖过了一天，需要看是不是任务漏跑'),
    (7 * 86400, '3-7 天', '日度汇总拖到快一周，等于周度了'),
    (None, '超过 7 天', '已经超过一个汇总周期，这一批基本失效'),
)

WEEKLY_LATENCY_BUCKETS = (
    (86400, '当天送达', '当天就汇总发出，用户基本无感'),
    (3 * 86400, '1-3 天', '周度节奏下很快，通常是手动补送'),
    (7 * 86400, '3-7 天', '一周内送到，符合周度的预期'),
    (14 * 86400, '7-14 天', '超过一周才送到，用户可能已经忘了这个关注'),
    (None, '超过 14 天', '跨了两个汇总周期，这一批基本失效'),
)

BUCKETS_BY_CADENCE = {
    'daily': DAILY_LATENCY_BUCKETS,
    'weekly': WEEKLY_LATENCY_BUCKETS,
}

# 一组样本至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条
# 样本上几个百分点的差距是噪声。
MIN_MEASURED_MATCHES = 12

# 待核实清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
STALE_LIST_LIMIT = 20

CADENCE_LABELS = dict(SavedSearch.FREQUENCY_CHOICES)

# 一次命中的完整轨迹，一次查完固定下来。时延分档和商品状态交叉要各读一遍，
# 按记录现算会让每条命中打两遍库，而看板是一次打开全算的。
Match = namedtuple('Match', [
    'row', 'latency_seconds', 'item_available', 'cadence', 'saved_search_name',
])


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


def _format_days(seconds):
    """等待时长一律按天说话，不足一天的说「当天」。

    这个模块报的全是天尺度的量，报「19 小时」会让运营以为是小时级的问题，
    而去查一个按天运转的定时任务。
    """
    if seconds is None:
        return '—'
    if seconds < 86400:
        return '当天'
    return f'{int(seconds // 86400)} 天'


def _mean(values):
    return sum(values) / len(values) if values else None


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _bucket_index(seconds, buckets):
    for index, (limit, _label, _hint) in enumerate(buckets):
        if limit is None or seconds < limit:
            return index
    return len(buckets) - 1


def _bucket_rows(seconds_list, buckets):
    """一份互斥分档，每一档带上它自己的口径说明。"""
    rows = [
        {'label': label, 'hint': hint, 'count': 0}
        for _limit, label, hint in buckets
    ]
    for seconds in seconds_list:
        rows[_bucket_index(seconds, buckets)]['count'] += 1
    for row in rows:
        row['share'] = _percent(row['count'], len(seconds_list))
    return rows


def _fetch_matches(*, matched_after=None, matched_before=None):
    """按显式排序取出窗口内已经送达的命中，带上关注条件、节奏和商品状态。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。

    窗口按 ``matched_at`` 取，不按 ``notified_at``。要回答的是「这段时间里产生的命中
    等了多久才送到」，所以起点是命中产生；一条上个月命中、这个月才补送到的记录，
    等的是上个月的那件事。

    商品是否可交易按当前状态取，不按送达那一刻的状态——后者没有记录。因此
    「送达时已不可交易」这一栏系统性偏高，报告里必须写清楚，见模块开头。
    """
    queryset = SavedSearchMatch.objects.filter(
        saved_search__notify_frequency__in=MEASURED_CADENCES,
        notified_at__isnull=False,
    ).select_related('saved_search', 'item')
    if matched_after is not None:
        queryset = queryset.filter(matched_at__gte=matched_after)
    if matched_before is not None:
        queryset = queryset.filter(matched_at__lte=matched_before)

    matches = []
    for row in queryset.order_by('matched_at', 'pk'):
        latency = max((row.notified_at - row.matched_at).total_seconds(), 0.0)
        matches.append(Match(
            row=row,
            latency_seconds=latency,
            item_available=row.item.is_available_now,
            cadence=row.saved_search.notify_frequency,
            saved_search_name=row.saved_search.name,
        ))
    return matches


def _cadence_stats(matches, cadence):
    """一个节奏自己的等待分布，以及送达时商品是否还可交易的交叉。"""
    subset = [m for m in matches if m.cadence == cadence]
    latencies = [m.latency_seconds for m in subset]
    buckets = BUCKETS_BY_CADENCE[cadence]
    rows = _bucket_rows(latencies, buckets)

    # 交叉按档看，不按总数看。想知道的是「等越久是不是越容易错过」，而这个问题
    # 只有逐档对照才答得出来：如果每一档的不可交易率都差不多，那等待就不是原因。
    available_count = sum(1 for m in subset if m.item_available)
    cross = []
    for index, row in enumerate(rows):
        in_bucket = [m for m in subset if _bucket_index(m.latency_seconds, buckets) == index]
        # 空档也要给出完整的键。少了 stale_count/stale_rate 两个键，模板和导出就得
        # 为「这个档有没有样本」分两套写法，而漏掉一套就是一个 KeyError。
        stale = sum(1 for m in in_bucket if not m.item_available)
        cross.append({
            'label': row['label'],
            'count': len(in_bucket),
            'stale_count': stale,
            'stale_rate': _percent(stale, len(in_bucket)) if in_bucket else None,
        })

    return {
        'cadence': cadence,
        'label': CADENCE_LABELS.get(cadence, cadence),
        'measured_count': len(subset),
        'mean_label': _format_days(_mean(latencies)),
        'median_label': _format_days(_percentile(latencies, 0.5)),
        'p90_label': _format_days(_percentile(latencies, 0.9)),
        'rows': rows,
        'available_count': available_count,
        'stale_count': len(subset) - available_count,
        'cross': cross,
        'is_small_sample': len(subset) < MIN_MEASURED_MATCHES,
    }


def _stale_rows(matches, limit=STALE_LIST_LIMIT):
    """等得最久、且送达时商品已经不可交易的命中，按等待时长倒序。

    返回 (截断后的行, 全部命中)。行数有上限，因为清单是给「现在去核实」用的；
    但上限之下的总量要一起报出来，否则模板里「完整的在导出里」这句话没有依据。
    """
    stale = [m for m in matches if not m.item_available]
    stale.sort(key=lambda m: (-m.latency_seconds, m.row.pk))
    # 截断之前的条数一起返回，模板才能说清「清单只列了前多少条」。
    return stale, [
        {
            'saved_search_name': m.saved_search_name,
            'item_title': m.row.item.title,
            'item_id': m.row.item_id,
            'cadence_label': CADENCE_LABELS.get(m.cadence, m.cadence),
            'wait_label': _format_days(m.latency_seconds),
            'wait_seconds': m.latency_seconds,
            'matched_at': m.row.matched_at,
            'notified_at': m.row.notified_at,
        }
        for m in stale[:limit]
    ]


def _summary(stats, matches, pending_count, stale_rows):
    parts = [
        f'周期内共有 {stats["daily"]["measured_count"] + stats["weekly"]["measured_count"]}'
        f'条汇总命中已经送达',
    ]
    for cadence in MEASURED_CADENCES:
        row = stats[cadence]
        if not row['measured_count']:
            parts.append(f'；{row["label"]}节奏没有已送达的命中')
            continue
        parts.append(
            f'；{row["label"]}节奏 {row["measured_count"]} 条，'
            f'中位等 {row["median_label"]}、P90 等 {row["p90_label"]}'
        )
    slowest = max(
        (stats[c] for c in MEASURED_CADENCES if stats[c]['measured_count']),
        key=lambda r: r['p90_label'],
        default=None,
    )
    if slowest is not None:
        parts.append(f'，其中 {slowest["label"]} 拖得最久')
    if stale_rows:
        parts.append(
            f'。{len(stale_rows)} 条送达时商品已经不可交易，已逐条列在下面'
            f'（按当前状态回看，送达之后才下架的也会算进来，所以这一栏偏高）'
        )
    if pending_count:
        parts.append(f'；另有 {pending_count} 条命中还等在缓冲区里，未计入任何等待分布')
    small = [stats[c] for c in MEASURED_CADENCES if stats[c]['is_small_sample'] and stats[c]['measured_count']]
    if small:
        parts.append(
            f'。{"、".join(r["label"] for r in small)}节奏的样本不足 '
            f'{MIN_MEASURED_MATCHES} 条，占比只作参考'
        )
    return ''.join(parts)


def build_saved_search_latency(days=30, now=None):
    """统计一条关注命中从产生到送达等了多久，只报告不改写任何记录。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    matches = _fetch_matches(matched_after=start, matched_before=now)
    stats = {cadence: _cadence_stats(matches, cadence) for cadence in MEASURED_CADENCES}
    stale_all, stale_rows = _stale_rows(matches)

    # 待送达的命中单独数，不计入任何等待分布：它们没有送达时刻，是右删失样本。
    pending_count = SavedSearchMatch.objects.filter(
        saved_search__notify_frequency__in=MEASURED_CADENCES,
        notified_at__isnull=True,
        matched_at__gte=start,
    ).count()

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(matches),
        'summary': _summary(stats, matches, pending_count, stale_rows),
        'daily': stats['daily'],
        'weekly': stats['weekly'],
        'stale_rows': stale_rows,
        'stale_row_limit': STALE_LIST_LIMIT,
        'stale_row_total': len(stale_all),
        'stale_count': sum(not match.item_available for match in matches),
        'pending_count': pending_count,
        'measured_total': len(matches),
        'min_measured_matches': MIN_MEASURED_MATCHES,
    }
