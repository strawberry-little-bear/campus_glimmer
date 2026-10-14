# -*- coding: utf-8 -*-
"""用户自己配的「一条通知里列几件商品」，第一次被运营读到。

``SavedSearch.max_matches_per_notice`` 是站内唯一一个由用户亲手决定「一条通知里列几条
命中商品」的字段，取值 1 到 9，默认 3。它的 help_text 写着「避免一次刷屏」。日度和周度
汇总把一个周期的命中并成一条通知，条数列得太多会刷屏，列得太少又会漏——这个字段就是
用户在这个权衡里给自己选的那个位置。

写入路径只有一处，``update_saved_search_cadence`` 里 ``1 <= limit <= 9`` 的校验加保存。
读取路径只有一处，``saved_search_digest._clamp_limit``：

::

    limit = saved_search.max_matches_per_notice or 3
    return max(1, min(limit, MAX_LISTED_MATCHES))

``MAX_LISTED_MATCHES = 9`` 是发送侧的硬天花板，``_pending_matches`` 只取缓冲区最新的
20 条，``shown = matches[:_clamp_limit(saved_search)]``，
``hidden = len(matches) - len(shown)``，通知文案末尾拼一句「另有 N 条未展示」。

**十五处引用，全是写入、校验和展示，没有一处统计过它的形状。**

而 ``hidden`` 是这十五处里最该被看见的一个量：一个把条数配成 3 的用户，每周都在收到
「另有 7 条未展示」。这是他知道自己配小了，还是根本没意识到这个数字可以调？这一整类
问题至今答不上来。

这个模块把那几个字段读出来，只报告，不改写。几处口径上的取舍比数字本身更要紧。

**只报此刻缓冲区里正在被截断的规模，不报历史规模。** 被截断的命中只存在于缓冲区里：
发送完成之后 ``notified_at`` 一写，那批命中是展示的还是截断的就再也分不出来了，没有任何
字段留下痕迹。所以本模块回答的是「现在压在库里的这批命中，有多少条正在被用户自己配的
数字挡住」，而不是「历史上总共被挡掉了多少条」。后者平台答不了，如实不报。这个边界必须
在面板上写明，否则一个当前快照会被读成长期趋势。

**配置值和实际生效值之间有一处缝隙，如实点出来。** ``_clamp_limit`` 的 ``or 3`` 和
``max(1, min(limit, 9))`` 意味着字段里存 0（或越界的老数据、手工数据）的关注，实际生效
的是 3。字段是 ``PositiveSmallIntegerField``，正常写入路径有 ``1 <= limit <= 9`` 的校验
挡着产生不了 0，但库里如果有老数据，0 是可能的。把「库里存的 0」并进「默认值 3」那一档
会让九档分布失真——一个配成 0 的用户被算成配了 3，而他的实际体验确实是收到了 3 条。两种
说法都对，但回答的不是同一个问题，所以分开数。

**不报条数改过几次，不报修改时刻。** ``updated_at`` 被同一个表单里的
``notify_frequency`` 和 ``quiet_until`` 共用——改频率、填静默时间、清空静默都会刷新它。
拿它反推「条数是什么时候改的」等于编一个数据出来。这一条和 ``saved_search_quiet_window``
对 ``updated_at`` 的取舍是同一条线：那个模块因此不报静默时长，本模块因此不报修改次数。

**空字符串提交是「不改」，不是「改成默认值」。** ``update_saved_search_cadence`` 里
``if raw_limit:`` 空字符串直接跳过保存，所以「没填」和「填了同一个值」在库里长得一样。
第一节的「默认值 3」因此是一个会合点，不是一个「从没配过」的精确计数——这一点必须写在
面板上，不能让它被读成后者。

**条数与节奏只做形状描述，不给因果。** 一个人选了周度汇总又在条数里填 1，看起来自相
矛盾——周度本来就是把一周的命中并成一条。但这同样有合理的解释：他可能只想要一个「有
动静了就告诉我一声」的信号，点进去看全部。平台没有能力区分这两种动机，所以只报形状，
不把它读成「用户配错了」，更不自动去改任何人的条数。

**不按用户排名，不报任何用户名、不列任何商品名。** 关注名称可以报——那是用户给自己
条件起的名字，不是身份；商品名不报，因为那会牵出「谁在找什么」。这一条和
``saved_search_quiet_window`` 完全一致。

**不重复相邻模块的结论。** ``saved_search_latency`` 报平台造成的等待，本模块报用户自己
配的截断；``saved_search_quiet_window`` 报暂停键，本模块报条数旋钮。三件事成因相反，
不合成任何一个总分。
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

# 条数上限取自发送侧的硬天花板，这里不另立一个数：两处对「最多几条」的理解必须一致，
# 否则看板会说一条通知最多列 5 条，而发送侧一直在列 9 条。
MIN_LISTED_MATCHES = 1
MAX_LISTED_MATCHES = 9
DEFAULT_LISTED_MATCHES = 3

# 一条关注，连同它此刻配的条数、实际生效的条数和缓冲区里还没发出去的命中。用
# namedtuple 而不是 dict，是因为下面每一节都要同时读五六个字段，dict 的键名拼错要到
# 模板里才炸。
Search = namedtuple('Search', 'row configured effective buffered')

# 一条缓冲中的命中，连同它是不是已经被自己配的条数挡在外面。
BufferedMatch = namedtuple('BufferedMatch', 'row hidden')

# 一档条数配置，连同它此刻的规模和形状。默认值 3 单独标出来，因为它同时是「主动配的」
# 和「从来没配过的」两种关注的会合点。
LimitRow = namedtuple('LimitRow', 'limit label count share is_default')

# 一条正在被截断的关注，按被挡掉的条数降序。只收关注名称和数字，不收用户名和商品名。
TruncatedRow = namedtuple(
    'TruncatedRow', 'saved_search_id saved_search_name cadence_label '
    'configured buffered_count hidden_count',
)

CADENCE_LABELS = dict(SavedSearch.FREQUENCY_CHOICES)

# 一档要给方向，至少这么多条关注。和其余模块同一个量级：几十条样本上几个百分点的
# 差距是噪声。
MIN_LIMIT_SEARCHES = 12

# 截断规模要给方向，至少这么多条缓冲命中。
MIN_BUFFERED_MATCHES = 12

# 条数 × 节奏的交叉格子要给方向，至少这么多条关注。
MIN_CADENCE_CELL = 12

# 百分点差值不满这么多视为噪声，不给方向。全站同一套。
NOISE_POINTS = 8

# 正在被截断的关注清单最多列这么多条。清单是给「现在去核实」用的，不是给归档用的。
TRUNCATED_LIST_LIMIT = 20

# 缓冲区只取发送侧扫过的那 20 条。多取没有意义：第 21 条命中在发送时压根不会被看到，
# 把它算进「被挡掉」会让截断规模虚高。
MAX_SCANNED_MATCHES = 20


def _percent(part, whole):
    """占比一律返回 None 表示空分母，不返回 0。

    0 会被读成「发了，占比很小」，而这里的事实是「分母不存在」。看板模板把 None
    显示成「—」，和 0 区分开。
    """
    return round(part / whole * 100, 1) if whole else None


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。"""
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _mean(values):
    return sum(values) / len(values) if values else None


def _effective_limit(saved_search):
    """这条关注实际生效的条数，和发送侧 ``_clamp_limit`` 同一个式子。

    复制这个式子而不是 import 它，是因为 ``saved_search_digest`` 顶层 import 了
    ``create_notification`` 和 ``academic_calendar``，一个只读的统计模块不该为了读一个
    纯函数把整条发送链路拖进依赖。两处对不上的那一天，测试会红。
    """
    limit = saved_search.max_matches_per_notice or DEFAULT_LISTED_MATCHES
    return max(MIN_LISTED_MATCHES, min(limit, MAX_LISTED_MATCHES))


def _fetch_searches():
    """按显式排序取出全部关注，带上缓冲区里还没发出去的命中。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的分组，比一份错的分组更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一批」都会随机漂移。

    不按统计周期过滤。条数是关注身上一个长期存在的配置，问的是「此刻有多少条关注停在
    这个位置上」，一个上个月就配成 1、这个月仍在配成 1 的关注，恰恰是最不该被周期切掉的
    样本。缓冲区也不按周期过滤，理由同 ``saved_search_quiet_window``。
    """
    searches = []
    for row in SavedSearch.objects.select_related(
        'user', 'category', 'location',
    ).order_by('pk'):
        matches = list(
            SavedSearchMatch.objects.filter(
                saved_search=row, notified_at__isnull=True,
            ).select_related('item').order_by('matched_at', 'pk')[:MAX_SCANNED_MATCHES]
        )
        effective = _effective_limit(row)
        searches.append(Search(
            row=row,
            configured=row.max_matches_per_notice,
            effective=effective,
            buffered=[
                BufferedMatch(row=match, hidden=index >= effective)
                for index, match in enumerate(matches)
            ],
        ))
    return searches


def _limit_rows(searches):
    """1 到 9 九档的关注数分布。

    九档全列出来，包括一条关注都没有的档——漏掉空档，读者会以为这一档也是齐的。默认值 3
    单独标出来，但**不报「从没配过多少条」**：空字符串提交是「不改」，见模块开头。

    库里存的 0 和越界值单开一档，不并进任何一档：它们实际生效的是 3，但库里存的不是 3，
    并进去会让这一档同时装着两种人。
    """
    counts = {limit: 0 for limit in range(MIN_LISTED_MATCHES, MAX_LISTED_MATCHES + 1)}
    counts[0] = 0
    for search in searches:
        counts[search.configured if search.configured in counts else 0] += 1

    total = len(searches)
    rows = []
    for limit in range(MIN_LISTED_MATCHES, MAX_LISTED_MATCHES + 1):
        rows.append(LimitRow(
            limit=limit,
            label=f'{limit} 条',
            count=counts[limit],
            # 占比的分母是全部关注，不是这一档自己——拿一档除一档永远得到 100%。
            share=_percent(counts[limit], total),
            is_default=(limit == DEFAULT_LISTED_MATCHES),
        ))
    return rows


def _out_of_range_row(searches):
    """库里存的 0 或越界值：实际生效 3，但配置不是 3。"""
    stored = sum(
        1 for s in searches
        if not MIN_LISTED_MATCHES <= (s.configured or 0) <= MAX_LISTED_MATCHES
    )
    return {
        'stored_count': stored,
        'effective_limit': DEFAULT_LISTED_MATCHES,
        'share': _percent(stored, len(searches)),
        'is_small_sample': stored < MIN_LIMIT_SEARCHES,
    }


def _truncation_stats(searches):
    """被截断的规模：这个模块的核心一问。

    只收**此刻压在缓冲区里**的命中。已经发出去的命中不保留展示/截断的痕迹，历史规模
    平台答不了，如实不报。

    平均被截掉几条只对被截断过的关注求平均，不对全部关注求——拿没被截断的关注一起去平
    均，会把「配 3 条、命中 3 条」的关注算成截断了 0 条，而它压根没被截断。
    """
    buffered_total = sum(len(s.buffered) for s in searches)
    hidden_total = sum(1 for s in searches for m in s.buffered if m.hidden)
    truncated = [s for s in searches if any(m.hidden for m in s.buffered)]

    hidden_counts = [
        sum(1 for m in s.buffered if m.hidden) for s in truncated
    ]
    return {
        'buffered_count': buffered_total,
        'hidden_count': hidden_total,
        'truncated_search_count': len(truncated),
        'truncated_share': _percent(len(truncated), len(searches)),
        'hidden_share': _percent(hidden_total, buffered_total),
        'mean_hidden_label': (
            f'{_mean(hidden_counts):.1f} 条' if hidden_counts else '—'
        ),
        'max_hidden_count': max(hidden_counts, default=0),
        'is_small_sample': buffered_total < MIN_BUFFERED_MATCHES,
    }


def _truncated_rows(searches, limit=TRUNCATED_LIST_LIMIT):
    """正在被截断的关注清单，按被挡掉的条数降序。

    只列关注名称、节奏、配了几条、缓冲区有几条、被挡掉几条。不列用户名、不列商品名——
    关注名称是用户给自己条件起的名字，商品名会牵出「谁在找什么」。
    """
    rows = []
    for search in searches:
        hidden_count = sum(1 for m in search.buffered if m.hidden)
        if not hidden_count:
            continue
        rows.append(TruncatedRow(
            saved_search_id=search.row.pk,
            saved_search_name=search.row.name,
            cadence_label=CADENCE_LABELS.get(
                search.row.notify_frequency, search.row.notify_frequency,
            ),
            configured=search.configured,
            buffered_count=len(search.buffered),
            hidden_count=hidden_count,
        ))
    rows.sort(key=lambda r: (-r.hidden_count, r.saved_search_id))
    return rows[:limit]


def _cadence_rows(searches):
    """条数配置与节奏的交叉：只做形状描述，不给因果。

    每一格报关注数、这一格的中位条数、以及这一格正被截断的关注占比。中位条数比平均条数
    稳——九档里一个配 9 的关注会把平均值抬起来，而这一节要看的正是「普遍配在哪一档」。

    不报「该配多少条」的建议，也不把任何一格读成「配错了」。
    """
    rows = []
    for cadence, label in SavedSearch.FREQUENCY_CHOICES:
        subset = [s for s in searches if s.row.notify_frequency == cadence]
        limits = sorted(s.effective for s in subset)
        median = limits[len(limits) // 2] if limits else None
        truncated = sum(1 for s in subset if any(m.hidden for m in s.buffered))
        rows.append({
            'cadence': cadence,
            'label': label,
            'search_count': len(subset),
            'median_limit': median,
            'mean_limit_label': (
                f'{_mean(limits):.1f} 条' if limits else '—'
            ),
            'truncated_count': truncated,
            'truncated_share': _percent(truncated, len(subset)),
            'is_small_sample': len(subset) < MIN_CADENCE_CELL,
        })
    return rows


def _summary(limit_rows, out_of_range, truncation, cadence_rows):
    """一句话说清这个周期里条数配置的整体形状。"""
    total = sum(row.count for row in limit_rows) + out_of_range['stored_count']
    if not total:
        return '当前没有任何关注搜索，条数配置无从谈起。'

    default_row = next(r for r in limit_rows if r.is_default)
    parts = [
        f'{total} 条关注里 {default_row.count} 条停在默认的 3 条'
        f'（{default_row.share}%）' if default_row.share is not None
        else f'{total} 条关注里 {default_row.count} 条停在默认的 3 条',
    ]
    if out_of_range['stored_count']:
        parts.append(
            f'{out_of_range["stored_count"]} 条库里存的不是 1 到 9，'
            f'实际按 {out_of_range["effective_limit"]} 条生效'
        )
    if truncation['buffered_count']:
        parts.append(
            f'缓冲区压着 {truncation["buffered_count"]} 条命中，'
            f'其中 {truncation["hidden_count"]} 条正被用户自己配的条数挡住'
        )
    else:
        parts.append('缓冲区此刻是空的，截断规模无从谈起')
    small = [r for r in cadence_rows if r['is_small_sample'] and r['search_count']]
    if small:
        parts.append(
            f'{"、".join(r["label"] for r in small)}节奏的关注不足 '
            f'{MIN_CADENCE_CELL} 条，交叉只作参考'
        )
    return '；'.join(parts) + '。'


def build_saved_search_limit_shape(days=30, now=None):
    """统计用户自己配的单次提醒条数的形状与被截断的规模，只报告不改写。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    searches = _fetch_searches()
    limit_rows = _limit_rows(searches)
    out_of_range = _out_of_range_row(searches)
    truncation = _truncation_stats(searches)
    truncated_rows = _truncated_rows(searches)
    cadence_rows = _cadence_rows(searches)

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(searches),
        'summary': _summary(limit_rows, out_of_range, truncation, cadence_rows),
        'search_count': len(searches),
        'limit_rows': limit_rows,
        'default_limit': DEFAULT_LISTED_MATCHES,
        'out_of_range': out_of_range,
        'truncation': truncation,
        'truncated_rows': truncated_rows,
        'truncated_row_limit': TRUNCATED_LIST_LIMIT,
        'truncated_row_total': sum(
            1 for s in searches if any(m.hidden for m in s.buffered)
        ),
        'cadence_rows': cadence_rows,
        'min_limit_searches': MIN_LIMIT_SEARCHES,
        'min_buffered_matches': MIN_BUFFERED_MATCHES,
        'min_cadence_cell': MIN_CADENCE_CELL,
        'noise_points': NOISE_POINTS,
    }
