# -*- coding: utf-8 -*-
"""看一条否决后来还在不在被人认着，以及那个词对有没有自己回来。

`search_rejection_stats` 把否决这件事第一次记了下来，它回答「运营为什么
否掉它们」。但它只看一个周期内的快照：三月的否决和九月再次出现的同一个
词对，在它眼里是两个互不相干的数字。于是有一个问题现在答不了——**运营的
否决，后来有没有被自己推翻？**

这个词组有两层，本模块都报，但分开报。

第一层是运营自己改主意。一条否决有两种可观测的终态：仍然生效
（`superseded_at` 为空）、被确认覆盖（词对后来进了同义词表）。第三种终态
是被撤销删除，行不在了，**读不到**——撤销的动作只写进消息日志，不落任何
表。所以本模块不为「已撤销」编一个数，撤销率只由前两桶构成，并在面板上
把这一点写明。硬给一个 0 出来，等于让一个看不见的桶看起来真的没有事。

前两种方向相反：仍然生效是「当初的判断现在仍作数」，被覆盖是「当初否错
了，它其实是个等价关系」。合成一个「撤销率」会把两个相反的事实搅成一个
数字，所以两个数分开报，同时给一个合计。

第二层是数据自己回来了。否决仍然生效，但那个词对的改口在此后又反复出
现。这个数没有字段可读，只能从 `SearchQuery` 的改口日志里重新挖一遍，挖
的规则和 `search_rejection_stats` 用的是同一条：同一个用户、间隔不超过
`MAX_REWRITE_GAP`、前一次无结果、后一次有结果。复用同一条规则是必须的，
否则「否决前出现了 8 次」和「否决后出现了 12 次」会是两把不同的尺子量出
来的，摆在一张表里也没有意义。

三条边界守住这个读数：

**回头次数不能跨失效继续累加。** 一条已被覆盖的否决不再挡任何候选，它之
后的出现次数属于一个已经在扩大搜索的词对，不属于一条仍在生效的判断。所
以回头的统计区间是 `[否决时刻, 失效时刻)`，仍然生效的数到现在。

**回头只报数，不进任何门槛判断。** 回头是「数据还在不在」，不是「证据够
不够」。一个回头 30 次的词对仍然是运营当初否掉它的那个理由——除非它被覆
盖，否则它一天就不该出现在候选里。它也不用来建议调门槛，那是
`search_threshold_feedback` 的地盘。

**覆盖本身不是错。** 一条否决被确认覆盖，说明运营在更充分的证据下改变了
主意，这是机制在正常工作。模块报这个数是为了让「判断有多稳」可读，不是
为了给运营打分。

本模块只报告。不改任何否决记录、不撤销任何否决、不恢复任何候选、不动
`MIN_OCCURRENCES`、不碰门槛建议，也不读备注字段——运营写的每一条备注都
只是备注。
"""

from datetime import timedelta

from django.utils import timezone

from .models import SearchSynonym, SearchSynonymRejection
from .search_rewrites import MAX_REWRITE_GAP, _normalize
from .search_rejection_stats import _reason_label, _rewrite_rows

# 回头次数的统计从最早一条否决的时刻往回看多久。默认和否决面板同一个窗
# 口，两个面板因此描述同一段日志；口径不同会让「回头了几次」对不上。
DEFAULT_DAYS = 180

# 一条否决要回头多少次才值得单独说。回头是稀疏事件，一两次回头既可能是
# 真的反复，也可能是同一个学生换了三次说法；三次以上才开始像一个反复出
# 现的说法而不是一次手误。
MIN_RECURRENCES = 3

# 被覆盖的比例超过这个数，说明否决这个动作本身需要回头看一眼产品设计。
# 这不是给运营打分，是一个该停下来检查判据的信号。
REVERSED_SHARE_LIMIT = 40

# 被覆盖率要多少条样本才说。低于这个数，一条被覆盖的否决就是 50% 的推翻
# 率——一个百分号和一个百分号加一个百分号不是同一个量级的东西。
MIN_JUDGED_REJECTIONS = 5

# 一条否决的两种可观测终态。第三种「已撤销删除」不在表里，见模块 docstring。
REVERSAL_KINDS = (
    ('standing', '仍然生效'),
    ('superseded', '被确认覆盖'),
)

REVERSAL_LABELS = dict(REVERSAL_KINDS)


def _rewrite_occurrences(*, start, end):
    """把一段日志里的改口按词对汇总，规则与挖掘完全一致。

    链只走一次，所有词对共用。这里返回的是每一次改口的时刻而不是一个合计，
    因为每一条否决的回头区间起点不同——否决时刻之后的才算这一条的回头。
    只在最后求和的模块会 3 月的否决背上 9 月的账。

    同一条链是全站最贵的一段内存计算，而它被两个面板共享，所以它必须读同
    一份 `_rewrite_rows`、用同一条间隔上限。
    """
    moments = {}
    previous = None
    for row in _rewrite_rows(start=start, now=end):
        if previous is None or previous['user_id'] != row['user_id']:
            previous = row
            continue
        gap = row['created_at'] - previous['created_at']
        if gap > MAX_REWRITE_GAP:
            previous = row
            continue
        source = _normalize(previous['query'])
        target = _normalize(row['query'])
        if (
            source and target and source != target
            and previous['result_count'] == 0 and row['result_count'] > 0
        ):
            moments.setdefault((source, target), []).append(row['created_at'])
        previous = row
    return moments


def _synonym_lookup():
    """已确认的同义词词对，两个方向都查。

    一条否决被确认覆盖的判据是「这个词对现在在同义词表里」。两个方向都要
    查：否决记的是「移动电源 → 充电宝」，而同义词可能记的是反方向。
    """
    pairs = set()
    for keyword, synonym in SearchSynonym.objects.values_list('keyword', 'synonym'):
        left, right = _normalize(keyword), _normalize(synonym)
        if left and right:
            pairs.add((left, right))
            pairs.add((right, left))
    return pairs


def _count_between(moments, start, end):
    """一段闭开区间内的改口次数，以及其中最后一次的时刻。"""
    inside = [moment for moment in moments if start <= moment < end]
    return len(inside), (max(inside) if inside else None)


def _rejection_rows(days, *, now, synonyms):
    """每一条窗口内的否决，连同它在生效期间回头了多少次。"""
    cutoff = now - timedelta(days=days)
    rejections = list(
        SearchSynonymRejection.objects.filter(
            created_at__gte=cutoff, created_at__lte=now,
        ).values_list(
            'source', 'target', 'reason', 'created_at', 'superseded_at',
        )
    )
    if not rejections:
        return []

    # 整段日志只走一次：最早一条否决的时刻之前不需要看。
    earliest = min(row[3] for row in rejections)
    moments = _rewrite_occurrences(start=earliest, end=now)

    rows = []
    for source, target, reason, created_at, superseded_at in rejections:
        left, right = _normalize(source), _normalize(target)
        if not left or not right:
            continue
        key = (left, right)
        standing = superseded_at is None
        # 回头只数到失效那一刻为止；仍然生效的数到现在。失效之后这条否决
        # 不再挡任何候选，那段时间的改口不属于一条仍在生效的判断。
        end = superseded_at or now
        recurrences, last_seen = _count_between(moments.get(key, ()), created_at, end)
        rows.append({
            'source': left,
            'target': right,
            'reason': reason,
            'reason_label': _reason_label(reason),
            'created_at': created_at,
            'superseded_at': superseded_at,
            'kind': 'standing' if standing else 'superseded',
            'kind_label': REVERSAL_LABELS['standing' if standing else 'superseded'],
            'is_synonym_now': key in synonyms,
            'recurrences': recurrences,
            'has_recurred': recurrences >= MIN_RECURRENCES,
            'last_seen': last_seen,
        })
    return rows


def _summary(rows, judged, reversed_count):
    if not rows:
        return '统计周期内还没有否决记录，回头与被推翻都需要先有否决存在。'
    standing = sum(1 for row in rows if row['kind'] == 'standing')
    superseded = sum(1 for row in rows if row['kind'] == 'superseded')
    parts = [
        f"周期内共 {len(rows)} 条否决，{standing} 条仍然生效，"
        f"{superseded} 条被确认覆盖。"
    ]
    if judged >= MIN_JUDGED_REJECTIONS:
        share = round(reversed_count / judged * 100, 1)
        if share >= REVERSED_SHARE_LIMIT:
            parts.append(
                f"其中 {reversed_count} 条后来被自己推翻（占 {share}%），"
                f"否决这个动作本身值得回头看一眼。"
            )
        else:
            parts.append(
                f"其中 {reversed_count} 条后来被自己推翻（占 {share}%），"
                f"判断整体是稳的。"
            )
    else:
        parts.append(
            f"样本不足 {MIN_JUDGED_REJECTIONS} 条，暂不判断推翻率。"
        )
    recurred = [row for row in rows if row['has_recurred']]
    if recurred:
        parts.append(
            f"有 {len(recurred)} 个词对在否决之后又反复出现，"
            f"最多的回来了 {max(row['recurrences'] for row in recurred)} 次。"
        )
    return ''.join(parts)


def build_search_rejection_recurrence(days=DEFAULT_DAYS, *, now=None):
    """看一条否决后来还在不在被人认着，以及那个词对有没有自己回来。"""
    now = now or timezone.now()
    synonyms = _synonym_lookup()
    rows = _rejection_rows(days, now=now, synonyms=synonyms)

    standing = sum(1 for row in rows if row['kind'] == 'standing')
    superseded = sum(1 for row in rows if row['kind'] == 'superseded')
    judged = standing + superseded
    recurred = [row for row in rows if row['has_recurred']]

    # 会自己回来的排最前：那是唯一一条运营现在还能有所动作的线索。仍然生
    # 效但没回头的排在后面——它已经不需要任何人再做什么。
    rows.sort(key=lambda row: (
        0 if row['has_recurred'] else 1,
        0 if row['kind'] == 'standing' else 1,
        -row['recurrences'],
        row['source'],
    ))

    return {
        'period_days': days,
        'min_recurrences': MIN_RECURRENCES,
        'min_judged_rejections': MIN_JUDGED_REJECTIONS,
        'reversed_share_limit': REVERSED_SHARE_LIMIT,
        'rows': rows,
        'total_count': len(rows),
        'standing_count': standing,
        'superseded_count': superseded,
        'recurred_count': len(recurred),
        'recurrence_total': sum(row['recurrences'] for row in rows),
        'reversed_count': superseded,
        # 样本不够时推翻率是 None 而不是 0。一条被覆盖的否决落在三两条样本
        # 上是 50%，落在五条以下是噪声——一个百分号和一个百分号加一个百分
        # 号不是同一个量级的东西，报 0 会让「没人推翻自己」和「样本太少说不
        # 上」看起来一模一样。数字照实给，方向不给。
        'is_measurable': judged >= MIN_JUDGED_REJECTIONS,
        'reversed_share': (
            round(superseded / judged * 100, 1) if judged >= MIN_JUDGED_REJECTIONS else None
        ),
        'is_reversed': (
            judged >= MIN_JUDGED_REJECTIONS
            and superseded / judged * 100 >= REVERSED_SHARE_LIMIT
        ),
        'summary': _summary(rows, judged, superseded),
        'has_data': bool(rows),
    }
