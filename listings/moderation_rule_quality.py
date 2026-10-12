# -*- coding: utf-8 -*-
"""内容审核队列的规则质量，第一次被运营读到。

`chat_messages.ModerationEvent` 是站内唯一一条「平台自己写分数、自己排序、却从来
不知道自己准不准」的队列。`chat_messages/moderation.py` 的四条规则各带一个权重
——微信导流 40、外部链接 20、手机号 35、高风险交易 50——命中后相加、封顶 100，
再按 60 / 30 切成 high / medium / low。

这张分数表有三个字段一直在被写，读路径却只有排序和筛选：

- `risk_score`——唯一的读路径是 `models.py` 的 `ordering` 和 `Index`。**没有任何
  一行代码拿它和另一个数比过大小，没有人算过它的分布。**
- `matched_terms`——读路径是 `admin.py` 的 `search_fields` 和审核队列页的一个
  `matched_terms__icontains` 搜索框。**没有人按规则拆开统计过。**
- `reviewed_at`——读路径只有 `admin.py` 的 `list_filter` 和 `readonly_fields`。
  **`reviewed_at - created_at` 这个差，从来没有任何一行代码算过。**

而 `governance_sla.py` 已经为另外四条队列（商品举报、交易争议、交付预约异常、
借用逾期催收）建好了统一的处理时效视图，**唯独漏了内容审核这一条**。

于是运营侧现在能回答「有多少高风险审核在等」（`pending_high_count` 早就上了看板），
回答不了「规则说高风险的，复核之后真的是高风险吗」。而这套规则全是关键词匹配：
「外部链接」那条只匹配 `https?://` 或 `www.`，一条正常分享课程资料链接的留言会被
拦下来，`risk_score` 记 20，`risk_level` 记 low，躺在队列里和真的导流内容排在一处。

唯一能验证这套规则的一批数据——复核人的 `confirmed` / `dismissed` 决定——就在同一
张表里，从来没有人把它和 `risk_score` 对照过。

这个模块把它们读出来，只报告，不改写任何一条审核记录。几处口径上的取舍比数字本身
更要紧，它们都在下面的注释里。

分母只收已经复核完的记录。`confirmed` 和 `dismissed` 都带 `reviewed_at`，有起止
两点；`pending` 没有复核时刻，是右删失样本，单独数出来，不丢也不混。

按规则拆开，不按风险等级拆开。`matched_terms` 是 `、` 连接的标签串，一条记录可以
同时命中多条规则，所以一条记录会同时出现在多个规则组里——这是故意的：要回答的是
「微信导流这条规则准不准」，所有命中它的记录都是它的样本，包括那些同时命中了手机号
的。按风险等级拆反而看不出问题，一条 100 分的记录分不出是哪条规则起的作用。

误判率和漏判率分开报，不合成一个「准确率」。两个数的分母不一样，合成一个数会把两个
问题压成一个说不清的数，而它们指向的动作完全相反：误判率高该去收紧规则，漏判率高
该去补规则。

权重只报分布，不重新拟合。记录里只有相加之后的分数，一条 55 分的记录可能是
40 + 20 减 5，也可能是 50 + 20 减 15，从分数反推不出权重组合。所以分数只当参照：
它回答「高分的那一档是不是真的更容易确认违规」，那是它唯一能回答的问题。

本模块不改 `_RULES`、不改权重、不改 60 / 30 的分档阈值、不自动确认或放行任何审核、
不按 `risk_score` 做任何自动放行、不读 `content` 正文。规则是发版本的代码，不是
运营可以随手调的参数；而本模块只读。
"""

from collections import namedtuple
from datetime import time as dt_time, timedelta

from django.utils import timezone

from chat_messages.models import ModerationEvent

PERIOD_CHOICES = (
    (7, '最近 7 天'),
    (30, '最近 30 天'),
    (90, '最近 90 天'),
    (365, '最近 1 年'),
)

# 一组样本至少要这么多条，占比和对照才给方向。和其余模块同一个量级：几十条
# 样本上几个百分点的差距是噪声。
MIN_MEASURED_EVENTS = 12

# 一条规则的样本下限。规则是命中即入组，一条被拦下来的内容常常同时命中两三条，
# 所以单条规则的样本通常比总数少不了太多；但「外部链接」这类窄规则在内容少的时候
# 会掉到很低，低于这个数就不给方向。
MIN_RULE_EVENTS = 8

# 误判率高过这个百分点才标黄。阈值和其余模块的噪声线同一个量级。
FALSE_POSITIVE_POINTS = 15

# 漏判率高过这个百分点才标黄。漏判的代价比误判高：误判拦住的是正常内容，漏判放过去
# 的是真的导流和诈骗，所以它的门槛比误判低。
FALSE_NEGATIVE_POINTS = 10

# 规则清单最多列这么多条。规则只有四条，列全部；上限留给将来加规则的情况。
RULE_LIST_LIMIT = 12

# 复核清单最多列这么多条。清单是给「现在去复核」用的，不是给归档用的。
REVIEW_LIST_LIMIT = 20

# 规则权重表。写在这里是为了报「这条规则的权重是多少」——权重是 `_RULES` 里的常量，
# 模块不复制它的正则，只把运营要对照的那个数摆出来。
RULE_WEIGHTS = {
    '微信导流': 40,
    '外部链接': 20,
    '手机号': 35,
    '高风险交易': 50,
}

# 风险等级的中文名。`risk_level` 只有三个取值，写死比反查 choices 更直白。
RISK_LEVEL_LABELS = {
    'low': '低风险',
    'medium': '中风险',
    'high': '高风险',
}

# 渠道的中文名。
CHANNEL_LABELS = {
    'comment': '商品留言',
    'private_message': '私信',
}

# 复核时长分档。互斥，左闭右开，按分钟和小时切。最后一档是尾巴：隔了一天以上才复核
# 的，多半是审核人力没跟上，那正是要动作的一批。
REVIEW_BUCKETS = (
    (600, '十分钟内', '拦下就复核了，审核人力跟得上'),
    (3600, '十分钟到一小时', '当天就看完了，是正常的复核节奏'),
    (86400, '一小时到一天', '隔了几个小时，可能是在忙'),
    (None, '超过一天', '隔了一天以上，审核队列积压了'),
)

# 分数段分档。互斥，按 `risk_score` 切。分档是为了回答「高分的档是不是真的更容易
# 确认违规」——那是这个字段唯一能回答的问题。不按单分切：一条 20 分和一条 21 分的
# 记录没有区别，而运营要看的是档位之间的差别。
SCORE_BUCKETS = (
    (0, 20, '低分档（0-19）', '只有外部链接这类轻规则命中'),
    (20, 40, '二十分档（20-39）', '单条中权重规则，或轻规则叠加'),
    (40, 60, '四十分档（40-59）', '两条中权重规则叠加'),
    (60, 101, '六十分档（60 及以上）', '跨过高风险线，通常是三条规则同时命中'),
)

# 一条审核记录的完整轨迹，一次查完固定下来。规则质量、分数分布、渠道分组各读一遍，
# 按记录现算会让每条记录打好几遍库，而看板是一次打开全算的。
Event = namedtuple('Event', [
    'row', 'rules', 'review_seconds',
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


def _format_elapsed(seconds):
    """复核时长按分钟和小时说话，不足十分钟的说「十分钟内」。

    这个模块报的是分钟到小时尺度的量。报「平均复核 2 小时 13 分」会让运营以为这是
    小时级的流程，而真正要动作的对象是「审核人力够不够」。
    """
    if seconds is None:
        return '—'
    if seconds < 600:
        return '十分钟内'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分钟'
    if seconds < 86400:
        return f'{int(seconds // 3600)} 小时'
    return f'{int(seconds // 86400)} 天'


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


def _score_bucket_index(score):
    for index, (low, high, _label, _hint) in enumerate(SCORE_BUCKETS):
        if low <= score < high:
            return index
    return len(SCORE_BUCKETS) - 1


def _split_rules(matched_terms):
    """把 `、` 连接的规则标签串拆成清单。

    空串和 None 都要挡掉：`matched_terms` 是 `blank=False` 的 CharField，但历史上
    可能存在空值，拆出来的空标签不该进任何统计。
    """
    if not matched_terms:
        return []
    return [term for term in matched_terms.split('、') if term]


def _fetch_events(*, reviewed_after=None, reviewed_before=None):
    """按显式排序取出窗口内已经复核完的审核记录。

    显式排序不是洁癖：同一份数据在不同时刻给出不同的榜单，比一份错的榜单更难用。
    查询没有 order_by 时数据库的返回顺序是不确定的，任何「第一名」都会随机漂移。

    窗口按 ``reviewed_at`` 取，不按 ``created_at``。要回答的是「这段时间里复核掉的
    审核，规则判得准不准」，所以终点是复核；一条上周才拦截、这周才复核的记录，
    验的是上周的那批规则命中。

    只收 `confirmed` 和 `dismissed` 两种已经复核完的状态。`pending` 没有复核时刻，
    谈不上规则准不准——它是右删失样本，由调用方单独数出来。
    """
    queryset = ModerationEvent.objects.filter(
        status__in=('confirmed', 'dismissed'),
        reviewed_at__isnull=False,
    ).select_related('author', 'item')
    if reviewed_after is not None:
        queryset = queryset.filter(reviewed_at__gte=reviewed_after)
    if reviewed_before is not None:
        queryset = queryset.filter(reviewed_at__lte=reviewed_before)

    events = []
    for row in queryset.order_by('reviewed_at', 'pk'):
        review_seconds = max(
            (row.reviewed_at - row.created_at).total_seconds(), 0.0,
        )
        events.append(Event(
            row=row,
            rules=_split_rules(row.matched_terms),
            review_seconds=review_seconds,
        ))
    return events


def _rule_rows(events):
    """按规则拆开的质量，误判率高的排在前面。

    一条记录会同时出现在多个规则组里——这是故意的。要回答的是「微信导流这条规则准不
    准」，所有命中它的记录都是它的样本，包括那些同时命中了手机号的。按风险等级拆反而
    看不出问题：一条 100 分的记录分不出是哪条规则起的作用。

    误判率是 `dismissed` 占本规则已复核记录的比例——规则说有问题，复核人说没问题。
    漏判率是另一件事：本规则没有命中、但复核之后 `confirmed` 的比例——规则说没问题，
    复核人说有问题。两个数的分母不一样，合成一个「准确率」会把两个问题压成一个说不清
    的数，而它们指向的动作完全相反：误判率高该去收紧规则，漏判率高该去补规则。
    """
    # 全部已复核记录里，确认违规的那些。漏判率的分母是「本规则没命中」的那批，
    # 分子是这批里被确认违规的。
    confirmed_total = sum(1 for event in events if event.row.status == 'confirmed')

    grouped = {}
    for event in events:
        for rule in event.rules:
            grouped.setdefault(rule, []).append(event)

    rows = []
    for rule in RULE_WEIGHTS:
        hits = grouped.get(rule, [])
        if not hits:
            # 这条规则这个周期一条都没命中。报出来是有意义的：一条从来没命中过的
            # 规则，可能已经失效了，也可能只是这段时间没人那么写。
            rows.append({
                'rule_name': rule,
                'weight': RULE_WEIGHTS[rule],
                'hit_count': 0,
                'confirmed_count': 0,
                'dismissed_count': 0,
                'false_positive_rate': 0,
                'is_small_sample': True,
                'is_flagged': False,
                'never_hit': True,
            })
            continue
        dismissed = sum(1 for event in hits if event.row.status == 'dismissed')
        confirmed = sum(1 for event in hits if event.row.status == 'confirmed')
        false_positive_rate = _percent(dismissed, len(hits))
        rows.append({
            'rule_name': rule,
            'weight': RULE_WEIGHTS[rule],
            'hit_count': len(hits),
            'confirmed_count': confirmed,
            'dismissed_count': dismissed,
            'false_positive_rate': false_positive_rate,
            'is_small_sample': len(hits) < MIN_RULE_EVENTS,
            'is_flagged': (
                not (len(hits) < MIN_RULE_EVENTS)
                and false_positive_rate >= FALSE_POSITIVE_POINTS
            ),
            'never_hit': False,
        })

    # 漏判：本规则没有命中、但复核之后确认违规的记录。分母是全部已复核记录减去命中
    # 本规则的那些——同一条记录可能命中多条规则，所以按记录去重，不按（规则，记录）
    # 对计数。
    for row in rows:
        if row['never_hit']:
            row['missed_count'] = 0
            row['false_negative_rate'] = 0
            continue
        hit_ids = {event.row.pk for event in grouped.get(row['rule_name'], [])}
        missed = [
            event for event in events
            if event.row.pk not in hit_ids and event.row.status == 'confirmed'
        ]
        row['missed_count'] = len(missed)
        row['false_negative_rate'] = _percent(len(missed), len(events) - len(hit_ids))

    rows.sort(key=lambda row: (
        row['never_hit'], -row['false_positive_rate'], -row['hit_count'],
    ))
    return rows


def _score_rows(events):
    """按分数段分的复核结果，只回答「高分的档是不是真的更容易确认违规」。

    权重只报分布，不重新拟合：记录里只有相加之后的分数，一条 55 分的记录可能是
    40 + 20 减 5，也可能是 50 + 20 减 15，从分数反推不出权重组合。所以分数只当参照。
    """
    rows = [
        {'label': label, 'hint': hint, 'count': 0, 'confirmed': 0, 'dismissed': 0}
        for _low, _high, label, hint in SCORE_BUCKETS
    ]
    for event in events:
        index = _score_bucket_index(event.row.risk_score)
        rows[index]['count'] += 1
        if event.row.status == 'confirmed':
            rows[index]['confirmed'] += 1
        else:
            rows[index]['dismissed'] += 1
    for row in rows:
        row['confirmed_rate'] = _percent(row['confirmed'], row['count'])
        row['is_small_sample'] = row['count'] < MIN_RULE_EVENTS
    return rows


def _channel_rows(events):
    """按内容渠道分组的复核结果，留言和私信分开报。

    用户在这两条渠道上写东西的心态完全不同：留言是公开的、写给所有来看商品的人；
    私信是一对一的、写给另一个同学的。混在一起报会让两边都被稀释。
    """
    grouped = {}
    for event in events:
        grouped.setdefault(event.row.channel, []).append(event)

    rows = []
    for channel in CHANNEL_LABELS:
        group = grouped.get(channel, [])
        confirmed = sum(1 for event in group if event.row.status == 'confirmed')
        dismissed = sum(1 for event in group if event.row.status == 'dismissed')
        rows.append({
            'channel': channel,
            'channel_label': CHANNEL_LABELS[channel],
            'count': len(group),
            'confirmed_count': confirmed,
            'dismissed_count': dismissed,
            'false_positive_rate': _percent(dismissed, len(group)),
            'is_small_sample': len(group) < MIN_RULE_EVENTS,
        })
    rows.sort(key=lambda row: (-row['count'], row['channel']))
    return rows


def _missed_rows(events, limit=REVIEW_LIST_LIMIT):
    """复核之后确认违规、但只命中了轻规则的记录。

    判据是「确认违规、且命中的规则里没有高风险交易」。高风险交易那一条的权重是 50，
    单它一条就能跨过 60 的高风险线；确认违规却只命中外部链接或手机号的记录，说明
    真正该拦的东西没有对应的规则——那是要补规则的对象。
    """
    missed = [
        event for event in events
        if event.row.status == 'confirmed' and '高风险交易' not in event.rules
    ]
    missed.sort(key=lambda event: (-event.row.risk_score, event.row.pk))
    rows = [
        {
            'event_id': event.row.pk,
            'channel_label': CHANNEL_LABELS.get(event.row.channel, event.row.channel),
            'risk_level_label': RISK_LEVEL_LABELS.get(
                event.row.risk_level, event.row.risk_level,
            ),
            'risk_score': event.row.risk_score,
            'rules': '、'.join(event.rules) or '无',
            'review_label': _format_elapsed(event.review_seconds),
            'created_at': event.row.created_at,
            'reviewed_at': event.row.reviewed_at,
        }
        for event in missed[:limit]
    ]
    return rows, len(missed)


def _summary(stats, rule_rows, score_rows, review_rows, pending_count):
    parts = [
        f'周期内共有 {stats["measured_count"]} 条审核记录被复核完'
    ]
    parts.append(
        f'，其中 {stats["confirmed_count"]} 条确认违规、'
        f'{stats["dismissed_count"]} 条误判放行'
    )
    flagged = [row for row in rule_rows if row['is_flagged']]
    if flagged:
        worst = flagged[0]
        parts.append(
            f'；「{worst["rule_name"]}」规则误判率 {worst["false_positive_rate"]}%，'
            f'最该先看'
        )
    if review_rows:
        parts.append(f'。{len(review_rows)} 条确认违规的记录没有命中高风险交易规则')
    if pending_count:
        parts.append(
            f'；另有 {pending_count} 条还挂在「待复核」，未计入任何分布'
        )
    if stats['is_small_sample']:
        parts.append(
            f'。样本不足 {MIN_MEASURED_EVENTS} 条，占比只作参考'
        )
    return ''.join(parts)


def build_moderation_rule_quality(days=30, now=None):
    """统计内容审核规则的误判与漏判情况，只报告不改写。"""
    allowed_days = {value for value, _ in PERIOD_CHOICES}
    if days not in allowed_days:
        days = 30
    now = now or timezone.now()

    start = _period_start(days, timezone.localtime(now).date())
    events = _fetch_events(reviewed_after=start, reviewed_before=now)
    review_list = [event.review_seconds for event in events]
    review_rows = _bucket_rows(review_list, REVIEW_BUCKETS)
    rule_rows = _rule_rows(events)
    score_rows = _score_rows(events)
    channel_rows = _channel_rows(events)
    missed_rows, missed_total = _missed_rows(events)

    confirmed_count = sum(1 for event in events if event.row.status == 'confirmed')
    dismissed_count = sum(1 for event in events if event.row.status == 'dismissed')
    stats = {
        'measured_count': len(events),
        'confirmed_count': confirmed_count,
        'dismissed_count': dismissed_count,
        'false_positive_rate': _percent(dismissed_count, len(events)),
        'mean_label': _format_elapsed(_mean(review_list)),
        'median_label': _format_elapsed(_percentile(review_list, 0.5)),
        'p90_label': _format_elapsed(_percentile(review_list, 0.9)),
        'rows': review_rows,
        'is_small_sample': len(events) < MIN_MEASURED_EVENTS,
    }

    # 还挂在 pending 上的审核单独数，不计入任何分布：它们没有复核时刻，是右删失
    # 样本。按 created_at 取窗口，因为要数的是「这段时间里拦下来的内容，有多少还没有
    # 人复核」。
    pending_count = ModerationEvent.objects.filter(
        status='pending', created_at__gte=start,
    ).count()

    return {
        'period_days': days,
        'period_start': start,
        'period_end': now,
        'has_data': bool(events),
        'summary': _summary(stats, rule_rows, score_rows, review_rows, pending_count),
        'stats': stats,
        'rule_rows': rule_rows[:RULE_LIST_LIMIT],
        'rule_row_total': len(rule_rows),
        'rule_row_limit': RULE_LIST_LIMIT,
        'score_rows': score_rows,
        'channel_rows': channel_rows,
        'missed_rows': missed_rows,
        'missed_row_total': missed_total,
        'missed_row_limit': REVIEW_LIST_LIMIT,
        'pending_count': pending_count,
        'false_positive_points': FALSE_POSITIVE_POINTS,
        'false_negative_points': FALSE_NEGATIVE_POINTS,
        'min_measured_events': MIN_MEASURED_EVENTS,
        'min_rule_events': MIN_RULE_EVENTS,
    }
