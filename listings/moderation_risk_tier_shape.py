# -*- coding: utf-8 -*-
"""复核分级的形状：三档复核节奏，第一次被数字读到。

``ModerationEvent.risk_tier`` 和 ``RISK_TIER_REVIEW_TARGETS`` 是站内唯一一对
「写进去了、读者却只有一个数」的规则。``chat_messages/moderation.py`` 的四条规则各带
一个档位——高风险交易 critical、微信导流和手机号 elevated、外部链接 caution——
``risk_tier_for_labels`` 取其中的最高档写进记录；``models.py`` 的
``RISK_TIER_REVIEW_TARGETS`` 再给三档各配一个复核目标：critical 4 小时、
elevated 12 小时、caution 48 小时。

而全站读这两个常量的地方只有一处：``chat_messages/views.py`` 的审核队列页，用它算
「此刻超期几条」，显示在三级复核时限那几个角标上。那是一个实时工单视图，报的是此刻
的积压，不是形状——三档各自拦下过多少、拦下来的东西复核之后是不是真的违规、复核
到底有没有按自己那一档的目标走、两套风险尺度在什么地方对不上，一个问题都没回答。

于是运营看得见「现在有几条严重风险超期」，看不见：

- 三档各自的规模和结局。caution 那条 48 小时的目标，是不是在养一批永远没人看的
  低权重线索？critical 那 4 小时，到底守住了几条？
- 复核节奏相对各自目标的样子。``moderation_rule_quality`` 已经报了绝对复核时长的
  分布，但绝对时长会把 caution 误读成积压——它的目标本身就是 48 小时，一条 caution
  记录躺 30 小时是达标的，一条 critical 记录躺 30 小时是彻底失守。
- ``risk_level``（分数 30 / 60 切）和 ``risk_tier``（最高规则档位）在什么地方对不上。
  两把尺子量的是两件事，从没被放在同一张表上看过。
- 每条规则实际产出的是哪一档。取最高档意味着一条 elevated 规则完全可能产出 critical
  事件，运营现在无法从任何一张表上看出这件事发生了多少次。

本模块把这两组数字读出来，只报告，不改写任何一条审核记录。

## 一、分母是窗口内全部记录，按 created_at 切

第一节的分母是窗口内**全部**审核记录，包含还挂着的 pending。这一节问的是「三档各自
拦下了多少、结局如何」，而「还没有结局」本身就是结局的一种，剔掉它会让每一档的规模
凭空少一块。

窗口按 ``created_at`` 切，不按 ``reviewed_at`` 切：分级是拦截那一刻写下的，要数的是
「这段时间里拦下来的内容被分成了哪几档」。``moderation_rule_quality`` 按
``reviewed_at`` 切，因为它问的是「这段时间复核掉的记录判得准不准」。两个模块的终点
不同，两张表不能直接相加。

## 二、复核节奏按各自的目标归一化，不按绝对时长

绝对时长分档在这张表上会读出假结论：caution 的目标是 48 小时，一条躺了 30 小时的
caution 记录是**达标**的；同一条 30 小时放到 critical 头上就是彻底失守。所以第二节把
每条记录的复核时长除以它那一档自己的目标，再分四档：目标内、一到两倍、两到四倍、
超过四倍。这样三档才第一次可以放在同一列里比。

只收 ``confirmed`` / ``dismissed`` 且 ``reviewed_at`` 非空的记录。``pending`` 没有
复核时刻，是右删失样本：它此刻的相对倍数还在随时间增长，报进任何一档都是一个会自己
变的数。它单独数出来——当前待复核、且已经超过各自目标的条数——不进分布。

## 三、两套尺子的分歧点不是 bug 报告

``risk_level`` 由 ``risk_score`` 切（30 / 60），``risk_tier`` 由命中的最高规则档位
决定。两者故意分开：``moderation.py`` 的注释写着，``risk_tier`` 是复核节奏，
``risk_score`` 是可解释的加权和，分开正是为了「不让一条普通链接变成 critical 事件」。

所以「分数高、档位没到严重风险」不是矛盾，那是设计：一条同时命中微信导流和手机号的
记录，分数 75 已经过高风险线，档位却停在 elevated。反过来「档位是严重风险、分数没到
高风险」也同样可能：单条高风险交易 50 分，够不着 60 的高风险线，档位却是 critical。

第三节把两个方向的不一致各数出来，**不给方向、不标红、不建议改任何一边**。它回答的
是「这两套尺度在这批数据上分开得有多远」，为的是将来讨论要不要合并时手里有数。
本模块不改分档阈值、不改权重、不把两套尺子合成一个数。

## 四、规则那一节报「参与产出」，不报独占产出

一条记录同时命中多条规则时，``risk_tier`` 取最高档。所以一条 critical 记录的
``matched_terms`` 里可能同时写着「微信导流、高风险交易」——它是两条规则共同产出的。
第四节因此按「参与」计数：一条记录出现在它命中的每一条规则底下。

这不是重复计数错误。要回答的是「高风险交易这条规则产出过多少 critical 事件」，所有
命中它的记录都是它的样本，包括那些同时命中了手机号的。反过来，elevated 规则那一行的
critical 列非零，正是「取最高档」这件事的直接证据。

## 五、不做什么

- 不改 ``_RULES``、不改权重、不改 60 / 30 的分档阈值、不改
  ``RISK_TIER_REVIEW_TARGETS``。规则和档位是发版本的代码，不是运营可以随手调的参数。
- 不自动确认或放行任何审核、不按 ``risk_tier`` 做任何自动处置。分级只影响人工排序，
  这个模块只让它被看见。
- 不读 ``content`` 正文、不输出用户名、不输出任何名单。
- 不判断规则准不准——那是 ``moderation_rule_quality`` 的事，那边按规则报误判率和
  漏判率。本模块只报分级本身的形状，以及两套尺子的分歧。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from chat_messages.models import ModerationEvent

from .borrow_risk import _rate

# 本模块按周期过滤，取值集合与看板下拉一致。
DEFAULT_DAYS = 30
PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一档的记录数下限。和其余模块同一个量级：几十条记录上几个百分点的差距是噪声。
MIN_TIER_EVENTS = 12

# 百分点差值不满此值视为噪声，不给方向。全站同一套。
NOISE_POINTS = 8

# 三档的中文名与展示顺序。顺序即紧迫程度，从最急的那一档开始读。
TIER_LABELS = (
    ('critical', '严重风险'),
    ('elevated', '脱离平台联系'),
    ('caution', '低权重线索'),
)
TIER_ORDER = tuple(key for key, _label in TIER_LABELS)
TIER_LABEL_MAP = dict(TIER_LABELS)

# 风险等级的中文名与展示顺序，按分数从低到高。
RISK_LEVEL_LABELS = (
    ('low', '低风险'),
    ('medium', '中风险'),
    ('high', '高风险'),
)
RISK_LEVEL_ORDER = tuple(key for key, _label in RISK_LEVEL_LABELS)
RISK_LEVEL_LABEL_MAP = dict(RISK_LEVEL_LABELS)

# 复核节奏分档：复核时长 ÷ 该档自己的目标。互斥，按倍数切，最后一档兜住。
# (上限倍数, 标签, 这一档说明什么)
RHYTHM_BANDS = (
    (1.0, '目标内', '在自己那档的目标时间里复核完了'),
    (2.0, '一到两倍', '超过目标，但还在两倍以内'),
    (4.0, '两到四倍', '已经拖到目标的两到四倍'),
    (None, '超过四倍', '拖过目标的四倍，这一档的节奏等于没有'),
)

# 规则表：权重与档位。两者都是 ``chat_messages/moderation.py`` 里 ``_RULES`` 的常量，
# 这里只把运营要对照的那两列摆出来，不复制正则——正则是代码，不是参数。
RULE_TABLE = (
    ('高风险交易', 50, 'critical'),
    ('微信导流', 40, 'elevated'),
    ('手机号', 35, 'elevated'),
    ('外部链接', 20, 'caution'),
)

# 规则清单最多列这么多条。规则只有四条，列全部；上限留给将来加规则的情况。
RULE_LIST_LIMIT = 12

# 表外取值的统一标签。档位和风险等级都是带 choices 的字段，正常写入只会落在已知取值
# 上；历史数据或直接写库可能落在外面，单独标出来，不静默并进任何一档。
OTHER_LABEL = '其它'

# 一条审核记录在本模块里的完整轨迹，一次查完固定下来。四张表各读一遍，按记录现算会
# 让每条记录打好几遍库，而看板是一次打开全算的。
Event = namedtuple('Event', [
    'row', 'rules', 'review_seconds', 'rhythm_ratio',
])


def _percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _period_start(days, today):
    """统计窗口的第一天零点，按项目时区解释。

    写 ``datetime.min`` 会当场报错——``combine`` 第二个参数要的是 ``time``，
    不是 ``datetime``，而 ``timezone.datetime`` re-export 的是 ``datetime``
    类本身。
    """
    start_date = today - timedelta(days=days - 1)
    return timezone.make_aware(
        timezone.datetime.combine(start_date, dt_time.min),
    )


def _format_seconds(seconds):
    """复核时长按秒、分钟、小时、天说话。

    这个模块报的是四小时到两天尺度的量。报「平均复核 9154 秒」会让运营以为这是
    秒级流程，而真正要动作的对象是「审核人力够不够」。
    """
    if seconds is None:
        return '—'
    seconds = int(round(seconds))
    if seconds < 60:
        return '%d 秒' % seconds
    if seconds < 3600:
        return '%d 分钟' % (seconds // 60)
    if seconds < 86400:
        return '%d 小时' % (seconds // 3600)
    return '%d 天' % (seconds // 86400)


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _mean(values):
    return sum(values) / len(values) if values else None


def _band_index(ratio, bands):
    """倍数落在哪一档。闭区间右端：恰好等于目标算「目标内」，不是「一到两倍」。

    踩着目标线复核完的那一条是达标的，把它推到下一档会让「目标内」这一列系统性少算
    一条，而那一列正是这一节唯一要报的数。
    """
    for index, (limit, _label, _hint) in enumerate(bands):
        if limit is None or ratio <= limit:
            return index
    return len(bands) - 1


def _split_rules(matched_terms):
    """把 `、` 连接的规则标签串拆成清单。

    空串和 None 都要挡掉：``matched_terms`` 是 ``blank=False`` 的 CharField，但历史
    上可能存在空值，拆出来的空标签不该进任何统计。
    """
    if not matched_terms:
        return []
    return [term for term in matched_terms.split('、') if term]


def _target_hours(tier):
    target = ModerationEvent.RISK_TIER_REVIEW_TARGETS.get(tier)
    if target is None:
        return None
    hours = target.total_seconds() / 3600
    return int(hours) if hours == int(hours) else round(hours, 1)


def _fetch_events(*, after, before):
    """按显式排序取出窗口内的审核记录。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的。

    窗口按 ``created_at`` 取，不按 ``reviewed_at`` 取：分级是拦截那一刻写下的，
    见模块开头第一节。
    """
    queryset = ModerationEvent.objects.filter(
        created_at__gte=after, created_at__lte=before,
    ).select_related('author', 'item')
    events = []
    for row in queryset.order_by('created_at', 'pk'):
        review_seconds = None
        ratio = None
        target = ModerationEvent.RISK_TIER_REVIEW_TARGETS.get(row.risk_tier)
        if row.reviewed_at is not None:
            review_seconds = max(
                (row.reviewed_at - row.created_at).total_seconds(), 0.0,
            )
            if target is not None:
                ratio = review_seconds / target.total_seconds()
        events.append(Event(
            row=row,
            rules=_split_rules(row.matched_terms),
            review_seconds=review_seconds,
            rhythm_ratio=ratio,
        ))
    return events


def _tier_row(key, label, group, total, target_hours):
    confirmed = sum(1 for event in group if event.row.status == 'confirmed')
    dismissed = sum(1 for event in group if event.row.status == 'dismissed')
    pending = sum(1 for event in group if event.row.status == 'pending')
    reviewed = confirmed + dismissed
    scores = [event.row.risk_score for event in group]
    return {
        'key': key,
        'label': label,
        'count': len(group),
        'share': _rate(len(group), total),
        'mean_score': round(_mean(scores), 1) if scores else None,
        'confirmed_count': confirmed,
        'dismissed_count': dismissed,
        'pending_count': pending,
        # 确认率的分母是已复核的那部分，不是整组：把 pending 摊进分母，会让一档的
        # 「确认率」随积压量漂移，而那和规则准不准没有关系。
        'confirmed_rate': _rate(confirmed, reviewed),
        'target_hours': target_hours,
        'is_small_sample': len(group) < MIN_TIER_EVENTS,
        'is_other': key not in TIER_ORDER,
    }


def _tier_rows(events):
    """三档各自的规模与结局，含 0 样本档。

    全列是有意义的：一档一条都没有，本身是结论——要么那条规则从来没命中过，要么这一档
    的目标定得没人够得着。缺行只会让人以为表格没画完。
    """
    total = len(events)
    grouped = {}
    for event in events:
        grouped.setdefault(event.row.risk_tier, []).append(event)

    rows = [
        _tier_row(
            key, label, grouped.get(key, []), total, _target_hours(key),
        )
        for key, label in TIER_LABELS
    ]
    # 档位取值不在三档之内（历史数据或直接写库），单独一行，只在真有的时候出现。
    outside = sorted(tier for tier in grouped if tier not in TIER_ORDER)
    if outside:
        stray = [event for tier in outside for event in grouped[tier]]
        rows.append(_tier_row(
            OTHER_LABEL, OTHER_LABEL, stray, total, None,
        ))
    return rows


def _rhythm_rows(events):
    """三档的复核节奏，相对各自的目标。

    分档按「复核时长 ÷ 该档目标」的倍数切，不按绝对时长切：caution 的目标本身就是
    48 小时，按绝对时长切会把达标读成积压，见模块开头第二节。

    只收已复核完且有目标的记录。``pending`` 是右删失样本，它此刻的倍数还在涨，
    不进任何一档；没有目标的档位取值同样不进——除不出一个数来。
    """
    rows = []
    for key, label in TIER_LABELS:
        group = [
            event for event in events
            if event.row.risk_tier == key and event.rhythm_ratio is not None
        ]
        seconds = [event.review_seconds for event in group]
        band_counts = [0] * len(RHYTHM_BANDS)
        for event in group:
            band_counts[_band_index(event.rhythm_ratio, RHYTHM_BANDS)] += 1
        rows.append({
            'key': key,
            'label': label,
            'target_hours': _target_hours(key),
            'reviewed_count': len(group),
            'bands': [
                {
                    'key': label_band,
                    'label': label_band,
                    'count': count,
                    'share': _rate(count, len(group)),
                    'hint': hint,
                }
                for (_limit, label_band, hint), count in zip(RHYTHM_BANDS, band_counts)
            ],
            'within_count': band_counts[0],
            'within_share': _rate(band_counts[0], len(group)),
            'median_seconds': _percentile(seconds, 0.5),
            'p90_seconds': _percentile(seconds, 0.9),
            'median_label': _format_seconds(_percentile(seconds, 0.5)),
            'p90_label': _format_seconds(_percentile(seconds, 0.9)),
            'is_small_sample': len(group) < MIN_TIER_EVENTS,
        })
    return rows


def _pending_rows(events, now):
    """当前待复核里，已经超过各自目标的那一批。

    右删失样本，不进第二节的任何分布：它们的复核时刻还不存在，此刻的超期数随时间
    增长，报进分布就等于报一个会自己变的数。

    按窗口取，不按全量取：这一节的分母和第一节一样是「窗口内拦截的记录」。窗口之前
    拦截、现在还挂着的，拦截时刻不属于这个周期，任何一节都不收它——要看全量积压，
    审核队列页那个数是全量的。
    """
    rows = []
    for key, label in TIER_LABELS:
        group = [
            event for event in events
            if event.row.risk_tier == key and event.row.status == 'pending'
        ]
        target = ModerationEvent.RISK_TIER_REVIEW_TARGETS.get(key)
        overdue = [
            event for event in group
            if target is not None and (now - event.row.created_at) >= target
        ]
        waited = [
            (now - event.row.created_at).total_seconds() for event in group
        ]
        rows.append({
            'key': key,
            'label': label,
            'target_hours': _target_hours(key),
            'pending_count': len(group),
            'overdue_count': len(overdue),
            'overdue_share': _rate(len(overdue), len(group)),
            'longest_wait_label': _format_seconds(max(waited) if waited else None),
            'is_small_sample': len(group) < MIN_TIER_EVENTS,
        })
    return rows


def _cross_table(events):
    """风险等级 × 复核分级的交叉表，含 0 样本格。

    两把尺子量的是两件事，这张表第一次把它们放进同一个坐标系。0 格要留着：
    「高风险里一条严重风险都没有」是结论，不是表格的缺口。
    """
    counts = {}
    for event in events:
        key = (event.row.risk_level, event.row.risk_tier)
        counts[key] = counts.get(key, 0) + 1

    columns = list(TIER_ORDER)
    extra_columns = sorted(tier for _level, tier in counts if tier not in columns)
    rows_order = list(RISK_LEVEL_ORDER)
    extra_rows = sorted(level for level, _tier in counts if level not in rows_order)

    def cell(level, tier):
        return counts.get((level, tier), 0)

    rows = []
    for level in rows_order + extra_rows:
        cells = [cell(level, tier) for tier in columns + extra_columns]
        rows.append({
            'key': level,
            'label': RISK_LEVEL_LABEL_MAP.get(level, level or OTHER_LABEL),
            'is_other': level not in RISK_LEVEL_ORDER,
            'cells': cells,
            'total': sum(cells),
        })
    return {
        'columns': [TIER_LABEL_MAP.get(t, t or OTHER_LABEL) for t in columns + extra_columns],
        'rows': rows,
        'column_totals': [
            sum(cell(level, tier) for level in rows_order + extra_rows)
            for tier in columns + extra_columns
        ],
        'grand_total': len(events),
    }


def _mismatch_rows(events, total):
    """两套尺度对不上的两个方向，各数一条。

    不是 bug 报告：``risk_level`` 是分数切出来的可解释加权和，``risk_tier`` 是最高
    规则档位决定的复核节奏，两者故意分开，见模块开头第三节。这里只报它们在这批数据
    上分开得有多远，不给方向、不建议合并。
    """
    aligned = sum(
        1 for event in events
        if event.row.risk_level == 'high' and event.row.risk_tier == 'critical'
    )
    high_not_critical = sum(
        1 for event in events
        if event.row.risk_level == 'high' and event.row.risk_tier != 'critical'
    )
    critical_not_high = sum(
        1 for event in events
        if event.row.risk_tier == 'critical' and event.row.risk_level != 'high'
    )
    rows = [
        {
            'key': 'aligned',
            'label': '两把尺子指向同一处',
            'count': aligned,
            'share': _rate(aligned, total),
            'is_mismatch': False,
            'note': '分数过高风险线，档位也到了严重风险',
        },
        {
            'key': 'level_high_tier_not_critical',
            'label': '分数高、档位没到严重风险',
            'count': high_not_critical,
            'share': _rate(high_not_critical, total),
            'is_mismatch': True,
            'note': '多条中档规则叠加把分数推过了 60，档位仍停在 elevated',
        },
        {
            'key': 'tier_critical_level_not_high',
            'label': '档位是严重风险、分数没到高风险',
            'count': critical_not_high,
            'share': _rate(critical_not_high, total),
            'is_mismatch': True,
            'note': '单条高风险交易 50 分够不着 60 的高风险线，档位却是 critical',
        },
    ]
    return rows


def _rule_row(name, weight, tier, hits):
    tier_counts = {key: 0 for key in TIER_ORDER}
    other = 0
    for event in hits:
        if event.row.risk_tier in tier_counts:
            tier_counts[event.row.risk_tier] += 1
        else:
            other += 1
    return {
        'rule_name': name,
        'weight': weight,
        'own_tier': tier,
        'own_tier_label': TIER_LABEL_MAP.get(tier, '未知档位'),
        'hit_count': len(hits),
        'tier_counts': [
            {
                'key': key,
                'label': TIER_LABEL_MAP[key],
                'count': tier_counts[key],
                'share': _rate(tier_counts[key], len(hits)),
            }
            for key in TIER_ORDER
        ],
        'other_count': other,
        'is_small_sample': len(hits) < MIN_TIER_EVENTS,
    }


def _rule_rows(events):
    """每条规则参与产出的事件，最终落在哪一档。

    按「参与」计数，不按独占计数：一条记录同时命中多条规则时 ``risk_tier`` 取最高档，
    它同时是那几条规则共同的产出，见模块开头第四节。所以各行相加大于总数，这是故意
    的，面板上必须写明。

    排序按自带档位从高到低、再按命中次数从多到少：这一节要读的是「最急的那条规则
    产出过什么」，不是「哪条规则最闲」。
    """
    grouped = {}
    for event in events:
        for rule in event.rules:
            grouped.setdefault(rule, []).append(event)

    rows = [
        _rule_row(name, weight, tier, grouped.get(name, []))
        for name, weight, tier in RULE_TABLE
    ]
    # 表外规则标签（规则改名或历史数据）也报出来，不静默丢掉。
    known = {name for name, _weight, _tier in RULE_TABLE}
    for name in sorted(label for label in grouped if label not in known):
        rows.append(_rule_row(name, None, '', grouped[name]))

    rows.sort(key=lambda row: (
        row['own_tier'] not in TIER_ORDER,
        TIER_ORDER.index(row['own_tier']) if row['own_tier'] in TIER_ORDER else 0,
        -row['hit_count'], row['rule_name'],
    ))
    return rows[:RULE_LIST_LIMIT]


def _summary(tier_rows, rhythm_rows, pending_rows, mismatch_rows, total):
    """一句话说清这三级复核节奏此刻的形状。"""
    if not total:
        return '周期内没有任何审核记录，复核分级的形状无从谈起。'

    parts = ['周期内共拦截 %d 条内容' % total]
    known = [row for row in tier_rows if not row['is_other']]
    parts.append('，%s' % '、'.join(
        '%s %d 条（%s%%）' % (row['label'], row['count'], row['share'])
        for row in known
    ))

    measurable = [
        row for row in rhythm_rows
        if row['reviewed_count'] >= MIN_TIER_EVENTS
    ]
    if measurable:
        worst = min(measurable, key=lambda row: row['within_share'])
        parts.append(
            '；%s的目标是 %s 小时，已复核的 %d 条里只有 %s%% 在目标内'
            % (worst['label'], worst['target_hours'],
               worst['reviewed_count'], worst['within_share'])
        )
    else:
        parts.append('；三档已复核样本都不足 %d 条，节奏占比只作参考' % MIN_TIER_EVENTS)

    overdue_total = sum(row['overdue_count'] for row in pending_rows)
    if overdue_total:
        parts.append('；当前待复核里已有 %d 条超过各自目标' % overdue_total)

    mismatch_total = sum(row['count'] for row in mismatch_rows if row['is_mismatch'])
    if mismatch_total:
        parts.append('；两套风险尺度有 %d 条对不上' % mismatch_total)
    return ''.join(parts) + '。'


def build_moderation_risk_tier_shape(days=DEFAULT_DAYS, now=None):
    """统计复核分级的构成、节奏与两套尺度的分歧，只报告不改写。"""
    allowed_days = {value for value, _label in PERIOD_CHOICES}
    if days not in allowed_days:
        days = DEFAULT_DAYS
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    events = _fetch_events(after=start, before=now)
    tier_rows = _tier_rows(events)
    rhythm_rows = _rhythm_rows(events)
    pending_rows = _pending_rows(events, now)
    cross = _cross_table(events)
    mismatch_rows = _mismatch_rows(events, len(events))
    rule_rows = _rule_rows(events)

    return {
        'days': days,
        'period_start': start,
        'period_end': now,
        'measured_count': len(events),
        'has_data': bool(events),
        'summary': _summary(
            tier_rows, rhythm_rows, pending_rows, mismatch_rows, len(events),
        ),
        'tier_rows': tier_rows,
        'rhythm_rows': rhythm_rows,
        'pending_rows': pending_rows,
        'cross': cross,
        'cross_rows': cross['rows'],
        'cross_columns': cross['columns'],
        'mismatch_rows': mismatch_rows,
        'rule_rows': rule_rows,
        'rule_row_total': len(rule_rows),
        'rule_row_limit': RULE_LIST_LIMIT,
        'rhythm_bands': [label for _limit, label, _hint in RHYTHM_BANDS],
        'pending_total': sum(row['pending_count'] for row in pending_rows),
        'overdue_total': sum(row['overdue_count'] for row in pending_rows),
        'min_tier_events': MIN_TIER_EVENTS,
        'noise_points': NOISE_POINTS,
    }
