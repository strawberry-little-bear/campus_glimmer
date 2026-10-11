# -*- coding: utf-8 -*-
"""互助反馈的补充说明里，固定标签之外的话有没有被说出来。

``MutualAidFeedback`` 有两个地方记录一次互助的结果：``tags`` 是四个固定选项，
``note`` 是一段最多五百字的自由文本。运营看板上的互助反馈闭环统计了结果、来源、
标签分布和每日趋势，唯独没有读过 ``note``——它被写进数据库，然后除了后台列表页
没有任何东西打开过它。

固定标签表达的是"勾了哪个框"，自由文本表达的是"固定选项没覆盖到的那部分"。
"对方迟到了半小时"、"约定的地点进不去"、"价格和说的不一样"这些话，四个标签一个都
装不下，而它们恰恰是一次互助为什么没走成的真实原因。所以这个模块读 ``note``，
但它只做最朴素的三件事，因为再进一步就要开始猜意思，而猜意思是这个项目一贯不肯做的事。

第一件是填写率与长度。一个没人写的字段不该被分析，该被撤掉；所以先问它还活着没有。
第二件是未完成与已完成的填写率对比，这是整套统计里最有信息量的一问：如果没走成的
那些互助反而更少有人留下解释，说明失败的原因系统根本采集不到，标签也就无从改进；
如果没走成的更愿意写，说明 ``note`` 是失败诊断的真实信号源，值得为它做点什么。
第三件是高频词，只做切词与去停用词后的计数，明确标注仅供参考——它不判断语义，
不聚类，不推断主题，因为它没有能力做这些，假装有比不做更糟。

它只报告。不改标签、不新增标签、不把任何词回写成结构化字段。
"""

from collections import Counter

from django.utils import timezone

from .models import MutualAidFeedback

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一条补充说明短于这个长度就不计入高频词：一两个字的碎片切不出词，
# 只会把停用词表撑大。
MIN_NOTE_LENGTH = 6

# 高频词只报前这么多条。运营要看的是"大家都在说哪几件事"，
# 不是一份完整的词典。
TOP_TERMS = 12

# 一个词要出现这么多次才进榜单。出现一两次的词是某一次互助的细节，
# 不是一类问题。
MIN_TERM_COUNT = 2

# 常见停用词。中文没有天然分词，这里用一张手工表把最常用的虚词和
# 与本模块无关的通用词剔掉，剩下的才可能是具体在说什么。
# 虚词。滑窗取词必然会切出跨词的碎片——「约定的地点」会产出「定的」和
# 「的地」，它们在计数上和真词一样高，却什么都不说明。规则是首字或尾字落在
# 这张表里的候选词直接丢掉：中文里虚词一般不构成词的边界，真正在说事的词
# 很少以它们起头或收尾。这是滑窗方案能付出的最小代价，也是它唯一需要的补丁。
FUNCTION_CHARS = frozenset('的了着过和与及或并在被把从到对于以为其之而已则')

STOP_TERMS = frozenset({
    '的', '了', '是', '在', '我', '有', '和', '就', '不', '人', '都', '一',
    '一个', '没有', '这个', '那个', '我们', '你们', '他们', '自己', '什么',
    '怎么', '这样', '那样', '还是', '但是', '因为', '所以', '如果', '已经',
    '可以', '时候', '现在', '然后', '一下', '不是', '就是', '感觉', '有点',
    '比较', '非常', '真的', '也挺', '也有', '也很', '也没', '也不', '都没',
    '对方', '同学', '卖家', '买家', '用户', '东西', '商品', '帮忙', '帮助',
})


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _note_text(feedback):
    return (feedback.note or '').strip()


def _tokenize(text):
    """把一段中文切成候选词。

    这里刻意不用任何分词库：引入一个依赖只为了数几个词，代价和这个项目的
    其余部分不匹配。做法是切掉标点后按连续中文片段滑窗取二字与三字组合，
    再过滤停用词与虚词边界的碎片。它抓不到"迟到了半小时"里的"迟到"，但抓得到
    "迟到"——而数词这件事要的就是后者这种粒度。

    返回的是集合而不是计数器：同一个词在一段说明里说三遍，说明的是这段说了
    三遍，不是三处互助都在说它。真正要数的是"多少个不同的人提到它"。
    """
    import re

    chunks = re.findall(r'[\u4e00-\u9fff]{2,}', text)
    terms = set()
    for chunk in chunks:
        length = len(chunk)
        if length < 2:
            continue
        for size in (2, 3):
            if length < size:
                continue
            for start in range(length - size + 1):
                term = chunk[start:start + size]
                if term in STOP_TERMS:
                    continue
                # 首字或尾字是虚词的候选词是滑窗切出来的碎片，
                # 不是用户在说的东西。
                if term[0] in FUNCTION_CHARS or term[-1] in FUNCTION_CHARS:
                    continue
                # 中间夹着虚词的候选词同样是滑窗的产物：「定的地」跨越了
                # 「约定的地点」的虚词边界，「进不去」里的「不去」也是。
                # 真正在说事的词很少把虚词夹在中间。
                if any(ch in FUNCTION_CHARS for ch in term):
                    continue
                terms.add(term)
    return terms


def _length_bucket(length):
    """长度分档，固定四档，和匹配质量模块的分段一个思路。"""
    if length <= 20:
        return '20 字以内'
    if length <= 60:
        return '21-60 字'
    if length <= 150:
        return '61-150 字'
    return '150 字以上'


def _build_fill_stats(feedbacks):
    """填写率、长度分布，以及写了的那些人平均写多长。"""
    total = len(feedbacks)
    filled = [fb for fb in feedbacks if _note_text(fb)]
    lengths = [len(_note_text(fb)) for fb in filled]
    buckets = {}
    for fb in filled:
        label = _length_bucket(len(_note_text(fb)))
        entry = buckets.setdefault(label, {'label': label, 'count': 0})
        entry['count'] += 1
    order = ['20 字以内', '21-60 字', '61-150 字', '150 字以上']
    length_rows = [buckets[label] for label in order if label in buckets]
    return {
        'total': total,
        'filled_count': len(filled),
        'fill_rate': _percent(len(filled), total),
        'average_length': round(sum(lengths) / len(lengths), 1) if lengths else 0,
        'max_length': max(lengths) if lengths else 0,
        'length_rows': length_rows,
    }


def _build_outcome_contrast(feedbacks):
    """未完成与已完成，各自有多少人愿意留下解释。"""
    rows = []
    for key, label in MutualAidFeedback.OUTCOME_CHOICES:
        group = [fb for fb in feedbacks if fb.outcome == key]
        stats = _build_fill_stats(group)
        rows.append({
            'key': key,
            'label': label,
            'feedback_count': stats['total'],
            'filled_count': stats['filled_count'],
            'fill_rate': stats['fill_rate'],
            'average_length': stats['average_length'],
        })
    completed = next((row for row in rows if row['key'] == 'completed'), None)
    unresolved = next((row for row in rows if row['key'] == 'unresolved'), None)
    gap = None
    if completed and unresolved and completed['feedback_count'] and unresolved['feedback_count']:
        gap = round(unresolved['fill_rate'] - completed['fill_rate'], 1)
    return {
        'rows': rows,
        'gap_points': gap,
        # 差值小于这个数视为噪声：两条填写率之间几个百分点的距离，
        # 在几十条样本上和抛硬币差不多。
        'gap_is_meaningful': gap is not None and abs(gap) >= 8,
    }


def _build_term_rows(feedbacks):
    """高频词，只计数不解释。

    计的是 document frequency：一个词出现在多少条不同的补充说明里。滑窗取词
    一定会切出跨词的碎片——「约定的地点」能产出「定的地」「方迟到」这种东西，
    它们靠重复计数也能爬到很高的位置。但它们只会在同一段文本里自我重复，
    极少在不同的反馈里不约而同地出现，所以按反馈条数而不是按出现次数计数，
    碎片自己就沉下去了。这是滑窗方案唯一需要的补丁，也是它不需要分词库的原因。
    """
    df = Counter()
    filled = 0
    for fb in feedbacks:
        text = _note_text(fb)
        if len(text) < MIN_NOTE_LENGTH:
            continue
        filled += 1
        for term in _tokenize(text):
            df[term] += 1
    # 名次相同的词按字面排，不靠 Counter 的插入顺序：一份榜单在不同时刻
    # 给出不同的前十二名，比一份错的榜单更难用。同理，下面的查询也要显式排序。
    ranked = sorted(df.items(), key=lambda item: (-item[1], item[0]))
    rows = [
        {'term': term, 'count': count, 'share': _percent(count, filled)}
        for term, count in ranked
        if count >= MIN_TERM_COUNT
    ]
    return rows[:TOP_TERMS]


def build_mutual_aid_feedback_notes(days=30, now=None):
    """统计互助反馈补充说明的填写情况，只报告不改任何字段。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = (timezone.localtime(now) - timezone.timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    feedbacks = list(
        MutualAidFeedback.objects.filter(created_at__gte=start, created_at__lte=now)
        .select_related('demand_response', 'lost_found_lead')
        .order_by('created_at', 'id')
    )

    fill_stats = _build_fill_stats(feedbacks)
    contrast = _build_outcome_contrast(feedbacks)
    term_rows = _build_term_rows(feedbacks)

    if not fill_stats['total']:
        summary = '当前周期还没有互助反馈，补充说明需要先有反馈发生。'
    elif not fill_stats['filled_count']:
        summary = '当前周期的互助反馈都没有填写补充说明，自由文本这一段还是空的。'
    else:
        summary = (
            f"共 {fill_stats['total']} 条反馈，其中 {fill_stats['filled_count']} 条"
            f"写了补充说明（{fill_stats['fill_rate']}%），平均 {fill_stats['average_length']} 字。"
        )
        if contrast['gap_is_meaningful']:
            direction = '更愿意' if contrast['gap_points'] > 0 else '反而不如'
            summary += (
                f"未完成的反馈{direction}已完成的愿意留下解释"
                f"（相差 {abs(contrast['gap_points'])} 个百分点）。"
            )
        else:
            summary += '未完成与已完成的填写率没有明显差别。'

    return {
        'period_days': days,
        'has_data': bool(fill_stats['total']),
        'has_notes': bool(fill_stats['filled_count']),
        'summary': summary,
        'fill_stats': fill_stats,
        'outcome_contrast': contrast,
        'term_rows': term_rows,
        'min_term_count': MIN_TERM_COUNT,
        'top_terms': TOP_TERMS,
        'min_note_length': MIN_NOTE_LENGTH,
    }
